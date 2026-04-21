"""Collector for the local OpenAI Codex CLI session rollouts.

The Codex CLI (``codex``, formerly ``@openai/codex``) writes JSONL session
rollouts under ``~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<uuid>.jsonl`` on
macOS/Linux and the equivalent path under ``%USERPROFILE%`` on Windows. We
parse those rollouts, look for lines that carry a ``usage`` block, and emit
one :class:`UsageEvent` per completed response.

No network I/O. Filesystem only.

Two wire shapes are tolerated because the format has drifted across Codex
versions:

* Shape A — the current responses-API style::

    {"type": "response_complete", "usage": {"input_tokens": ..., "output_tokens": ...}}

* Shape B — older chat-completions style::

    {"role": "assistant", "usage": {"prompt_tokens": ..., "completion_tokens": ...}}

Lines missing both ``usage.input_tokens`` and ``usage.prompt_tokens`` are
skipped silently.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from tokie_cli.collectors.base import (
    Collector,
    CollectorHealth,
    aiterate,
)
from tokie_cli.schema import Confidence, UsageEvent, compute_raw_hash

logger = logging.getLogger(__name__)

_ENV_VAR = "TOKIE_CODEX_SESSION_ROOT"


def _default_session_root() -> Path:
    """Best-guess session root, honoring ``TOKIE_CODEX_SESSION_ROOT`` first.

    We check the env var, then the primary ``~/.codex/sessions`` location,
    then the XDG-style fallback. The first candidate that exists wins; if
    none exist, the primary location is returned so ``detect()`` can report
    "not present" without raising.
    """

    override = os.environ.get(_ENV_VAR)
    if override:
        return Path(override)

    home = Path.home()
    primary = home / ".codex" / "sessions"
    fallback = home / ".config" / "codex" / "sessions"
    if primary.exists():
        return primary
    if fallback.exists():
        return fallback
    return primary


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp into a tz-aware UTC datetime.

    Returns ``None`` on anything unrecognizable so the caller can decide
    whether to skip the line or fall back to a file mtime.
    """

    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _extract_tokens(usage: dict[str, Any]) -> tuple[int, int, int, int] | None:
    """Return ``(input, output, cache_read, reasoning)`` or ``None`` to skip.

    Handles both Shape A (responses API) and Shape B (chat-completions).
    Shape A is preferred when both sets of keys are present.
    """

    if "input_tokens" in usage or "output_tokens" in usage:
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        cache_read = int(usage.get("cached_input_tokens") or 0)
        reasoning = int(usage.get("reasoning_tokens") or 0)
        return input_tokens, output_tokens, cache_read, reasoning

    if "prompt_tokens" in usage or "completion_tokens" in usage:
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        cache_read = int(usage.get("cached_tokens") or 0)
        return input_tokens, output_tokens, cache_read, 0

    return None


class CodexCollector(Collector):
    """Parse local OpenAI Codex CLI session JSONL rollouts."""

    name = "codex"
    default_confidence = Confidence.EXACT

    def __init__(self, session_root: Path | None = None) -> None:
        self.session_root: Path = session_root or _default_session_root()

    @classmethod
    def detect(cls) -> bool:
        """Fast, side-effect-free check for the session root."""

        return _default_session_root().exists()

    def scan(self, since: datetime | None = None) -> AsyncIterator[UsageEvent]:
        """Yield one :class:`UsageEvent` per usage-bearing JSONL line."""

        return aiterate(self._iter_events(since))

    def _iter_events(self, since: datetime | None) -> Iterator[UsageEvent]:
        root = self.session_root
        if not root.exists() or not root.is_dir():
            return
        for path in sorted(root.rglob("*.jsonl")):
            yield from self._iter_file(path, since)

    def _iter_file(self, path: Path, since: datetime | None) -> Iterator[UsageEvent]:
        try:
            rel = path.relative_to(self.session_root)
        except ValueError:
            rel = path
        rel_str = rel.as_posix() if isinstance(rel, Path) else str(rel)

        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("codex: cannot open %s (%s)", path.name, type(exc).__name__)
            return

        with handle as fp:
            for lineno, raw_line in enumerate(fp, start=1):
                line = raw_line.rstrip("\n").rstrip("\r")
                if not line.strip():
                    continue
                event = self._parse_line(line, path=path, rel=rel_str, lineno=lineno)
                if event is None:
                    continue
                if since is not None and event.occurred_at < since:
                    continue
                yield event

    def _parse_line(
        self,
        line: str,
        *,
        path: Path,
        rel: str,
        lineno: int,
    ) -> UsageEvent | None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("codex: malformed json at %s:%d", path.name, lineno)
            return None
        if not isinstance(payload, dict):
            return None

        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return None
        tokens = _extract_tokens(usage)
        if tokens is None:
            return None
        input_tokens, output_tokens, cache_read, reasoning = tokens

        occurred_at = _parse_timestamp(payload.get("timestamp"))
        if occurred_at is None:
            return None

        model = payload.get("model")
        if not isinstance(model, str) or not model:
            return None

        raw_session = payload.get("session_id")
        session_id = raw_session if isinstance(raw_session, str) and raw_session else path.stem

        source = f"codex:{rel}:{lineno}"

        try:
            return self.make_event(
                occurred_at=occurred_at,
                provider="openai",
                product="codex",
                account_id="default",
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                reasoning_tokens=reasoning,
                raw_hash=compute_raw_hash(line),
                source=source,
                session_id=session_id,
                project=None,
            )
        except ValueError as exc:
            logger.warning(
                "codex: invalid usage row at %s:%d (%s)",
                path.name,
                lineno,
                type(exc).__name__,
            )
            return None

    def health(self) -> CollectorHealth:
        """Report whether the session root is readable and list soft warnings."""

        root = self.session_root
        warnings: list[str] = []

        if not root.exists():
            return CollectorHealth(
                name=self.name,
                detected=False,
                ok=False,
                last_scan_at=None,
                last_scan_events=0,
                message=f"session root not found: {root}",
            )
        if not root.is_dir():
            return CollectorHealth(
                name=self.name,
                detected=False,
                ok=False,
                last_scan_at=None,
                last_scan_events=0,
                message=f"session root is not a directory: {root}",
            )

        files = 0
        for path in root.rglob("*.jsonl"):
            files += 1
            if not os.access(path, os.R_OK):
                warnings.append(f"unreadable: {path.name}")

        message = f"{files} session file(s) under {root}"
        return CollectorHealth(
            name=self.name,
            detected=True,
            ok=not warnings,
            last_scan_at=None,
            last_scan_events=0,
            message=message,
            warnings=tuple(warnings),
        )


__all__ = ["CodexCollector"]
