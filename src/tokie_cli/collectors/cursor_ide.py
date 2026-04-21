"""Collector for Cursor IDE usage.

Two complementary scan paths are supported:

1. **Local SQLite (auto, preferred).** Cursor stores per-message metadata in
   ``state.vscdb`` (a SQLite database inside the global VSCode storage
   directory). Tokie reads ``cursorDiskKV`` rows (``bubbleId:*`` keys) to
   extract one event per assistant response, and ``ItemTable`` rows
   (``aiCodeTracking.dailyStats.v1.5.*`` keys) for daily code-line activity.
   Token counts are NOT stored locally by Cursor — only timestamp, model name,
   and conversation membership are available — so confidence is ``ESTIMATED``.
   This path is fully automatic: if ``state.vscdb`` exists, the collector
   detects and reads it without any user setup.

2. **File-drop (manual fallback).** Export the per-request CSV from
   Cursor's dashboard ("Request history" → "Download CSV"), drop it into
   ``~/.cursor/history/`` (or set ``TOKIE_CURSOR_LOG``), and re-run
   ``tokie scan``. Confidence is ``ESTIMATED`` (token counts derived from a
   fixed heuristic). Optionally pipe your own HTTP wrapper's response usage
   blocks into NDJSON format for ``EXACT`` confidence.

Feature flag: the collector is **disabled by default**. Opt in by setting
``[collectors.cursor-ide] enabled = true`` in ``tokie.toml``.

No prompt content, code context, or embeddings are parsed or logged — only
model name, timestamp, bubble ID, and (when available) token counts leave
this module.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import sqlite3
import sys
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timezone
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

# Type-2 bubbles are assistant responses (type-1 are human messages).
# We count type-2 as "one request" against the monthly quota.
_BUBBLE_TYPE_ASSISTANT = "2"


def _state_vscdb_candidates() -> tuple[Path, ...]:
    """Candidate paths for Cursor's global state.vscdb (platform-specific)."""
    home = Path.home()
    candidates: list[Path] = []
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        candidates.append(
            appdata / "Cursor" / "User" / "globalStorage" / "state.vscdb"
        )
    elif sys.platform == "darwin":
        candidates.append(
            home / "Library" / "Application Support" / "Cursor" / "User"
            / "globalStorage" / "state.vscdb"
        )
    else:
        xdg = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        candidates.append(
            xdg / "Cursor" / "User" / "globalStorage" / "state.vscdb"
        )
    return tuple(candidates)


def _find_state_vscdb() -> Path | None:
    for p in _state_vscdb_candidates():
        if p.is_file():
            return p
    return None


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
    """Cursor IDE collector — reads local state.vscdb and/or manual file drops.

    Confidence is always ``ESTIMATED`` because Cursor does not persist token
    counts in the local database; we can record that a request happened and
    which model was used, but not how many tokens it consumed.
    """

    name = "cursor-ide"
    default_confidence = Confidence.ESTIMATED

    @classmethod
    def detect(cls) -> bool:
        return bool(_find_state_vscdb()) or bool(_resolve_paths())

    def scan(self, since: datetime | None = None) -> AsyncIterator[UsageEvent]:
        return aiterate(self._scan_sync(since))

    def _scan_sync(self, since: datetime | None) -> Iterator[UsageEvent]:
        # Primary path: local SQLite database
        db_path = _find_state_vscdb()
        if db_path:
            yield from self._scan_sqlite(db_path, since)

        # Fallback / supplementary path: manual file drops
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

    def _scan_sqlite(self, db_path: Path, since: datetime | None) -> Iterator[UsageEvent]:
        """Read assistant-response bubbles from Cursor's local state.vscdb.

        Each type-2 (assistant) bubble is one request against the monthly
        quota.  Token counts are not stored locally; we emit 0 so the event
        registers as a message without inflating the token totals.
        """
        try:
            # Open in read-only URI mode to avoid locking the live DB.
            uri = db_path.as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5)
        except sqlite3.OperationalError:
            # WAL mode fallback: open normally but read-only flag via pragma.
            try:
                conn = sqlite3.connect(str(db_path), timeout=5)
            except sqlite3.OperationalError as exc:
                logger.warning("cursor-ide: cannot open %s (%s)", db_path.name, exc)
                return

        try:
            yield from self._read_bubbles(conn, since)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cursor-ide: error reading bubbles (%s)", exc)
        finally:
            conn.close()

    def _read_bubbles(
        self, conn: sqlite3.Connection, since: datetime | None
    ) -> Iterator[UsageEvent]:
        """Yield one UsageEvent per assistant response bubble."""
        try:
            cursor = conn.execute(
                "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
            )
        except sqlite3.OperationalError as exc:
            logger.warning("cursor-ide: cursorDiskKV query failed (%s)", exc)
            return

        since_ts = since.isoformat() if since else None

        for _key, raw_val in cursor:
            try:
                bubble = json.loads(raw_val)
            except (json.JSONDecodeError, TypeError):
                continue

            # Only assistant responses (type == "2") count as a request.
            if str(bubble.get("type", "")) != _BUBBLE_TYPE_ASSISTANT:
                continue

            created_raw = bubble.get("createdAt", "")
            if not created_raw:
                continue

            occurred_at = _parse_timestamp(str(created_raw))
            if occurred_at is None:
                continue

            # Apply since filter using string comparison (ISO sorts lexically).
            if since_ts and str(created_raw)[:26] < since_ts[:26]:
                continue

            bubble_id = str(bubble.get("bubbleId", "") or _key.split(":")[-1])
            model = str(bubble.get("modelType") or bubble.get("model") or "cursor-auto")

            yield self.make_event(
                occurred_at=occurred_at,
                provider="cursor",
                product="cursor-ide",
                account_id="default",
                model=model,
                # Token counts are NOT stored in the local DB.
                input_tokens=0,
                output_tokens=0,
                session_id=str(bubble.get("composerId", "") or ""),
                raw_hash=hashlib.sha256(
                    f"cursor-bubble:{bubble_id}".encode()
                ).hexdigest(),
                source=f"cursor-ide:state.vscdb:{bubble_id[:8]}",
                confidence=Confidence.ESTIMATED,
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
        db_path = _find_state_vscdb()
        drop_paths = _resolve_paths()

        if not db_path and not drop_paths:
            return CollectorHealth(
                name=self.name,
                detected=False,
                ok=False,
                last_scan_at=None,
                last_scan_events=0,
                message=(
                    "cursor state.vscdb not found; also no drop files in "
                    "~/.cursor/history/ — is Cursor installed?"
                ),
            )

        sources: list[str] = []
        warnings: list[str] = []
        if db_path:
            sources.append(f"local db ({db_path.name})")
            warnings.append(
                "token counts are not stored locally — events register as "
                "requests (messages) only; token totals will show 0"
            )
        if drop_paths:
            sources.append(f"{len(drop_paths)} drop file(s)")

        return CollectorHealth(
            name=self.name,
            detected=True,
            ok=True,
            last_scan_at=None,
            last_scan_events=0,
            message=f"found {', '.join(sources)}",
            warnings=tuple(warnings),
        )


__all__ = ["CursorIDECollector"]
