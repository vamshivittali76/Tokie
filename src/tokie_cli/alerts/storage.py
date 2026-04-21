"""Persistent de-dup log for threshold fires.

We keep alert state in the same SQLite database as usage events so operators
only have one file to back up / delete, but in a separate table that can be
truncated without destroying raw data. The dedupe key matches
:attr:`ThresholdCrossing.dedupe_key` so writes and reads never disagree.

The table is created lazily on first use (``IF NOT EXISTS``) — we don't bump
:data:`tokie_cli.db.SCHEMA_VERSION` because the alerts feature is additive and
we don't want old v0.2 databases to surface as "needs migration" just because
someone upgraded Tokie.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from tokie_cli.alerts.thresholds import ThresholdCrossing
from tokie_cli.db import connect

_ALERTS_SCHEMA_SQL: Final[str] = """
CREATE TABLE IF NOT EXISTS threshold_fires (
    plan_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    window_type TEXT NOT NULL,
    window_starts_at TEXT NOT NULL,
    threshold_pct INTEGER NOT NULL,
    fired_at TEXT NOT NULL,
    PRIMARY KEY (plan_id, account_id, window_type, window_starts_at, threshold_pct)
);
CREATE INDEX IF NOT EXISTS idx_threshold_fires_time
    ON threshold_fires(fired_at);
"""

_INSERT_FIRE_SQL: Final[str] = (
    "INSERT OR IGNORE INTO threshold_fires "
    "(plan_id, account_id, window_type, window_starts_at, threshold_pct, fired_at) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)


@dataclass(frozen=True)
class FireRecord:
    """A row in the ``threshold_fires`` table."""

    plan_id: str
    account_id: str
    window_type: str
    window_starts_at: str
    threshold_pct: int
    fired_at: datetime


def connect_alerts(path: Path | str) -> sqlite3.Connection:
    """Open the usage DB and ensure the alerts tables exist.

    Idempotent: safe to call on every tick. The returned connection shares the
    same PRAGMA/journal setup as :func:`tokie_cli.db.connect`, so callers can
    pass it straight to :func:`record_fires` or use it for their own queries.
    """

    conn = connect(path)
    conn.executescript(_ALERTS_SCHEMA_SQL)
    conn.commit()
    return conn


class AlertStorage:
    """Thin wrapper around the ``threshold_fires`` table.

    Constructed with an open :class:`sqlite3.Connection` so tests can share
    the in-memory DB with the rest of the suite without re-opening files.
    The engine keeps the connection alive across dispatches; CLI callers pass
    the same connection returned by :func:`connect_alerts`.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def record_fires(
        self,
        crossings: Iterable[ThresholdCrossing],
        *,
        now: datetime | None = None,
    ) -> list[ThresholdCrossing]:
        """Persist every *new* crossing and return the subset that was new.

        Idempotent — a crossing that was already recorded returns
        ``rowcount == 0`` from the ``INSERT OR IGNORE`` and is filtered out.
        That's the mechanism that stops Tokie from pinging you every minute
        once you cross 95% until the window resets and the ``window_starts_at``
        part of the key flips.

        ``now`` is injectable so tests can freeze the clock without monkey-
        patching :mod:`datetime` globally.
        """

        ts = (now or datetime.now(UTC)).isoformat()
        new_fires: list[ThresholdCrossing] = []
        with self._conn:
            for crossing in crossings:
                cur = self._conn.execute(
                    _INSERT_FIRE_SQL,
                    (
                        crossing.plan_id,
                        crossing.account_id,
                        crossing.window_type,
                        crossing.window_starts_at_iso,
                        int(crossing.threshold_pct),
                        ts,
                    ),
                )
                if cur.rowcount == 1:
                    new_fires.append(crossing)
        return new_fires

    def recent_fires(
        self,
        *,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[FireRecord]:
        """Return recent fires newest-first, optionally filtered by ``since``."""

        params: list[object] = []
        sql = "SELECT * FROM threshold_fires"
        if since is not None:
            sql += " WHERE fired_at >= ?"
            params.append(since.isoformat())
        sql += " ORDER BY fired_at DESC LIMIT ?"
        params.append(int(limit))
        cur = self._conn.execute(sql, params)
        out: list[FireRecord] = []
        for row in cur.fetchall():
            out.append(
                FireRecord(
                    plan_id=row["plan_id"],
                    account_id=row["account_id"],
                    window_type=row["window_type"],
                    window_starts_at=row["window_starts_at"],
                    threshold_pct=int(row["threshold_pct"]),
                    fired_at=datetime.fromisoformat(row["fired_at"]),
                )
            )
        return out

    def clear(self) -> int:
        """Wipe every fire record; returns the row count deleted.

        Useful for ``tokie alerts reset`` and for tests that want a blank
        slate between cases. The SQL is a full-table delete, not a drop, so
        the schema remains intact.
        """

        cur = self._conn.execute("DELETE FROM threshold_fires")
        self._conn.commit()
        return int(cur.rowcount or 0)


__all__ = ["AlertStorage", "FireRecord", "connect_alerts"]
