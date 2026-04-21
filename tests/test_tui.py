"""Tests for :mod:`tokie_cli.tui`.

Textual's App main loop is difficult to exercise in a unit test without a
real terminal, so these tests focus on the pure helper functions and the
collaboration with the aggregator. The App itself gets a smoke test via
``tokie watch --help``-style launches in the CLI test module.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tokie_cli.dashboard.aggregator import WindowView
from tokie_cli.schema import UsageEvent
from tokie_cli.tui import _fmt_countdown, _render_bar, _sparkline


def _make_window(pct: float) -> WindowView:
    return WindowView(
        window_type="rolling_5h",
        starts_at=None,
        resets_at=None,
        limit_basis="messages",
        used=pct * 100,
        limit=100.0,
        remaining=100.0 - pct * 100,
        pct_used=pct,
        is_over=pct >= 1.0,
        shared_with=(),
        messages=0,
        total_tokens=0,
        cost_usd=0.0,
    )


def test_fmt_countdown_none_returns_no_reset() -> None:
    now = datetime(2026, 4, 20, tzinfo=UTC)
    assert _fmt_countdown(None, now=now) == "no reset"


def test_fmt_countdown_past_returns_now() -> None:
    now = datetime(2026, 4, 20, 12, tzinfo=UTC)
    assert _fmt_countdown(now - timedelta(minutes=5), now=now) == "now"


def test_fmt_countdown_renders_hours_and_minutes() -> None:
    now = datetime(2026, 4, 20, 12, tzinfo=UTC)
    target = now + timedelta(hours=2, minutes=30)
    assert _fmt_countdown(target, now=now) == "2h 30m"


def test_fmt_countdown_renders_days() -> None:
    now = datetime(2026, 4, 20, tzinfo=UTC)
    target = now + timedelta(days=3, hours=5)
    assert _fmt_countdown(target, now=now) == "3d 5h"


def test_render_bar_colour_escalates_with_usage() -> None:
    low = _render_bar(_make_window(0.2)).markup
    mid = _render_bar(_make_window(0.8)).markup
    high = _render_bar(_make_window(0.97)).markup
    over = _render_bar(_make_window(1.2)).markup
    assert "green" in low
    assert "yellow" in mid
    assert "red" in high
    assert "bold red" in over


def test_sparkline_builds_fixed_width_string() -> None:
    now = datetime(2026, 4, 20, 12, tzinfo=UTC)
    events = [
        UsageEvent(
            id=f"e{i}",
            collected_at=now,
            occurred_at=now - timedelta(hours=i),
            provider="anthropic",
            product="claude-code",
            account_id="a",
            session_id=None,
            project=None,
            model="sonnet-4.5",
            input_tokens=100 * (i + 1),
            output_tokens=10,
            cache_read_tokens=0,
            cache_write_tokens=0,
            reasoning_tokens=0,
            cost_usd=None,
            confidence="exact",  # type: ignore[arg-type]
            source="test",
            raw_hash=f"{i}" * 8,
        )
        for i in range(10)
    ]
    spark = _sparkline(events, now=now)
    assert len(spark) == 24
    assert any(c != " " for c in spark)
