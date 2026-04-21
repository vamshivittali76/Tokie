"""Collector for Google Gemini API usage via local log tailing.

**Why no HTTP call?**  Google's developer-facing Gemini API does NOT expose a
historical usage or billing endpoint. Paid-tier spend is routed through
Google Cloud Billing (a separate product, gated by GCP IAM and the Billing
Export pipeline), and the free developer tier publishes nothing at all. As a
result, the v0.1 strategy for Gemini is strictly offline: we tail NDJSON/JSONL
files the user already has on disk — either the Gemini CLI's session history
or a user-supplied drop file their own application writes for each
``generateContent`` call.

Two record shapes are supported:

* **Format A** — Gemini CLI session files under ``~/.gemini/history`` (with
  XDG and Vertex-oriented fallbacks). Each line carries ``timestamp``,
  ``model``, ``sessionId``, and a ``usageMetadata`` block.
* **Format B** — a user-supplied NDJSON drop file where each line embeds a
  ``usageMetadata`` block in the exact shape returned by Google's GenAI REST
  ``generateContent`` response. The user is expected to add at minimum a
  top-level ``timestamp`` and ``model`` (or ``modelVersion``) field; we parse
  whatever is present and skip lines without ``usageMetadata``.

No prompt content, ``parts[]``, or ``contents[]`` is ever read or logged —
only model name, timestamp, and token counts leave this module.
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

_ENV_VAR = "TOKIE_GEMINI_LOG"
_SUPPORTED_SUFFIXES: tuple[str, ...] = (".jsonl", ".ndjson")


def _candidate_roots() -> tuple[Path, ...]:
    """Default locations, in detection order.

    Computed at call time rather than module import so tests that mutate
    ``Path.home()`` via ``monkeypatch.setenv("HOME", ...)`` behave predictably.
    """

    home = Path.home()
    return (
        home / ".gemini" / "history",
        home / ".config" / "gemini" / "history",
        home / ".google-gemini" / "sessions",
    )


def _first_existing_default() -> Path | None:
    """Return the first default candidate that exists, or ``None``.

    Used by both ``__init__`` (to pick a session root) and :meth:`detect`.
    Side-effect-free other than the ``os.path`` stat calls the loop makes.
    """

    for candidate in _candidate_roots():
        if candidate.exists():
            return candidate
    return None


def _env_path() -> Path | None:
    """Return the ``TOKIE_GEMINI_LOG`` path if set and on disk, else ``None``.

    The env var can point at either a file or a directory. Non-existent paths
    are ignored so a stale export from a previous shell session doesn't break
    detection or scans.
    """

    raw = os.environ.get(_ENV_VAR)
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.exists():
        return None
    return path


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 ``timestamp`` field into a tz-aware UTC datetime.

    Returns ``None`` on anything unrecognizable so the caller can skip the
    line. We intentionally refuse naive datetimes — mixing zones would poison
    ``since`` filtering downstream.
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


def _non_negative_int(value: Any) -> int:
    """Coerce ``value`` to a non-negative int, defaulting to 0.

    Booleans are rejected explicitly because ``bool`` subclasses ``int`` in
    Python and we don't want ``True`` flowing through as ``1`` token.
    """

    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value >= 0 else 0
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return 0


class GeminiAPICollector(Collector):
    """Parse local Gemini CLI history or user-supplied NDJSON drop files.

    The collector walks three ordered sources and yields one
    :class:`UsageEvent` per line with a ``usageMetadata`` block:

    1. ``session_root`` (defaults to the first existing candidate on disk);
    2. ``extra_paths`` — user-configured files or directories;
    3. ``$TOKIE_GEMINI_LOG`` — env override for one-shot captures.

    Sources are deduplicated by resolved path so overlapping configuration
    never doubles events. If no source is configured, :meth:`scan` is a
    no-op and :meth:`health` reports "no source configured".
    """

    name = "gemini-api"
    default_confidence = Confidence.EXACT

    def __init__(
        self,
        *,
        session_root: Path | None = None,
        extra_paths: tuple[Path, ...] = (),
        account_id: str = "default",
    ) -> None:
        self.session_root: Path | None = (
            session_root if session_root is not None else _first_existing_default()
        )
        self.extra_paths: tuple[Path, ...] = tuple(extra_paths)
        self.account_id: str = account_id

    @classmethod
    def detect(cls) -> bool:
        """Cheap probe: does any default root or env path exist?"""

        if _first_existing_default() is not None:
            return True
        return _env_path() is not None

    def scan(self, since: datetime | None = None) -> AsyncIterator[UsageEvent]:
        """Yield events from every configured source, newest filter applied."""

        return aiterate(self._iter_events(since))

    def _iter_events(self, since: datetime | None) -> Iterator[UsageEvent]:
        seen: set[Path] = set()
        for root in self._configured_roots():
            for path, base in self._iter_files(root):
                key = self._canonical_key(path)
                if key in seen:
                    continue
                seen.add(key)
                yield from self._iter_file(path, base, since)

    def _configured_roots(self) -> list[Path]:
        """Ordered list of all configured source paths (files or dirs)."""

        roots: list[Path] = []
        if self.session_root is not None:
            roots.append(self.session_root)
        roots.extend(self.extra_paths)
        env = _env_path()
        if env is not None:
            roots.append(env)
        return roots

    @staticmethod
    def _canonical_key(path: Path) -> Path:
        """Best-effort canonical path for dedup without raising on reparse points."""

        try:
            return path.resolve()
        except OSError:
            return path

    def _iter_files(self, root: Path) -> Iterator[tuple[Path, Path]]:
        """Yield ``(file, base)`` pairs, where ``base`` anchors source paths.

        ``base`` is the directory we render provenance relative to — the root
        itself when it's a directory, its parent when it's a single file.
        """

        if not root.exists():
            return
        if root.is_file():
            if root.suffix.lower() in _SUPPORTED_SUFFIXES:
                yield root, root.parent
            return
        if root.is_dir():
            matches: list[Path] = []
            for suffix in _SUPPORTED_SUFFIXES:
                matches.extend(root.rglob(f"*{suffix}"))
            for path in sorted(matches):
                yield path, root

    def _iter_file(self, path: Path, base: Path, since: datetime | None) -> Iterator[UsageEvent]:
        try:
            rel = path.relative_to(base)
            rel_source = str(rel)
        except ValueError:
            rel_source = path.name

        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("gemini-api: cannot open %s (%s)", path.name, type(exc).__name__)
            return

        with handle as fp:
            for lineno, raw_line in enumerate(fp, start=1):
                line = raw_line.rstrip("\n").rstrip("\r")
                if not line.strip():
                    continue
                event = self._parse_line(line, path=path, rel=rel_source, lineno=lineno)
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
            logger.warning("gemini-api: malformed json at %s:%d", path.name, lineno)
            return None
        if not isinstance(payload, dict):
            return None

        usage = payload.get("usageMetadata")
        if not isinstance(usage, dict):
            return None

        occurred_at = _parse_timestamp(payload.get("timestamp"))
        if occurred_at is None:
            logger.warning("gemini-api: missing or bad timestamp at %s:%d", path.name, lineno)
            return None

        model_raw = payload.get("model")
        if not isinstance(model_raw, str) or not model_raw:
            fallback = payload.get("modelVersion")
            model_raw = fallback if isinstance(fallback, str) and fallback else ""
        if not model_raw:
            return None

        session_raw = payload.get("sessionId")
        session_id = session_raw if isinstance(session_raw, str) and session_raw else path.stem

        input_tokens = _non_negative_int(usage.get("promptTokenCount"))
        output_tokens = _non_negative_int(usage.get("candidatesTokenCount"))
        cache_read = _non_negative_int(usage.get("cachedContentTokenCount"))
        reasoning = _non_negative_int(usage.get("thoughtsTokenCount"))

        try:
            return self.make_event(
                occurred_at=occurred_at,
                provider="google",
                product="gemini-api",
                account_id=self.account_id,
                model=model_raw,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_write_tokens=0,
                reasoning_tokens=reasoning,
                cost_usd=None,
                raw_hash=compute_raw_hash(line),
                source=f"gemini:{rel}:{lineno}",
                session_id=session_id,
                project=None,
                confidence=Confidence.EXACT,
            )
        except ValueError as exc:
            logger.warning(
                "gemini-api: invalid usage row at %s:%d (%s)",
                path.name,
                lineno,
                type(exc).__name__,
            )
            return None

    def health(self) -> CollectorHealth:
        """Report configured source status without parsing any events.

        Counts total ``*.jsonl`` and ``*.ndjson`` files reachable from all
        configured roots, records unreadable files as warnings, and reports
        "no source configured" when nothing is set.
        """

        roots = self._configured_roots()
        if not roots:
            return CollectorHealth(
                name=self.name,
                detected=False,
                ok=False,
                last_scan_at=None,
                last_scan_events=0,
                message="no source configured",
            )

        warnings: list[str] = []
        files: list[Path] = []
        for root in roots:
            if not root.exists():
                warnings.append(f"missing: {root}")
                continue
            if root.is_file():
                if root.suffix.lower() in _SUPPORTED_SUFFIXES:
                    files.append(root)
                continue
            if root.is_dir():
                try:
                    for suffix in _SUPPORTED_SUFFIXES:
                        files.extend(root.rglob(f"*{suffix}"))
                except OSError as exc:
                    warnings.append(f"cannot walk {root}: {type(exc).__name__}")

        for path in files:
            if not os.access(path, os.R_OK):
                warnings.append(f"unreadable: {path.name}")

        detected = bool(files) or any(r.exists() for r in roots)
        message = f"{len(files)} gemini log file(s) across {len(roots)} source(s)"
        return CollectorHealth(
            name=self.name,
            detected=detected,
            ok=detected and not warnings,
            last_scan_at=None,
            last_scan_events=0,
            message=message,
            warnings=tuple(warnings),
        )


__all__ = ["GeminiAPICollector"]
