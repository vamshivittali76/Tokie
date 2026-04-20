"""Tests for :mod:`tokie_cli.db`.

Covers migration idempotency, insert/dedup semantics, filter composition,
ordering, connection defaults, and POSIX file-mode hardening.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tokie_cli.db import (
    SCHEMA_VERSION,
    InsertStats,
    connect,
    insert_event,
    insert_events,
    migrate,
    query_events,
)
from tokie_cli.schema import Confidence, UsageEvent


def make_event(**overrides: object) -> UsageEvent:
    defaults: dict[str, object] = {
        "id": "evt-1",
        "collected_at": datetime(2026, 4, 20, tzinfo=UTC),
        "occurred_at": datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
        "provider": "anthropic",
        "product": "claude-code",
        "account_id": "hash-me",
        "model": "claude-opus-4-7",
        "input_tokens": 100,
        "output_tokens": 50,
        "confidence": Confidence.EXACT,
        "source": "jsonl:~/.claude/projects/foo.jsonl",
        "raw_hash": "a" * 64,
    }
    defaults.update(overrides)
    return UsageEvent(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def test_migrate_creates_schema_and_version_row() -> None:
    c = connect(":memory:")
    migrate(c)

    tables = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"schema_version", "usage_events"} <= tables

    versions = [row[0] for row in c.execute("SELECT version FROM schema_version")]
    assert versions == [SCHEMA_VERSION]


def test_migrate_is_idempotent() -> None:
    c = connect(":memory:")
    migrate(c)
    migrate(c)

    count = c.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    assert count == 1


def test_insert_event_roundtrip(conn: sqlite3.Connection) -> None:
    evt = make_event()
    assert insert_event(conn, evt) is True

    got = query_events(conn)
    assert len(got) == 1
    assert got[0] == evt


def test_insert_event_dedups_on_raw_hash(conn: sqlite3.Connection) -> None:
    evt = make_event()
    assert insert_event(conn, evt) is True

    collision = make_event(id="evt-2")
    assert insert_event(conn, collision) is False

    assert len(query_events(conn)) == 1


def test_insert_events_bulk_reports_stats(conn: sqlite3.Connection) -> None:
    e1 = make_event(id="evt-1", raw_hash="a" * 64)
    e2 = make_event(id="evt-2", raw_hash="b" * 64)
    e3 = make_event(id="evt-3", raw_hash="a" * 64)

    stats = insert_events(conn, [e1, e2, e3])

    assert stats == InsertStats(inserted=2, deduped=1)
    assert len(query_events(conn)) == 2


def test_query_events_filters_by_provider(conn: sqlite3.Connection) -> None:
    anth = make_event(id="a", raw_hash="a" * 64, provider="anthropic")
    oai = make_event(
        id="b",
        raw_hash="b" * 64,
        provider="openai",
        source="har:openai.har",
    )
    insert_events(conn, [anth, oai])

    got = query_events(conn, provider="openai")
    assert [e.id for e in got] == ["b"]


def test_query_events_filters_by_time_range(conn: sqlite3.Connection) -> None:
    base = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    before = make_event(id="a", raw_hash="a" * 64, occurred_at=base - timedelta(hours=1))
    at_since = make_event(id="b", raw_hash="b" * 64, occurred_at=base)
    inside = make_event(id="c", raw_hash="c" * 64, occurred_at=base + timedelta(hours=1))
    at_until = make_event(id="d", raw_hash="d" * 64, occurred_at=base + timedelta(hours=2))
    insert_events(conn, [before, at_since, inside, at_until])

    got = query_events(conn, since=base, until=base + timedelta(hours=2))

    assert [e.id for e in got] == ["b", "c"]


def test_query_events_orders_by_occurred_at_asc(conn: sqlite3.Connection) -> None:
    base = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    late = make_event(id="a", raw_hash="a" * 64, occurred_at=base + timedelta(hours=2))
    early = make_event(id="b", raw_hash="b" * 64, occurred_at=base)
    middle = make_event(id="c", raw_hash="c" * 64, occurred_at=base + timedelta(hours=1))
    insert_events(conn, [late, early, middle])

    got = query_events(conn)
    assert [e.id for e in got] == ["b", "c", "a"]


def test_connect_row_factory_is_row() -> None:
    c = connect(":memory:")
    assert c.row_factory is sqlite3.Row


def test_connect_enables_foreign_keys() -> None:
    c = connect(":memory:")
    result = c.execute("PRAGMA foreign_keys").fetchone()
    assert result[0] == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only file permissions")
def test_connect_creates_file_with_mode_0600_on_posix() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "tokie.db"
        connect(db_path)
        mode = db_path.stat().st_mode & 0o777
        assert mode == 0o600
