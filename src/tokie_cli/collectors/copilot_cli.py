"""Collector for GitHub Copilot CLI local session logs.

The Copilot CLI (``gh copilot``, ``github-copilot``) does not expose a
historical usage endpoint — GitHub's Copilot billing dashboard is the
only vendor surface, and it has no public API for individuals. Teams +
Enterprise tiers get a reporting REST API, but that's out of scope for
Tokie's solo-developer target.

So v0.2's strategy matches ``api_gemini``: tail a local NDJSON/JSONL log
that the user either (a) lets the Copilot CLI write via its verbose
telemetry flag, or (b) produces themselves from a thin wrapper around
``gh copilot suggest``/``gh copilot explain``. Whatever the origin, each
line MUST include at minimum::

    {"timestamp": "2026-04-20T12:34:56Z",
     "model":     "gpt-4o-copilot",
     "usage":     {"prompt_tokens": 120, "completion_tokens": 60}}

Optional fields: ``session_id``, ``project``, ``cost_usd``, ``account_id``.
Lines without a ``usage`` block are silently skipped.

No prompt content is parsed or logged — only model, timestamp, token
counts, and cost leave this module.
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

_ENV_VAR = "TOKIE_COPILOT_LOG"
_SUPPORTED_SUFFIXES: tuple[str, ...] = (".jsonl", ".ndjson")


def _candidate_roots() -> tuple[Path, ...]:
    """Default locations checked when ``TOKIE_COPILOT_LOG`` is unset.

    Computed at call time so tests that override ``HOME`` via ``monkeypatch``
    behave predictably.
    """

    home = Path.home()
    return (
        home / ".config" / "github-copilot" / "history",
        home / ".copilot" / "history",
        home / ".cache" / "github-copilot" / "history",
        home / "AppData" / "Roaming" / "GitHub Copilot" / "history",
    )


def _resolve_paths() -> list[Path]:
    """Expand the env override (file or directory) plus any default root hits."""

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
        value = raw.replace("Z", "+00:00")
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _extract_usage(record: dict[str, Any]) -> tuple[int, int] | None:
    usage = record.get("usage")
    if not isinstance(usage, dict):
        return None
    # Copilot has historically emitted both `prompt_tokens` (chat-completions)
    # and `input_tokens` (responses API) shapes depending on the model.
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    return input_tokens, output_tokens


class CopilotCLICollector(Collector):
    """Log-tail collector for local Copilot CLI NDJSON history."""

    name = "copilot-cli"
    default_confidence = Confidence.EXACT

    @classmethod
    def detect(cls) -> bool:
        return bool(_resolve_paths())

    def scan(self, since: datetime | None = None) -> AsyncIterator[UsageEvent]:
        return aiterate(self._scan_sync(since))

    def _scan_sync(self, since: datetime | None) -> Iterator[UsageEvent]:
        for path in _resolve_paths():
            try:
                yield from self._scan_file(path, since)
            except OSError as exc:
                logger.warning("copilot-cli: cannot read %s (%s)", path.name, type(exc).__name__)

    def _scan_file(self, path: Path, since: datetime | None) -> Iterator[UsageEvent]:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line_no, raw in enumerate(fh, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    logger.debug("copilot-cli: skipping malformed line %s:%d", path.name, line_no)
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

                raw_hash = compute_raw_hash(raw)
                yield self.make_event(
                    occurred_at=occurred_at,
                    provider="github",
                    product="copilot-cli",
                    account_id=str(record.get("account_id", "default")),
                    model=str(record.get("model", "copilot-unknown")),
                    input_tokens=int(usage[0]),
                    output_tokens=int(usage[1]),
                    session_id=(
                        str(record["session_id"])
                        if isinstance(record.get("session_id"), str)
                        else None
                    ),
                    project=(
                        str(record["project"])
                        if isinstance(record.get("project"), str)
                        else None
                    ),
                    cost_usd=(
                        float(record["cost_usd"])
                        if isinstance(record.get("cost_usd"), int | float)
                        else None
                    ),
                    raw_hash=raw_hash,
                    source=f"copilot-cli:{path.name}:{line_no}",
                )

    def health(self) -> CollectorHealth:
        paths = _resolve_paths()
        detected = bool(paths)
        if not detected:
            return CollectorHealth(
                name=self.name,
                detected=False,
                ok=False,
                last_scan_at=None,
                last_scan_events=0,
                message=(
                    "no copilot history found; drop JSONL into "
                    "~/.config/github-copilot/history/ or set TOKIE_COPILOT_LOG"
                ),
            )
        return CollectorHealth(
            name=self.name,
            detected=True,
            ok=True,
            last_scan_at=None,
            last_scan_events=0,
            message=f"found {len(paths)} log file(s)",
        )


__all__ = ["CopilotCLICollector"]
