"""OpenAI Administration Usage API collector.

Calls the organization-scoped ``/v1/organization/usage/completions`` endpoint
with an admin API key to pull authoritative token counts for direct API
customers. One :class:`UsageEvent` is emitted per result row inside every
returned bucket; pagination is followed until the server stops returning a
``next_page`` cursor.

See section 8 of ``TOKIE_DEVELOPMENT_PLAN_FINAL.md`` for why this collector is
``exact`` confidence: OpenAI's admin API is the same source of truth that
drives their billing dashboard.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from tokie_cli.collectors.base import Collector, CollectorError, CollectorHealth
from tokie_cli.schema import Confidence, UsageEvent, compute_raw_hash

logger = logging.getLogger(__name__)

_KEYRING_SERVICE = "tokie-openai"
_KEYRING_USERNAME = "admin_api_key"
_USAGE_PATH = "/v1/organization/usage/completions"
_DEFAULT_LOOKBACK = timedelta(days=30)
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SEC = 1.0
_AUTH_ERROR_MSG = "authentication failed; check your OpenAI admin API key"


def _load_api_key_from_keyring() -> str | None:
    """Fetch the admin API key from the system keyring, if present.

    Isolated in a helper so tests can monkeypatch ``keyring.get_password``
    without importing :mod:`keyring` eagerly at module import time.
    """

    try:
        import keyring
    except ImportError:  # pragma: no cover - keyring is a hard dep
        return None
    try:
        value = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
    except Exception:  # pragma: no cover - backend-specific
        return None
    if isinstance(value, str) and value:
        return value
    return None


class OpenAIAPICollector(Collector):
    """Collector for the OpenAI Administration Usage API.

    Credentials are stored in the system keyring under service
    ``tokie-openai`` / username ``admin_api_key``. The key is loaded lazily on
    :meth:`scan` so :meth:`detect` remains side-effect-free, and it is never
    written to logs, error messages, or ``__repr__`` output.
    """

    name = "openai-api"
    default_confidence = Confidence.EXACT

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com",
        bucket_width: str = "1h",
        account_id: str = "default",
        timeout_sec: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key_override = api_key
        self.base_url = base_url.rstrip("/")
        self.bucket_width = bucket_width
        self.account_id = account_id
        self.timeout_sec = timeout_sec
        # ``transport`` is kept private so tests can inject ``httpx.MockTransport``
        # without widening the public contract.
        self._transport = transport

    def __repr__(self) -> str:
        # Hard guarantee: the key never reaches a traceback or debug print via
        # the default dataclass-style repr.
        return (
            f"OpenAIAPICollector(base_url={self.base_url!r}, "
            f"bucket_width={self.bucket_width!r}, account_id={self.account_id!r})"
        )

    @classmethod
    def detect(cls) -> bool:
        """Return True when an admin API key is present in the keyring.

        Kept fast and read-only: no network, no token probe. A stored key is
        treated as "data source exists" because without the key there's nothing
        we can legitimately fetch.
        """

        return _load_api_key_from_keyring() is not None

    def _resolve_api_key(self) -> str:
        """Return the admin API key or raise :class:`CollectorError`.

        Precedence: constructor override -> keyring lookup. The error message
        is intentionally generic so a stray log line never leaks the key.
        """

        if self._api_key_override:
            return self._api_key_override
        stored = _load_api_key_from_keyring()
        if stored:
            return stored
        raise CollectorError("openai admin api key not configured — run 'tokie init'")

    def scan(self, since: datetime | None = None) -> AsyncIterator[UsageEvent]:
        """Yield usage events from the admin API since ``since`` (UTC).

        ``since`` defaults to 30 days ago when ``None``. The admin API only
        retains usage for ~30 days, so this matches the server-side window.
        """

        return self._scan(since)

    async def _scan(self, since: datetime | None) -> AsyncIterator[UsageEvent]:
        api_key = self._resolve_api_key()
        start_time = self._compute_start_time(since)

        params: dict[str, Any] = {
            "start_time": start_time,
            "bucket_width": self.bucket_width,
        }
        headers = {"Authorization": f"Bearer {api_key}"}

        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_sec,
            transport=self._transport,
        ) as client:
            while True:
                payload = await self._request_page(client, headers, params)
                for event in self._payload_to_events(payload):
                    yield event
                next_page = payload.get("next_page")
                if not payload.get("has_more") or not isinstance(next_page, str) or not next_page:
                    return
                params = {**params, "page": next_page}

    @staticmethod
    def _compute_start_time(since: datetime | None) -> int:
        """Convert a tz-aware ``since`` to the int epoch seconds the API wants."""

        if since is None:
            return int((datetime.now(UTC) - _DEFAULT_LOOKBACK).timestamp())
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        return int(since.timestamp())

    async def _request_page(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """GET one page with retry on 429/5xx and sanitized errors on failure.

        Never raises any exception that would include the API key: auth errors
        produce a fixed string, transport errors mention only the exception
        *type*, and upstream error bodies are dropped.
        """

        last_error: str | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = await client.get(_USAGE_PATH, headers=headers, params=params)
            except httpx.TimeoutException as exc:
                last_error = f"timeout ({type(exc).__name__})"
            except httpx.HTTPError as exc:
                last_error = f"transport error ({type(exc).__name__})"
            else:
                status = response.status_code
                if status in (401, 403):
                    raise CollectorError(_AUTH_ERROR_MSG)
                if status in _RETRY_STATUS:
                    last_error = f"upstream status {status}"
                elif status >= 400:
                    raise CollectorError(f"openai usage api returned status {status}")
                else:
                    try:
                        data = response.json()
                    except ValueError as exc:
                        raise CollectorError(
                            f"openai usage api returned invalid json ({type(exc).__name__})"
                        ) from None
                    if not isinstance(data, dict):
                        raise CollectorError("openai usage api returned unexpected payload")
                    return data

            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(_BACKOFF_BASE_SEC * (2**attempt))

        raise CollectorError(
            f"openai usage api unavailable after {_MAX_ATTEMPTS} attempts: {last_error}"
        )

    def _payload_to_events(self, payload: dict[str, Any]) -> list[UsageEvent]:
        """Flatten one response page into a list of :class:`UsageEvent`."""

        events: list[UsageEvent] = []
        buckets = payload.get("data")
        if not isinstance(buckets, list):
            return events

        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            start_time = bucket.get("start_time")
            if not isinstance(start_time, int):
                continue
            results = bucket.get("results")
            if not isinstance(results, list):
                continue

            occurred_at = datetime.fromtimestamp(start_time, tz=UTC)
            for result in results:
                if not isinstance(result, dict):
                    continue
                events.append(self._result_to_event(occurred_at, start_time, result))
        return events

    def _result_to_event(
        self, occurred_at: datetime, start_time: int, result: dict[str, Any]
    ) -> UsageEvent:
        """Build one :class:`UsageEvent` from a single result row."""

        model_raw = result.get("model")
        model = model_raw if isinstance(model_raw, str) and model_raw else "unknown"
        project_raw = result.get("project_id")
        project = project_raw if isinstance(project_raw, str) and project_raw else None

        input_tokens = _non_negative_int(result.get("input_tokens"))
        output_tokens = _non_negative_int(result.get("output_tokens"))
        cache_read = _non_negative_int(result.get("input_cached_tokens"))

        hash_payload: dict[str, Any] = {
            "start_time": start_time,
            "model": model,
            "project_id": result.get("project_id"),
            "user_id": result.get("user_id"),
            "api_key_id": result.get("api_key_id"),
            "input": input_tokens,
            "output": output_tokens,
            "cached": cache_read,
        }

        return self.make_event(
            occurred_at=occurred_at,
            provider="openai",
            product="openai-api",
            account_id=self.account_id,
            session_id=None,
            project=project,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=0,
            reasoning_tokens=0,
            cost_usd=None,
            raw_hash=compute_raw_hash(hash_payload),
            source=f"openai_api:{self.bucket_width}:{start_time}:{model}",
            confidence=Confidence.EXACT,
        )

    def health(self) -> CollectorHealth:
        """Report credential presence without touching the network."""

        detected = self.detect() or bool(self._api_key_override)
        if not detected:
            return CollectorHealth(
                name=self.name,
                detected=False,
                ok=False,
                last_scan_at=None,
                last_scan_events=0,
                message="openai admin api key not configured",
            )
        return CollectorHealth(
            name=self.name,
            detected=True,
            ok=True,
            last_scan_at=None,
            last_scan_events=0,
            message=f"admin api key configured; bucket_width={self.bucket_width}",
        )


def _non_negative_int(value: object) -> int:
    """Coerce a JSON value to a non-negative int, or 0 on any ambiguity.

    Booleans are explicitly rejected because ``bool`` is an ``int`` subclass
    and would otherwise sneak through as 0/1 for the ``batch`` field if it
    ever leaked into a token column.
    """

    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value >= 0 else 0
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return 0


__all__ = ["OpenAIAPICollector"]
