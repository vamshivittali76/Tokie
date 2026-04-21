"""Anthropic Admin API usage collector.

Pulls exact usage from the Anthropic Admin API endpoint
``/v1/organizations/usage_report/messages`` for *direct API customers*.

This collector does **not** cover Claude Pro/Max subscribers — those are
billed via subscription quotas and tracked by the ``claude_code``
collector. Mixing the two would double-count tokens.

The admin key is fetched from the system keyring under service
``tokie-anthropic`` / username ``admin_api_key`` and MUST NEVER be
logged or echoed in error messages.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import keyring

from tokie_cli.collectors.base import Collector, CollectorError, CollectorHealth
from tokie_cli.schema import Confidence, UsageEvent, compute_raw_hash

KEYRING_SERVICE = "tokie-anthropic"
KEYRING_USERNAME = "admin_api_key"
ANTHROPIC_VERSION = "2023-06-01"
USAGE_PATH = "/v1/organizations/usage_report/messages"
DEFAULT_LOOKBACK_DAYS = 30
MAX_RETRIES = 3
BACKOFF_BASE_SEC = 1.0
PAGE_LIMIT = 1000


def _rfc3339(dt: datetime) -> str:
    """Render a tz-aware datetime as the ``YYYY-MM-DDTHH:MM:SSZ`` form the API expects."""

    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_bucket_start(value: str) -> datetime:
    """Parse an API bucket ``starting_at`` string into a tz-aware UTC datetime."""

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class AnthropicAPICollector(Collector):
    """Collector for Anthropic direct-API customers (Admin API usage report)."""

    name = "anthropic-api"
    default_confidence = Confidence.EXACT

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.anthropic.com",
        bucket_width: str = "1h",
        account_id: str = "default",
        timeout_sec: float = 30.0,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._bucket_width = bucket_width
        self._account_id = account_id
        self._timeout_sec = timeout_sec
        self._transport = _transport

    @classmethod
    def detect(cls) -> bool:
        """Return True iff an admin API key is stored in the keyring.

        Never performs a network call — keyring access only.
        """

        try:
            stored = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
        except Exception:
            return False
        return bool(stored)

    def health(self) -> CollectorHealth:
        detected = self.detect()
        return CollectorHealth(
            name=self.name,
            detected=detected,
            ok=detected,
            last_scan_at=None,
            last_scan_events=0,
            message=(
                "anthropic admin api key configured"
                if detected
                else "anthropic admin api key not configured"
            ),
        )

    def scan(self, since: datetime | None = None) -> AsyncIterator[UsageEvent]:
        return self._scan(since)

    def _resolve_key(self) -> str:
        if self._api_key:
            return self._api_key
        try:
            stored = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
        except Exception as exc:
            raise CollectorError(
                "anthropic admin api key not configured — run 'tokie init'"
            ) from exc
        if not stored:
            raise CollectorError("anthropic admin api key not configured — run 'tokie init'")
        return stored

    async def _scan(self, since: datetime | None) -> AsyncIterator[UsageEvent]:
        api_key = self._resolve_key()
        start_dt = (
            since
            if since is not None
            else datetime.now(UTC) - timedelta(days=DEFAULT_LOOKBACK_DAYS)
        )
        end_dt = datetime.now(UTC)

        headers = {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        params: dict[str, Any] = {
            "starting_at": _rfc3339(start_dt),
            "ending_at": _rfc3339(end_dt),
            "bucket_width": self._bucket_width,
            "limit": PAGE_LIMIT,
        }

        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout_sec,
            transport=self._transport,
        ) as client:
            while True:
                payload = await self._request(client, headers, params)
                data = payload.get("data") or []
                if not isinstance(data, list):
                    raise CollectorError("Anthropic API returned unexpected payload shape")
                for bucket in data:
                    if not isinstance(bucket, dict):
                        continue
                    bucket_start = bucket.get("starting_at")
                    if not isinstance(bucket_start, str):
                        continue
                    results = bucket.get("results") or []
                    if not isinstance(results, list):
                        continue
                    for result in results:
                        if not isinstance(result, dict):
                            continue
                        yield self._build_event(bucket_start, result)

                if not payload.get("has_more"):
                    return
                next_page = payload.get("next_page")
                if not next_page:
                    return
                params = {**params, "page": next_page}

    async def _request(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        params: dict[str, Any],
    ) -> dict[str, Any]:
        last_status: int | None = None
        for attempt in range(MAX_RETRIES):
            try:
                response = await client.get(USAGE_PATH, headers=headers, params=params)
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt == MAX_RETRIES - 1:
                    raise CollectorError(
                        "Anthropic API unreachable; check your network connection"
                    ) from None
                await asyncio.sleep(BACKOFF_BASE_SEC * (2**attempt))
                continue

            status = response.status_code
            if status in (401, 403):
                raise CollectorError("authentication failed; check your Anthropic admin API key")
            if status == 429 or status >= 500:
                last_status = status
                if attempt == MAX_RETRIES - 1:
                    break
                await asyncio.sleep(BACKOFF_BASE_SEC * (2**attempt))
                continue
            if status >= 400:
                raise CollectorError(f"Anthropic API request failed (status {status})")

            try:
                body = response.json()
            except ValueError as exc:
                raise CollectorError("Anthropic API returned invalid JSON") from exc
            if not isinstance(body, dict):
                raise CollectorError("Anthropic API returned unexpected payload shape")
            return body

        raise CollectorError(
            f"Anthropic API unavailable; try again later (last status {last_status})"
        )

    def _build_event(self, bucket_start: str, result: dict[str, Any]) -> UsageEvent:
        occurred = _parse_bucket_start(bucket_start)
        model = str(result.get("model") or "unknown")
        uncached = int(result.get("uncached_input_tokens") or 0)
        cache_creation = int(result.get("cache_creation_input_tokens") or 0)
        cache_read = int(result.get("cache_read_input_tokens") or 0)
        output = int(result.get("output_tokens") or 0)
        service_tier = str(result.get("service_tier") or "")

        raw_hash = compute_raw_hash(
            {
                "bucket_start": bucket_start,
                "model": model,
                "uncached": uncached,
                "cache_creation": cache_creation,
                "cache_read": cache_read,
                "output": output,
                "service_tier": service_tier,
            }
        )

        return self.make_event(
            occurred_at=occurred,
            provider="anthropic",
            product="anthropic-api",
            account_id=self._account_id,
            model=model,
            input_tokens=uncached,
            output_tokens=output,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_creation,
            reasoning_tokens=0,
            cost_usd=None,
            source=f"anthropic_api:{self._bucket_width}:{bucket_start}:{model}",
            raw_hash=raw_hash,
        )


__all__ = ["AnthropicAPICollector"]
