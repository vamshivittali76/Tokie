"""Generic OpenAI-compatible NDJSON log collector.

One collector, many providers. This module parses a local newline-delimited
JSON log written by the user's application (or by the ``litellm`` proxy) and
emits one :class:`UsageEvent` per line. Because almost every hosted provider
in the OpenAI-compatible ecosystem speaks the same chat-completions wire
format and returns the same ``usage`` block shape, a single parser covers an
enormous long tail.

Supported providers (non-exhaustive): Groq, Together AI, DeepSeek, OpenRouter,
Mistral, xAI Grok, Fireworks, Anyscale, Perplexity Sonar, Cerebras, Ollama,
vLLM, LiteLLM, and any local or self-hosted OpenAI-compatible gateway.

This is the "bring your own log" pattern. None of the providers above expose
a reliable historical usage admin endpoint, so rather than silently inventing
numbers we require the user to point us at a file (or directory) that their
own code or proxy is writing. The upside: ``Confidence.EXACT`` for free —
these are the actual usage blocks the provider returned to the caller.

Line contract (enforced; anything else is skipped with a logger warning that
never contains prompt or response content)::

    {
      "timestamp": "2026-04-20T12:34:56Z",
      "provider": "groq",
      "model": "llama-3.1-70b-versatile",
      "usage": {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150
      }
    }

Optional fields: ``cached_tokens`` or ``prompt_tokens_details.cached_tokens``
(``cache_read_tokens``), ``reasoning_tokens`` or
``completion_tokens_details.reasoning_tokens`` (``reasoning_tokens``),
``session_id``, ``account_id`` (per-line override), and ``product``
(per-line override).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tokie_cli.collectors.base import Collector, CollectorHealth, aiterate
from tokie_cli.schema import Confidence, UsageEvent, compute_raw_hash

logger = logging.getLogger(__name__)

_ENV_VAR = "TOKIE_OPENAI_COMPAT_LOG"
_LOG_SUFFIXES: tuple[str, ...] = (".jsonl", ".ndjson")


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp into a tz-aware UTC datetime.

    Accepts trailing ``Z`` and assumes naive timestamps are already UTC; the
    log contract says timestamps are UTC so anything else is caller error
    rather than something we should silently guess at.
    """

    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _non_negative_int(value: object) -> int:
    """Coerce a JSON value to a non-negative int, defaulting to zero.

    Pydantic would reject a negative count downstream, but we never want a
    malformed counter to take out an entire scan — the other fields in the
    line are still useful.
    """

    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value >= 0 else 0
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return 0


def _extract_cache_read(usage: dict[str, Any]) -> int:
    """Return the cached-prompt token count across both known key shapes.

    Providers either hang the counter off the top-level ``cached_tokens`` key
    (Groq, DeepSeek's older responses) or nest it under
    ``prompt_tokens_details.cached_tokens`` (OpenAI, DeepSeek v3, OpenRouter).
    Top-level wins when both are present because it's closer to the caller.
    """

    if "cached_tokens" in usage:
        return _non_negative_int(usage.get("cached_tokens"))
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        return _non_negative_int(details.get("cached_tokens"))
    return 0


def _extract_reasoning(usage: dict[str, Any]) -> int:
    """Return reasoning/thinking tokens, honoring both flat and nested keys.

    Reasoning models (o-series, DeepSeek R1, Grok thinking, some OpenRouter
    routes) expose this under ``completion_tokens_details.reasoning_tokens``;
    a few community proxies flatten it to ``reasoning_tokens``. Again the
    flat key wins when both exist.
    """

    if "reasoning_tokens" in usage:
        return _non_negative_int(usage.get("reasoning_tokens"))
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict):
        return _non_negative_int(details.get("reasoning_tokens"))
    return 0


class OpenAICompatibleCollector(Collector):
    """Parse a local NDJSON log into canonical :class:`UsageEvent` rows.

    The user (or their LiteLLM proxy) is responsible for writing one JSON
    object per line in the documented shape. We never generate, mutate, or
    reach out over the network for anything — this is a pure filesystem
    parser, which is why its default confidence is ``EXACT``.
    """

    name = "openai-compat"
    default_confidence = Confidence.EXACT

    def __init__(
        self,
        *,
        log_path: Path,
        default_account_id: str = "default",
        default_provider: str | None = None,
    ) -> None:
        self.log_path: Path = log_path
        self._default_account_id = default_account_id
        self._default_provider = default_provider

    @classmethod
    def detect(cls) -> bool:
        """True iff ``TOKIE_OPENAI_COMPAT_LOG`` points at an existing path.

        This collector is intentionally opt-in: without an explicit log path
        there is no way to guess where the user's proxy is writing, and we
        refuse to scan ``$HOME`` speculatively. Side-effect-free.
        """

        raw = os.environ.get(_ENV_VAR)
        if not raw:
            return False
        return Path(raw).expanduser().exists()

    def scan(self, since: datetime | None = None) -> AsyncIterator[UsageEvent]:
        """Yield one :class:`UsageEvent` per well-formed line in the log(s)."""

        return aiterate(self._iter_events(since))

    def _iter_events(self, since: datetime | None) -> Iterator[UsageEvent]:
        for path in self._iter_log_files():
            yield from self._iter_file(path, since)

    def _iter_log_files(self) -> Iterator[Path]:
        """Yield every log file under :attr:`log_path` in a stable order.

        Accepts either a single file (any extension — we trust the user's
        explicit choice) or a directory, in which case we recursively pick
        up ``*.jsonl`` and ``*.ndjson``. Non-existent paths simply yield
        nothing so callers can scan before the proxy has written anything.
        """

        root = self.log_path
        if not root.exists():
            return
        if root.is_file():
            yield root
            return
        if not root.is_dir():
            return
        matches: list[Path] = []
        for suffix in _LOG_SUFFIXES:
            matches.extend(root.rglob(f"*{suffix}"))
        yield from sorted(set(matches))

    def _relative_source(self, path: Path) -> str:
        """Render ``path`` relative to the configured root for provenance.

        When :attr:`log_path` is itself a file we want the bare filename in
        the source string; for directories we want a tree-relative path so
        operators can paste it back into their shell. The ``posix()`` style
        is forced so the same log produces the same ``source`` on Windows
        and Linux (matters for ``raw_hash`` dedup checks in tests).
        """

        root = self.log_path
        base = root.parent if root.is_file() else root
        try:
            return path.relative_to(base).as_posix()
        except ValueError:
            return path.as_posix()

    def _iter_file(self, path: Path, since: datetime | None) -> Iterator[UsageEvent]:
        rel = self._relative_source(path)
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("openai-compat: cannot open %s (%s)", rel, type(exc).__name__)
            return
        with handle as fp:
            for lineno, raw_line in enumerate(fp, start=1):
                line = raw_line.rstrip("\n").rstrip("\r")
                if not line.strip():
                    continue
                event = self._parse_line(line, rel=rel, lineno=lineno)
                if event is None:
                    continue
                if since is not None and event.occurred_at < since:
                    continue
                yield event

    def _parse_line(self, line: str, *, rel: str, lineno: int) -> UsageEvent | None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("openai-compat: malformed json at %s:%d", rel, lineno)
            return None
        if not isinstance(payload, dict):
            logger.warning("openai-compat: non-object line at %s:%d", rel, lineno)
            return None

        usage = payload.get("usage")
        if not isinstance(usage, dict):
            logger.warning("openai-compat: missing usage at %s:%d", rel, lineno)
            return None

        occurred_at = _parse_timestamp(payload.get("timestamp"))
        if occurred_at is None:
            logger.warning("openai-compat: missing/bad timestamp at %s:%d", rel, lineno)
            return None

        model = payload.get("model")
        if not isinstance(model, str) or not model:
            logger.warning("openai-compat: missing model at %s:%d", rel, lineno)
            return None

        provider_raw = payload.get("provider")
        provider = (
            provider_raw
            if isinstance(provider_raw, str) and provider_raw
            else (self._default_provider or "openai-compat")
        )

        product_raw = payload.get("product")
        product = product_raw if isinstance(product_raw, str) and product_raw else f"{provider}-api"

        account_raw = payload.get("account_id")
        account_id = (
            account_raw
            if isinstance(account_raw, str) and account_raw
            else self._default_account_id
        )

        session_raw = payload.get("session_id")
        session_id = session_raw if isinstance(session_raw, str) and session_raw else None

        try:
            return self.make_event(
                occurred_at=occurred_at,
                provider=provider,
                product=product,
                account_id=account_id,
                session_id=session_id,
                project=None,
                model=model,
                input_tokens=_non_negative_int(usage.get("prompt_tokens")),
                output_tokens=_non_negative_int(usage.get("completion_tokens")),
                cache_read_tokens=_extract_cache_read(usage),
                cache_write_tokens=0,
                reasoning_tokens=_extract_reasoning(usage),
                cost_usd=None,
                raw_hash=compute_raw_hash(line),
                source=f"openai_compat:{provider}:{rel}:{lineno}",
                confidence=Confidence.EXACT,
            )
        except ValueError as exc:
            logger.warning(
                "openai-compat: invalid row at %s:%d (%s)",
                rel,
                lineno,
                type(exc).__name__,
            )
            return None

    def health(self) -> CollectorHealth:
        """Report whether the configured log path is present and readable."""

        root = self.log_path
        if not root.exists():
            return CollectorHealth(
                name=self.name,
                detected=False,
                ok=False,
                last_scan_at=None,
                last_scan_events=0,
                message=f"log path not found: {root}",
            )

        warnings: list[str] = []
        files: list[Path] = list(self._iter_log_files())
        if root.is_dir() and not files:
            warnings.append(f"no *.jsonl or *.ndjson under {root}")

        latest: datetime | None = None
        for path in files:
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            except OSError as exc:
                warnings.append(f"unreadable: {path.name} ({type(exc).__name__})")
                continue
            if latest is None or mtime > latest:
                latest = mtime

        if root.is_file():
            message = f"log file: {root}"
        else:
            message = f"{len(files)} log file(s) under {root}"

        return CollectorHealth(
            name=self.name,
            detected=True,
            ok=not warnings,
            last_scan_at=latest,
            last_scan_events=0,
            message=message,
            warnings=tuple(warnings),
        )


__all__ = ["OpenAICompatibleCollector"]
