"""Tests for the SQLite-backed fire log."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from tokie_cli.alerts.storage import AlertStorage, connect_alerts
from tokie_cli.alerts.thresholds import ThresholdCrossing


def _crossing(
    *,
    plan_id: str = "claude_pro",
    account_id: str = "default",
    window_type: str = "rolling_5h",
    window_starts_at: str = "2026-04-20T10:00:00+00:00",
    threshold_pct: int = 95,
) -> ThresholdCrossing:
    return ThresholdCrossing(
        plan_id=plan_id,
        account_id=account_id,
        display_name=plan_id,
        provider="anthropic",
        product="claude",
        window_type=window_type,
        window_starts_at_iso=window_starts_at,
        window_resets_at_iso="2026-04-20T15:00:00+00:00",
        threshold_pct=threshold_pct,
        pct_used=0.97,
        used=970,
        limit=1000,
        remaining=30,
        channels=("banner",),
    )


def test_record_fires_returns_only_new_crossings(tmp_path: Path) -> None:
    db = tmp_path / "tokie.db"
    conn = connect_alerts(db)
    try:
        storage = AlertStorage(conn)
        new = storage.record_fires([_crossing()])
        assert len(new) == 1
        # Same crossing again — de-dup by primary key.
        dup = storage.record_fires([_crossing()])
        assert dup == []
    finally:
        conn.close()


def test_record_fires_distinguishes_thresholds(tmp_path: Path) -> None:
    db = tmp_path / "tokie.db"
    conn = connect_alerts(db)
    try:
        storage = AlertStorage(conn)
        new = storage.record_fires(
            [
                _crossing(threshold_pct=75),
                _crossing(threshold_pct=95),
                _crossing(threshold_pct=100),
            ]
        )
        assert {c.threshold_pct for c in new} == {75, 95, 100}
    finally:
        conn.close()


def test_record_fires_distinguishes_window_starts(tmp_path: Path) -> None:
    db = tmp_path / "tokie.db"
    conn = connect_alerts(db)
    try:
        storage = AlertStorage(conn)
        storage.record_fires([_crossing(window_starts_at="2026-04-20T10:00:00+00:00")])
        new = storage.record_fires(
            [_crossing(window_starts_at="2026-04-20T15:00:00+00:00")]
        )
        # Same threshold but next window = new fire.
        assert len(new) == 1
    finally:
        conn.close()


def test_record_fires_accepts_empty_window_start_for_none_windows(tmp_path: Path) -> None:
    db = tmp_path / "tokie.db"
    conn = connect_alerts(db)
    try:
        storage = AlertStorage(conn)
        new = storage.record_fires(
            [_crossing(window_starts_at="", window_type="none")]
        )
        assert len(new) == 1
        # Same empty-window crossing should still dedupe.
        dup = storage.record_fires(
            [_crossing(window_starts_at="", window_type="none")]
        )
        assert dup == []
    finally:
        conn.close()


def test_recent_fires_filters_and_orders(tmp_path: Path) -> None:
    db = tmp_path / "tokie.db"
    conn = connect_alerts(db)
    try:
        storage = AlertStorage(conn)
        base = datetime(2026, 4, 20, 10, 0, tzinfo=UTC)
        storage.record_fires([_crossing(threshold_pct=75)], now=base)
        storage.record_fires(
            [_crossing(threshold_pct=95)],
            now=base + timedelta(minutes=5),
        )
        rows = storage.recent_fires()
        assert len(rows) == 2
        assert rows[0].threshold_pct == 95  # newest first
        # Filter by 'since'
        filtered = storage.recent_fires(since=base + timedelta(minutes=3))
        assert [r.threshold_pct for r in filtered] == [95]
    finally:
        conn.close()


def test_clear_wipes_all_fires(tmp_path: Path) -> None:
    db = tmp_path / "tokie.db"
    conn = connect_alerts(db)
    try:
        storage = AlertStorage(conn)
        storage.record_fires(
            [_crossing(threshold_pct=75), _crossing(threshold_pct=95)]
        )
        assert storage.clear() == 2
        assert storage.recent_fires() == []
    finally:
        conn.close()


def test_connect_alerts_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "tokie.db"
    for _ in range(3):
        conn = connect_alerts(db)
        conn.close()
    # No crash = pass.
    assert db.exists()
