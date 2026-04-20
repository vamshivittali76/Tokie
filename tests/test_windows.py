"""Tests for :mod:`tokie_cli.windows` — quota window math.

Every branch in ``window_bounds``, ``next_reset_at``, ``aggregate_events``,
and ``capacity`` is exercised here. If you touch the windows module and
these tests still pass, the downstream dashboard math should still be
trustworthy.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from tokie_cli.schema import Confidence, LimitWindow, UsageEvent, WindowType
from tokie_cli.windows import (
    Capacity,
    UsageAggregate,
    aggregate_events,
    capacity,
    next_reset_at,
    window_bounds,
)


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


# ---------------------------------------------------------------------------
# window_bounds
# ---------------------------------------------------------------------------


def test_window_bounds_rolling_5h() -> None:
    session_start = datetime(2026, 4, 20, 9, 0, tzinfo=UTC)
    now = datetime(2026, 4, 20, 10, 30, tzinfo=UTC)
    bounds = window_bounds(WindowType.ROLLING_5H, session_start, now)
    assert bounds == (session_start, datetime(2026, 4, 20, 14, 0, tzinfo=UTC))


def test_window_bounds_daily_ignores_session_start() -> None:
    now = datetime(2026, 4, 20, 15, 30, tzinfo=UTC)
    a = window_bounds(WindowType.DAILY, datetime(2020, 1, 1, tzinfo=UTC), now)
    b = window_bounds(WindowType.DAILY, datetime(2026, 4, 20, 23, 59, tzinfo=UTC), now)
    assert a == b
    assert a == (
        datetime(2026, 4, 20, 0, 0, tzinfo=UTC),
        datetime(2026, 4, 21, 0, 0, tzinfo=UTC),
    )


def test_window_bounds_daily_respects_utc_from_non_utc_input() -> None:
    eastern = timezone(timedelta(hours=-4))
    now_local = datetime(2026, 4, 20, 22, 0, tzinfo=eastern)
    bounds = window_bounds(WindowType.DAILY, now_local, now_local)
    assert bounds == (
        datetime(2026, 4, 21, 0, 0, tzinfo=UTC),
        datetime(2026, 4, 22, 0, 0, tzinfo=UTC),
    )


def test_window_bounds_weekly() -> None:
    session_start = datetime(2026, 4, 15, 9, 30, tzinfo=UTC)
    now = datetime(2026, 4, 17, tzinfo=UTC)
    bounds = window_bounds(WindowType.WEEKLY, session_start, now)
    assert bounds == (session_start, datetime(2026, 4, 22, 9, 30, tzinfo=UTC))


def test_window_bounds_monthly_december_wraps_to_next_year() -> None:
    now = datetime(2026, 12, 15, 8, 0, tzinfo=UTC)
    bounds = window_bounds(WindowType.MONTHLY, datetime(2020, 1, 1, tzinfo=UTC), now)
    assert bounds == (
        datetime(2026, 12, 1, 0, 0, tzinfo=UTC),
        datetime(2027, 1, 1, 0, 0, tzinfo=UTC),
    )


def test_window_bounds_monthly_mid_month() -> None:
    now = datetime(2026, 4, 20, 14, 30, tzinfo=UTC)
    bounds = window_bounds(WindowType.MONTHLY, datetime(2020, 1, 1, tzinfo=UTC), now)
    assert bounds == (
        datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
    )


def test_window_bounds_none_returns_none() -> None:
    assert (
        window_bounds(
            WindowType.NONE,
            datetime(2026, 4, 20, tzinfo=UTC),
            datetime(2026, 4, 20, tzinfo=UTC),
        )
        is None
    )


def test_window_bounds_rejects_naive_datetime() -> None:
    naive = datetime(2026, 4, 20, 12, 0)
    aware = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="session_start"):
        window_bounds(WindowType.ROLLING_5H, naive, aware)
    with pytest.raises(ValueError, match="now"):
        window_bounds(WindowType.DAILY, aware, naive)


# ---------------------------------------------------------------------------
# next_reset_at
# ---------------------------------------------------------------------------


def test_next_reset_at_each_window_type() -> None:
    session_start = datetime(2026, 4, 20, 9, 0, tzinfo=UTC)
    now = datetime(2026, 4, 20, 10, 0, tzinfo=UTC)

    assert next_reset_at(WindowType.ROLLING_5H, session_start, now) == datetime(
        2026, 4, 20, 14, 0, tzinfo=UTC
    )
    assert next_reset_at(WindowType.DAILY, session_start, now) == datetime(
        2026, 4, 21, 0, 0, tzinfo=UTC
    )
    assert next_reset_at(WindowType.WEEKLY, session_start, now) == datetime(
        2026, 4, 27, 9, 0, tzinfo=UTC
    )
    assert next_reset_at(WindowType.MONTHLY, session_start, now) == datetime(
        2026, 5, 1, 0, 0, tzinfo=UTC
    )


def test_next_reset_at_none_is_none() -> None:
    assert (
        next_reset_at(
            WindowType.NONE,
            datetime(2026, 4, 20, tzinfo=UTC),
            datetime(2026, 4, 20, tzinfo=UTC),
        )
        is None
    )


# ---------------------------------------------------------------------------
# aggregate_events
# ---------------------------------------------------------------------------


def test_aggregate_empty_events_is_zero() -> None:
    agg = aggregate_events(
        [],
        start=datetime(2026, 4, 20, tzinfo=UTC),
        end=datetime(2026, 4, 21, tzinfo=UTC),
    )
    assert agg == UsageAggregate(
        total_input_tokens=0,
        total_output_tokens=0,
        total_cache_read_tokens=0,
        total_cache_write_tokens=0,
        total_reasoning_tokens=0,
        total_cost_usd=0.0,
        total_messages=0,
    )
    assert agg.total_tokens == 0


def test_aggregate_sums_all_token_counters() -> None:
    start = datetime(2026, 4, 20, 0, 0, tzinfo=UTC)
    end = datetime(2026, 4, 21, 0, 0, tzinfo=UTC)
    events = [
        make_event(
            id="a",
            occurred_at=datetime(2026, 4, 20, 10, 0, tzinfo=UTC),
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=10,
            cache_write_tokens=5,
            reasoning_tokens=3,
            cost_usd=0.25,
        ),
        make_event(
            id="b",
            occurred_at=datetime(2026, 4, 20, 11, 0, tzinfo=UTC),
            input_tokens=200,
            output_tokens=75,
            cache_read_tokens=20,
            cache_write_tokens=0,
            reasoning_tokens=12,
            cost_usd=0.50,
            raw_hash="b" * 64,
        ),
    ]
    agg = aggregate_events(events, start=start, end=end)
    assert agg.total_input_tokens == 300
    assert agg.total_output_tokens == 125
    assert agg.total_cache_read_tokens == 30
    assert agg.total_cache_write_tokens == 5
    assert agg.total_reasoning_tokens == 15
    assert agg.total_cost_usd == pytest.approx(0.75)
    assert agg.total_messages == 2
    assert agg.total_tokens == 475


def test_aggregate_cost_ignores_none() -> None:
    start = datetime(2026, 4, 20, 0, 0, tzinfo=UTC)
    end = datetime(2026, 4, 21, 0, 0, tzinfo=UTC)
    events = [
        make_event(
            id="priced",
            occurred_at=datetime(2026, 4, 20, 10, 0, tzinfo=UTC),
            cost_usd=1.25,
        ),
        make_event(
            id="unpriced",
            occurred_at=datetime(2026, 4, 20, 11, 0, tzinfo=UTC),
            cost_usd=None,
            raw_hash="b" * 64,
        ),
    ]
    agg = aggregate_events(events, start=start, end=end)
    assert agg.total_cost_usd == pytest.approx(1.25)
    assert agg.total_messages == 2


def test_aggregate_filters_by_window_bounds() -> None:
    start = datetime(2026, 4, 20, 10, 0, tzinfo=UTC)
    end = datetime(2026, 4, 20, 11, 0, tzinfo=UTC)
    events = [
        make_event(
            id="before",
            occurred_at=start - timedelta(microseconds=1),
            input_tokens=1,
            raw_hash="1" * 64,
        ),
        make_event(
            id="at-start",
            occurred_at=start,
            input_tokens=2,
            raw_hash="2" * 64,
        ),
        make_event(
            id="inside",
            occurred_at=datetime(2026, 4, 20, 10, 30, tzinfo=UTC),
            input_tokens=4,
            raw_hash="3" * 64,
        ),
        make_event(
            id="at-end",
            occurred_at=end,
            input_tokens=8,
            raw_hash="4" * 64,
        ),
        make_event(
            id="after",
            occurred_at=end + timedelta(seconds=1),
            input_tokens=16,
            raw_hash="5" * 64,
        ),
    ]
    agg = aggregate_events(events, start=start, end=end)
    assert agg.total_input_tokens == 2 + 4
    assert agg.total_messages == 2


def test_aggregate_rejects_naive_bounds() -> None:
    naive = datetime(2026, 4, 20, 10, 0)
    aware = datetime(2026, 4, 20, 11, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="start"):
        aggregate_events([], start=naive, end=aware)
    with pytest.raises(ValueError, match="end"):
        aggregate_events([], start=aware, end=naive)


# ---------------------------------------------------------------------------
# capacity
# ---------------------------------------------------------------------------


def _agg(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    messages: int = 0,
    cost: float = 0.0,
) -> UsageAggregate:
    return UsageAggregate(
        total_input_tokens=input_tokens,
        total_output_tokens=output_tokens,
        total_cache_read_tokens=0,
        total_cache_write_tokens=0,
        total_reasoning_tokens=0,
        total_cost_usd=cost,
        total_messages=messages,
    )


def test_capacity_with_only_token_limit() -> None:
    limit = LimitWindow(window_type=WindowType.ROLLING_5H, limit_tokens=1000)
    agg = _agg(input_tokens=400, output_tokens=100)
    cap = capacity(limit, agg)
    assert cap == Capacity(
        limit_basis="tokens",
        used=500.0,
        limit=1000.0,
        remaining=500.0,
        pct_used=0.5,
    )
    assert not cap.is_over


def test_capacity_with_only_message_limit() -> None:
    limit = LimitWindow(window_type=WindowType.ROLLING_5H, limit_messages=45)
    agg = _agg(messages=9)
    cap = capacity(limit, agg)
    assert cap.limit_basis == "messages"
    assert cap.used == 9.0
    assert cap.limit == 45.0
    assert cap.remaining == pytest.approx(36.0)
    assert cap.pct_used == pytest.approx(0.2)


def test_capacity_with_only_usd_limit() -> None:
    limit = LimitWindow(window_type=WindowType.MONTHLY, limit_usd=20.0)
    agg = _agg(cost=5.0)
    cap = capacity(limit, agg)
    assert cap.limit_basis == "usd"
    assert cap.used == pytest.approx(5.0)
    assert cap.limit == pytest.approx(20.0)
    assert cap.remaining == pytest.approx(15.0)
    assert cap.pct_used == pytest.approx(0.25)


def test_capacity_picks_most_constrained_when_multiple() -> None:
    limit = LimitWindow(
        window_type=WindowType.WEEKLY,
        limit_tokens=1000,
        limit_messages=10,
        limit_usd=10.0,
    )
    agg = _agg(input_tokens=100, messages=9, cost=1.0)
    cap = capacity(limit, agg)
    assert cap.limit_basis == "messages"
    assert cap.pct_used == pytest.approx(0.9)
    assert cap.remaining == pytest.approx(1.0)


def test_capacity_none_window_returns_zero_pct() -> None:
    limit = LimitWindow(window_type=WindowType.NONE)
    agg = _agg(input_tokens=500, messages=3, cost=2.50)
    cap = capacity(limit, agg)
    assert cap == Capacity(
        limit_basis="none",
        used=0.0,
        limit=None,
        remaining=None,
        pct_used=0.0,
    )
    assert not cap.is_over


def test_capacity_is_over_when_used_exceeds_limit() -> None:
    limit = LimitWindow(window_type=WindowType.ROLLING_5H, limit_messages=45)
    agg = _agg(messages=60)
    cap = capacity(limit, agg)
    assert cap.is_over
    assert cap.pct_used > 1.0
    assert cap.remaining == 0.0
