"""Collector for Perplexity API response logs.

**Why no direct HTTP call?** As of v0.2 Perplexity does not expose a public
per-account historical usage endpoint; their ``/chat/completions`` responses
do carry a ``usage`` block per call, but the server-side aggregation view is
only reachable through the authenticated web dashboard at
``https://www.perplexity.ai/account/api``.

So v0.2's Perplexity strategy mirrors ``api_gemini``: tail a local NDJSON
file the user produces from their own client wrapper. Every line is expected
to carry at minimum::

    {"timestamp": "2026-04-20T12:34:56Z",
     "model":     "sonar-large-online",
     "usage":     {"prompt_tokens": 120, "completion_tokens": 60}}

Optional fields: ``session_id``, ``project``, ``cost_usd``, ``account_id``.
An empty ``timestamp`` or missing ``usage`` block causes the line to be
skipped silently. No prompt text, citations, or search results are parsed
or logged — only metadata and token counts leave this module.

When/if Perplexity ships a vendor-level usage endpoint, this collector will
grow an HTTP path alongside the existing log tail. The keyring slot
``tokie-perplexity/api_key`` is already reserved for that future upgrade.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from tokie_cli.collectors.base import Collector, CollectorHealth, aiterate
from tokie_cli.schema import Confidence, UsageEvent, compute_raw_hash

logger = logging.getLogger(__name__)

_KEYRING_SERVICE = "tokie-perplexity"
_KEYRING_USERNAME = "api_key"
_ENV_VAR = "TOKIE_PERPLEXITY_LOG"
_SUPPORTED_SUFFIXES: tuple[str, ...] = (".jsonl", ".ndjson")


def _candidate_roots() -> tuple[Path, ...]:
    home = Path.home()
    return (
        home / ".perplexity" / "history",
        home / ".config" / "perplexity" / "history",
        home / "AppData" / "Roaming" / "Perplexity" / "history",
    )


def _resolve_paths() -> list[Path]:
    override = os.environ.get(_ENV_VAR)
    paths: list[Path] = []
    if override:
        root = Path(override)
        if root.is_file() and root.suffix.lower() in _SUPPORTED_SUFFIXES:
            paths.append(root)
        elif root.is_dir():
            paths.extend(sorted(_walk_logs(root)))

    for candidate in _candidate_roots():
        if candidate.is_dir():
            paths.extend(sorted(_walk_logs(candidate)))

    seen: set[Path] = set()
    unique: list[Path] = []
    for p in paths:
        resolved = p.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(p)
    return unique


def _walk_logs(root: Path) -> Iterator[Path]:
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in _SUPPORTED_SUFFIXES:
            yield p


def _parse_timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_usage(record: dict[str, Any]) -> tuple[int, int] | None:
    usage = record.get("usage")
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    return input_tokens, output_tokens


def _keyring_has_key() -> bool:
    """True when a Perplexity API key is stored — reserved for future HTTP path."""

    try:
        import keyring
    except ImportError:  # pragma: no cover
        return False
    try:
        value = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
    except Exception:  # pragma: no cover - backend-specific
        return False
    return isinstance(value, str) and bool(value)


class PerplexityAPICollector(Collector):
    """Log-tail collector for Perplexity API response drops.

    The HTTP client path is intentionally NOT wired yet — vendor gap. The
    keyring lookup exists so future upgrades don't require a detect() rewrite.
    """

    name = "perplexity-api"
    default_confidence = Confidence.EXACT

    @classmethod
    def detect(cls) -> bool:
        # Present a data source if *either* a history drop or a stored API
        # key exists — `doctor` uses this to surface "configured but idle".
        return bool(_resolve_paths()) or _keyring_has_key()

    def scan(self, since: datetime | None = None) -> AsyncIterator[UsageEvent]:
        return aiterate(self._scan_sync(since))

    def _scan_sync(self, since: datetime | None) -> Iterator[UsageEvent]:
        for path in _resolve_paths():
            try:
                yield from self._scan_file(path, since)
            except OSError as exc:
                logger.warning(
                    "perplexity-api: cannot read %s (%s)", path.name, type(exc).__name__
                )

    def _scan_file(self, path: Path, since: datetime | None) -> Iterator[UsageEvent]:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line_no, raw in enumerate(fh, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                usage = _extract_usage(record)
                if usage is None:
                    continue
                occurred_at = _parse_timestamp(record.get("timestamp"))
                if occurred_at is None:
                    continue
                if since is not None and occurred_at < since:
                    continue

                yield self.make_event(
                    occurred_at=occurred_at,
                    provider="perplexity",
                    product="perplexity-api",
                    account_id=str(record.get("account_id", "default")),
                    model=str(record.get("model", "perplexity-unknown")),
                    input_tokens=int(usage[0]),
                    output_tokens=int(usage[1]),
                    session_id=(
                        str(record["session_id"])
                        if isinstance(record.get("session_id"), str)
                        else None
                    ),
                    cost_usd=(
                        float(record["cost_usd"])
                        if isinstance(record.get("cost_usd"), int | float)
                        else None
                    ),
                    raw_hash=compute_raw_hash(raw),
                    source=f"perplexity-api:{path.name}:{line_no}",
                )

    def health(self) -> CollectorHealth:
        paths = _resolve_paths()
        has_key = _keyring_has_key()
        if not paths and not has_key:
            return CollectorHealth(
                name=self.name,
                detected=False,
                ok=False,
                last_scan_at=None,
                last_scan_events=0,
                message=(
                    "no perplexity history found; drop JSONL into "
                    "~/.perplexity/history/ or store API key in keyring"
                ),
            )
        warnings: list[str] = []
        if has_key and not paths:
            # Intentionally loud — we want users to know the HTTP path is a
            # vendor-gap follow-up and not already live.
            warnings.append(
                "api key stored but vendor has no historical usage endpoint "
                "yet; drop response NDJSON to populate usage"
            )
        message = f"found {len(paths)} log file(s)" if paths else "api key configured (idle)"
        return CollectorHealth(
            name=self.name,
            detected=True,
            ok=bool(paths),
            last_scan_at=None,
            last_scan_events=0,
            message=message,
            warnings=tuple(warnings),
        )


__all__ = ["PerplexityAPICollector"]
