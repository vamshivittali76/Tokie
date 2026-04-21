"""Collector for Cursor IDE usage drops (unofficial, feature-flagged).

Cursor's team does NOT expose a public, per-user historical usage endpoint.
Their web dashboard at ``https://cursor.com/dashboard`` is the only
vendor-authoritative surface, and it uses short-lived session cookies that
rotate without notice. Reverse-engineering that surface breaks every time
Cursor ships a new auth flow, which is why Tokie ships Cursor support
strictly as a drop-ingest collector with an explicit ``ESTIMATED``
confidence tier.

Users populate this collector one of two ways:

1. **Manual export (recommended).** Export the per-request CSV from
   Cursor's dashboard ("Request history" → "Download CSV"), drop it into
   ``~/.cursor/history/`` (or set ``TOKIE_CURSOR_LOG`` to a file/dir), and
   re-run ``tokie scan``. Tokie parses request rows, uses Cursor's
   ``model`` column as the canonical model name, and applies a fixed-ratio
   token estimator for each request (since the CSV omits token counts).
2. **NDJSON wrapper (advanced).** Pipe your own HTTP wrapper's response
   usage blocks into NDJSON in the same shape as ``copilot_cli``/``api_gemini``.
   Tokie will prefer NDJSON rows (``EXACT`` confidence) over CSV rows
   (``ESTIMATED`` confidence) when both contain the same request id.

Feature flag: the collector is **disabled by default**. Opt in by setting
``[collectors.cursor-ide] enabled = true`` in ``tokie.toml`` or by running
``tokie doctor --enable cursor-ide``. ``detect()`` still returns True when
drops exist so users get the nudge to enable the collector.

No prompt content, code context, or embeddings are parsed or logged — only
model name, timestamp, request id, and (when available) token counts leave
this module. Cost calculation is delegated to the aggregation layer using
the ``plans.yaml`` cost table.
"""

from __future__ import annotations

import csv
import hashlib
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

_ENV_VAR = "TOKIE_CURSOR_LOG"
_CSV_SUFFIXES: tuple[str, ...] = (".csv",)
_JSON_SUFFIXES: tuple[str, ...] = (".jsonl", ".ndjson")
_ALL_SUFFIXES: tuple[str, ...] = _CSV_SUFFIXES + _JSON_SUFFIXES

_CSV_TS_CANDIDATES: tuple[str, ...] = ("timestamp", "date", "created_at", "time")
_CSV_MODEL_CANDIDATES: tuple[str, ...] = ("model", "model_name")
_CSV_REQUEST_CANDIDATES: tuple[str, ...] = ("request_id", "id", "requestId")

_ESTIMATED_INPUT_TOKENS_PER_REQUEST = 4000
_ESTIMATED_OUTPUT_TOKENS_PER_REQUEST = 600


def _candidate_roots() -> tuple[Path, ...]:
    home = Path.home()
    return (
        home / ".cursor" / "history",
        home / ".config" / "cursor" / "history",
        home / "AppData" / "Roaming" / "Cursor" / "history",
    )


def _resolve_paths() -> list[Path]:
    override = os.environ.get(_ENV_VAR)
    paths: list[Path] = []
    if override:
        root = Path(override)
        if root.is_file() and root.suffix.lower() in _ALL_SUFFIXES:
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
        if p.is_file() and p.suffix.lower() in _ALL_SUFFIXES:
            yield p


def _parse_timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    candidate = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        pass
    # CSV exports sometimes use plain `YYYY-MM-DD HH:MM:SS`.
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _first_present(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


class CursorIDECollector(Collector):
    """Cursor IDE drop-ingest collector.

    Confidence is ``ESTIMATED`` for CSV rows (token counts are derived from
    a fixed heuristic since the vendor CSV omits them) and ``EXACT`` for
    user-supplied NDJSON rows that carry a real ``usage`` block.
    """

    name = "cursor-ide"
    default_confidence = Confidence.ESTIMATED

    @classmethod
    def detect(cls) -> bool:
        return bool(_resolve_paths())

    def scan(self, since: datetime | None = None) -> AsyncIterator[UsageEvent]:
        return aiterate(self._scan_sync(since))

    def _scan_sync(self, since: datetime | None) -> Iterator[UsageEvent]:
        for path in _resolve_paths():
            try:
                if path.suffix.lower() in _CSV_SUFFIXES:
                    yield from self._scan_csv(path, since)
                else:
                    yield from self._scan_jsonl(path, since)
            except OSError as exc:
                logger.warning(
                    "cursor-ide: cannot read %s (%s)", path.name, type(exc).__name__
                )

    def _scan_csv(self, path: Path, since: datetime | None) -> Iterator[UsageEvent]:
        with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                return
            for row_no, row in enumerate(reader, 1):
                occurred_at = _parse_timestamp(_first_present(row, _CSV_TS_CANDIDATES))
                if occurred_at is None:
                    continue
                if since is not None and occurred_at < since:
                    continue
                model = _first_present(row, _CSV_MODEL_CANDIDATES) or "cursor-unknown"
                request_id = _first_present(row, _CSV_REQUEST_CANDIDATES)
                # Hash stays deterministic across re-exports of the same row.
                hash_seed = f"{path.name}:{request_id or row_no}:{occurred_at.isoformat()}:{model}"
                yield self.make_event(
                    occurred_at=occurred_at,
                    provider="cursor",
                    product="cursor-ide",
                    account_id=str(row.get("account_id", "default")),
                    model=str(model),
                    input_tokens=_ESTIMATED_INPUT_TOKENS_PER_REQUEST,
                    output_tokens=_ESTIMATED_OUTPUT_TOKENS_PER_REQUEST,
                    session_id=(
                        str(row["session_id"])
                        if row.get("session_id")
                        else None
                    ),
                    project=(
                        str(row["project"])
                        if row.get("project")
                        else None
                    ),
                    raw_hash=hashlib.sha256(hash_seed.encode("utf-8")).hexdigest(),
                    source=f"cursor-ide:{path.name}:{row_no}",
                    confidence=Confidence.ESTIMATED,
                )

    def _scan_jsonl(self, path: Path, since: datetime | None) -> Iterator[UsageEvent]:
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
                usage = record.get("usage")
                if not isinstance(usage, dict):
                    continue
                input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
                output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
                if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
                    continue
                occurred_at = _parse_timestamp(record.get("timestamp"))
                if occurred_at is None:
                    continue
                if since is not None and occurred_at < since:
                    continue
                yield self.make_event(
                    occurred_at=occurred_at,
                    provider="cursor",
                    product="cursor-ide",
                    account_id=str(record.get("account_id", "default")),
                    model=str(record.get("model", "cursor-unknown")),
                    input_tokens=int(input_tokens),
                    output_tokens=int(output_tokens),
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
                    raw_hash=compute_raw_hash(raw),
                    source=f"cursor-ide:{path.name}:{line_no}",
                    confidence=Confidence.EXACT,
                )

    def health(self) -> CollectorHealth:
        paths = _resolve_paths()
        if not paths:
            return CollectorHealth(
                name=self.name,
                detected=False,
                ok=False,
                last_scan_at=None,
                last_scan_events=0,
                message=(
                    "cursor-ide is opt-in: export CSV from cursor.com/dashboard "
                    "into ~/.cursor/history/ or set TOKIE_CURSOR_LOG"
                ),
            )
        return CollectorHealth(
            name=self.name,
            detected=True,
            ok=True,
            last_scan_at=None,
            last_scan_events=0,
            message=f"found {len(paths)} drop file(s)",
            warnings=(
                "cursor has no public usage API — CSV rows are ESTIMATED "
                "confidence; NDJSON rows with usage blocks are EXACT",
            ),
        )


__all__ = ["CursorIDECollector"]
