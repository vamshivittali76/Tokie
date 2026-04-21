"""Claude Code session JSONL collector.

Parses local Claude Code session files written under ``~/.claude/projects``
and emits one :class:`UsageEvent` per assistant turn that carries a
``message.usage`` block. Pure filesystem reads, no network I/O.

See section 8 of ``TOKIE_DEVELOPMENT_PLAN_FINAL.md`` for why collectors are
split per product and why Claude Code is ``exact`` confidence: Anthropic
writes the authoritative token counts straight into the JSONL.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

from tokie_cli.collectors.base import Collector, CollectorHealth
from tokie_cli.schema import Confidence, UsageEvent, compute_raw_hash

logger = logging.getLogger(__name__)

_ENV_OVERRIDE = "TOKIE_CLAUDE_SESSION_ROOT"
_CANDIDATE_ROOTS: tuple[Path, ...] = (
    Path.home() / ".claude" / "projects",
    Path.home() / ".config" / "claude" / "projects",
)


def _default_session_root() -> Path:
    """Pick the first Claude Code session root that exists on this machine.

    Resolution order: ``$TOKIE_CLAUDE_SESSION_ROOT`` override, then the primary
    ``~/.claude/projects`` location, then the XDG-style Linux fallback. If
    nothing exists yet, return the primary path anyway so ``detect()`` can
    report ``False`` without raising.
    """

    override = os.environ.get(_ENV_OVERRIDE)
    if override:
        return Path(override).expanduser()
    for candidate in _CANDIDATE_ROOTS:
        if candidate.exists():
            return candidate
    return _CANDIDATE_ROOTS[0]


def _parse_timestamp(raw: str) -> datetime:
    """Parse a Claude Code timestamp and force it to UTC.

    Claude writes ``...Z`` suffixes that :func:`datetime.fromisoformat` only
    accepts since 3.11, so the replacement is defensive for bit-rot.
    """

    ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


class ClaudeCodeCollector(Collector):
    """Collector for local Claude Code session JSONL files.

    The collector walks every ``*.jsonl`` under :attr:`session_root` and
    yields a :class:`UsageEvent` for each assistant turn with a usage block.
    Non-usage lines (user turns, tool results, system events) are skipped
    silently. Malformed JSON and unreadable files never raise — they are
    logged and surfaced via :meth:`health`.
    """

    name = "claude-code"
    default_confidence = Confidence.EXACT

    def __init__(self, session_root: Path | None = None) -> None:
        self.session_root: Path = (
            session_root if session_root is not None else _default_session_root()
        )

    @classmethod
    def detect(cls) -> bool:
        """Fast probe: does the default Claude Code session directory exist?"""

        root = _default_session_root()
        return root.exists() and root.is_dir()

    def scan(self, since: datetime | None = None) -> AsyncIterator[UsageEvent]:
        """Walk every JSONL under :attr:`session_root` and yield usage events.

        ``since`` is compared inclusively: events whose ``occurred_at`` is
        strictly older than ``since`` are dropped. The implementation is an
        async generator so it plays nicely with :meth:`Collector.watch`.
        """

        return self._scan(since)

    async def _scan(self, since: datetime | None) -> AsyncIterator[UsageEvent]:
        if not self.session_root.exists():
            return
        files = sorted(self.session_root.rglob("*.jsonl"))
        for path in files:
            for event in self._iter_file_events(path, since):
                yield event

    def _iter_file_events(self, path: Path, since: datetime | None) -> Iterator[UsageEvent]:
        """Yield events from a single JSONL file without loading it whole."""

        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("claude-code: cannot open %s (%s)", path.name, type(exc).__name__)
            return

        try:
            rel_source = self._relative_source(path)
            with handle:
                for line_number, raw_line in enumerate(handle, start=1):
                    line = raw_line.rstrip("\n").rstrip("\r")
                    if not line.strip():
                        continue
                    event = self._line_to_event(line, rel_source, line_number, since)
                    if event is not None:
                        yield event
        except OSError as exc:
            logger.warning("claude-code: error reading %s (%s)", path.name, type(exc).__name__)

    def _relative_source(self, path: Path) -> str:
        """Render a path relative to ``session_root`` for provenance.

        Falls back to the absolute path if the file is somehow outside the
        configured root (symlinks, reparse points). The native OS separator
        is preserved so operators can paste the value straight into their
        shell.
        """

        try:
            return str(path.relative_to(self.session_root))
        except ValueError:
            return str(path)

    def _line_to_event(
        self,
        line: str,
        rel_source: str,
        line_number: int,
        since: datetime | None,
    ) -> UsageEvent | None:
        """Convert one JSONL line into a :class:`UsageEvent` or ``None``.

        Returns ``None`` when the line is malformed, has no usage block, or
        falls outside the ``since`` filter. Never raises.
        """

        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("claude-code: malformed json at %s:%d", rel_source, line_number)
            return None

        if not isinstance(record, dict):
            return None

        message = record.get("message")
        if not isinstance(message, dict):
            return None
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return None

        ts_raw = record.get("timestamp")
        if not isinstance(ts_raw, str):
            return None
        try:
            occurred_at = _parse_timestamp(ts_raw)
        except ValueError:
            logger.warning("claude-code: bad timestamp at %s:%d", rel_source, line_number)
            return None

        if since is not None and occurred_at < since:
            return None

        session_id = record.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            # Filename stem is a stable fallback: Claude Code names the file
            # after the session UUID.
            session_id = Path(rel_source).stem

        cwd = record.get("cwd")
        project: str | None = None
        if isinstance(cwd, str) and cwd:
            project = os.path.basename(cwd.rstrip("/\\")) or None

        model = message.get("model")
        if not isinstance(model, str) or not model:
            return None

        def _non_negative_int(value: object) -> int:
            if isinstance(value, bool):  # bool is an int subclass; reject.
                return 0
            if isinstance(value, int):
                return value if value >= 0 else 0
            if isinstance(value, float) and value.is_integer() and value >= 0:
                return int(value)
            return 0

        return self.make_event(
            occurred_at=occurred_at,
            provider="anthropic",
            product="claude-code",
            account_id="default",
            session_id=session_id,
            project=project,
            model=model,
            input_tokens=_non_negative_int(usage.get("input_tokens")),
            output_tokens=_non_negative_int(usage.get("output_tokens")),
            cache_write_tokens=_non_negative_int(usage.get("cache_creation_input_tokens")),
            cache_read_tokens=_non_negative_int(usage.get("cache_read_input_tokens")),
            reasoning_tokens=0,
            cost_usd=None,
            raw_hash=compute_raw_hash(line),
            source=f"claude_code:{rel_source}:{line_number}",
            confidence=Confidence.EXACT,
        )

    def health(self) -> CollectorHealth:
        """Probe disk for readiness without parsing any events.

        Walks the tree once to count JSONL files and find the most recent
        mtime, and records unreadable files as warnings so ``tokie doctor``
        can surface them.
        """

        detected = self.session_root.exists() and self.session_root.is_dir()
        if not detected:
            return CollectorHealth(
                name=self.name,
                detected=False,
                ok=False,
                last_scan_at=None,
                last_scan_events=0,
                message=f"no Claude Code session directory at {self.session_root}",
            )

        warnings: list[str] = []
        files: list[Path] = []
        try:
            files = sorted(self.session_root.rglob("*.jsonl"))
        except OSError as exc:
            warnings.append(f"cannot walk {self.session_root}: {type(exc).__name__}")

        latest_mtime: datetime | None = None
        for path in files:
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            except OSError as exc:
                warnings.append(f"unreadable file {path.name}: {type(exc).__name__}")
                continue
            if latest_mtime is None or mtime > latest_mtime:
                latest_mtime = mtime

        return CollectorHealth(
            name=self.name,
            detected=True,
            ok=not warnings,
            last_scan_at=latest_mtime,
            last_scan_events=0,
            message=f"found {len(files)} jsonl file(s) under {self.session_root}",
            warnings=tuple(warnings),
        )


__all__ = ["ClaudeCodeCollector"]
