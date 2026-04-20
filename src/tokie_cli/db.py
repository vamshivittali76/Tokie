"""SQLite persistence layer for Tokie usage events.

Single-file, stdlib-only. Every public function documents whether it commits
or mutates disk state. The schema DDL lives in this module and is versioned
via the ``schema_version`` table so future migrations can detect the jump.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from tokie_cli.schema import Confidence, UsageEvent

SCHEMA_VERSION: Final[int] = 1

_MEMORY_PATH: Final[str] = ":memory:"

_SCHEMA_SQL: Final[str] = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_events (
    id TEXT PRIMARY KEY,
    collected_at TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    product TEXT NOT NULL,
    account_id TEXT NOT NULL,
    session_id TEXT,
    project TEXT,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL,
    confidence TEXT NOT NULL,
    source TEXT NOT NULL,
    raw_hash TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_events_occurred_at ON usage_events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_acct ON usage_events(provider, product, account_id);
"""

_INSERT_SQL: Final[str] = (
    "INSERT OR IGNORE INTO usage_events ("
    "id, collected_at, occurred_at, provider, product, account_id, "
    "session_id, project, model, input_tokens, output_tokens, "
    "cache_read_tokens, cache_write_tokens, reasoning_tokens, cost_usd, "
    "confidence, source, raw_hash"
    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


@dataclass(frozen=True)
class InsertStats:
    """Result counts from a batch insert."""

    inserted: int
    deduped: int


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a SQLite connection configured with Tokie's defaults.

    Side effects: may create the database file, tighten its permissions to
    0600 on POSIX, and enable WAL journaling for on-disk databases.
    """

    path_str = str(path)
    is_memory = path_str == _MEMORY_PATH

    newly_created = False
    if not is_memory:
        newly_created = not Path(path_str).exists()

    conn = sqlite3.connect(path_str)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not is_memory:
        conn.execute("PRAGMA journal_mode = WAL")

    if newly_created and not is_memory and hasattr(os, "chmod") and os.name == "posix":
        os.chmod(path_str, 0o600)

    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Create schema v1 if missing and record the current version.

    Side effects: runs DDL and commits a ``schema_version`` row when absent.
    """

    conn.executescript(_SCHEMA_SQL)
    conn.execute(
        "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION, datetime.now(UTC).isoformat()),
    )
    conn.commit()


def _event_to_params(event: UsageEvent) -> tuple[object, ...]:
    return (
        event.id,
        event.collected_at.isoformat(),
        event.occurred_at.isoformat(),
        event.provider,
        event.product,
        event.account_id,
        event.session_id,
        event.project,
        event.model,
        event.input_tokens,
        event.output_tokens,
        event.cache_read_tokens,
        event.cache_write_tokens,
        event.reasoning_tokens,
        event.cost_usd,
        event.confidence.value,
        event.source,
        event.raw_hash,
    )


def insert_event(conn: sqlite3.Connection, event: UsageEvent) -> bool:
    """Insert one event and return ``False`` if ``raw_hash`` already exists.

    Side effects: commits the transaction.
    """

    cur = conn.execute(_INSERT_SQL, _event_to_params(event))
    conn.commit()
    return cur.rowcount == 1


def insert_events(conn: sqlite3.Connection, events: Iterable[UsageEvent]) -> InsertStats:
    """Insert many events in one transaction and report insert/dedup counts.

    Side effects: commits the transaction via the ``with conn`` context.
    """

    inserted = 0
    deduped = 0
    with conn:
        for event in events:
            cur = conn.execute(_INSERT_SQL, _event_to_params(event))
            if cur.rowcount == 1:
                inserted += 1
            else:
                deduped += 1
    return InsertStats(inserted=inserted, deduped=deduped)


def _row_to_event(row: sqlite3.Row) -> UsageEvent:
    return UsageEvent(
        id=row["id"],
        collected_at=datetime.fromisoformat(row["collected_at"]),
        occurred_at=datetime.fromisoformat(row["occurred_at"]),
        provider=row["provider"],
        product=row["product"],
        account_id=row["account_id"],
        session_id=row["session_id"],
        project=row["project"],
        model=row["model"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cache_read_tokens=row["cache_read_tokens"],
        cache_write_tokens=row["cache_write_tokens"],
        reasoning_tokens=row["reasoning_tokens"],
        cost_usd=row["cost_usd"],
        confidence=Confidence(row["confidence"]),
        source=row["source"],
        raw_hash=row["raw_hash"],
    )


def query_events(
    conn: sqlite3.Connection,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    provider: str | None = None,
    product: str | None = None,
    account_id: str | None = None,
) -> list[UsageEvent]:
    """Return events matching filters, ordered by ``occurred_at`` ascending.

    ``since`` is inclusive, ``until`` is exclusive. All filters compose with
    AND; unspecified filters are skipped. Side effects: none (read-only).
    """

    clauses: list[str] = []
    params: list[object] = []
    if since is not None:
        clauses.append("occurred_at >= ?")
        params.append(since.isoformat())
    if until is not None:
        clauses.append("occurred_at < ?")
        params.append(until.isoformat())
    if provider is not None:
        clauses.append("provider = ?")
        params.append(provider)
    if product is not None:
        clauses.append("product = ?")
        params.append(product)
    if account_id is not None:
        clauses.append("account_id = ?")
        params.append(account_id)

    sql = "SELECT * FROM usage_events"
    if clauses:
        sql = sql + " WHERE " + " AND ".join(clauses)
    sql = sql + " ORDER BY occurred_at ASC"

    cur = conn.execute(sql, params)
    return [_row_to_event(row) for row in cur.fetchall()]


__all__ = [
    "SCHEMA_VERSION",
    "InsertStats",
    "connect",
    "insert_event",
    "insert_events",
    "migrate",
    "query_events",
]
