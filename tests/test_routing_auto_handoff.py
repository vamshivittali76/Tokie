"""Tests for the auto-handoff bridge used by the alert engine."""

from __future__ import annotations

from datetime import UTC, datetime

from tokie_cli.alerts.thresholds import ThresholdCrossing
from tokie_cli.dashboard.aggregator import SubscriptionView, WindowView
from tokie_cli.routing.auto_handoff import suggest_alternatives
from tokie_cli.routing.table import (
    RoutingTable,
    TaskEntry,
    TaskRecommendationEntry,
    ToolEntry,
)


def _window(
    pct_used: float = 0.5,
    is_over: bool = False,
    shared_with: tuple[str, ...] = (),
) -> WindowView:
    return WindowView(
        window_type="rolling_5h",
        starts_at=None,
        resets_at=None,
        limit_basis="messages",
        used=0,
        limit=100.0,
        remaining=100.0,
        pct_used=pct_used,
        is_over=is_over,
        shared_with=shared_with,
        messages=0,
        total_tokens=0,
        cost_usd=0.0,
    )


def _sub(
    plan_id: str,
    product: str,
    *,
    account_id: str = "default",
    windows: tuple[WindowView, ...] = (),
) -> SubscriptionView:
    return SubscriptionView(
        plan_id=plan_id,
        display_name=plan_id,
        provider="any",
        product=product,
        plan=plan_id,
        account_id=account_id,
        trackability="local_exact",
        confidence="exact",
        event_count=0,
        windows=windows,
    )


def _table() -> RoutingTable:
    return RoutingTable(
        version=1,
        updated="2026",
        tools=(
            ToolEntry(
                id="claude-code",
                display_name="Claude Code",
                products=("claude-code",),
                notes=None,
            ),
            ToolEntry(
                id="cursor-ide",
                display_name="Cursor",
                products=("cursor-ide",),
                notes=None,
            ),
        ),
        tasks=(
            TaskEntry(
                id="code_generation",
                description="",
                preferred=(
                    TaskRecommendationEntry(
                        tool_id="claude-code", tier=1, rationale=""
                    ),
                    TaskRecommendationEntry(
                        tool_id="cursor-ide", tier=1, rationale="idx"
                    ),
                ),
            ),
        ),
    )


def _crossing(
    plan_id: str = "claude_pro",
    pct: float = 1.05,
    threshold: int = 100,
    is_over: bool = True,
) -> ThresholdCrossing:
    # ``is_over`` is kept in the signature for test readability; the
    # runtime ``ThresholdCrossing`` infers it from pct_used/threshold_pct.
    del is_over
    return ThresholdCrossing(
        plan_id=plan_id,
        account_id="default",
        display_name=plan_id,
        provider="any",
        product="any",
        window_type="rolling_5h",
        window_starts_at_iso="2026-04-20T10:00:00+00:00",
        window_resets_at_iso="2026-04-20T15:00:00+00:00",
        threshold_pct=threshold,
        pct_used=pct,
        used=105.0,
        limit=100.0,
        remaining=0.0,
        channels=("banner",),
    )


def test_suggests_alternative_on_over_limit() -> None:
    table = _table()
    subs = (
        _sub("claude_pro", "claude-code", windows=(_window(is_over=True, pct_used=1.05),)),
        _sub("cursor_pro", "cursor-ide", windows=(_window(pct_used=0.2),)),
    )
    crossings = (_crossing(plan_id="claude_pro"),)
    out = suggest_alternatives(
        crossings=crossings, subscriptions=subs, table=table
    )
    assert len(out) == 1
    suggestion = out[0]
    assert suggestion.saturated_plan_id == "claude_pro"
    assert suggestion.alternative is not None
    assert suggestion.alternative.plan_id == "cursor_pro"


def test_skips_non_over_crossings_by_default() -> None:
    table = _table()
    subs = (
        _sub("claude_pro", "claude-code", windows=(_window(pct_used=0.96),)),
        _sub("cursor_pro", "cursor-ide", windows=(_window(pct_used=0.2),)),
    )
    crossings = (_crossing(pct=0.96, threshold=95, is_over=False),)
    out = suggest_alternatives(
        crossings=crossings, subscriptions=subs, table=table
    )
    assert out == ()


def test_only_over_false_includes_near_miss() -> None:
    table = _table()
    subs = (
        _sub("claude_pro", "claude-code", windows=(_window(pct_used=0.96),)),
        _sub("cursor_pro", "cursor-ide", windows=(_window(pct_used=0.2),)),
    )
    crossings = (_crossing(pct=0.96, threshold=95, is_over=False),)
    out = suggest_alternatives(
        crossings=crossings,
        subscriptions=subs,
        table=table,
        only_over=False,
    )
    assert len(out) == 1


def test_no_alternative_when_user_owns_only_one_tool() -> None:
    table = _table()
    subs = (
        _sub(
            "claude_pro",
            "claude-code",
            windows=(_window(is_over=True, pct_used=1.05),),
        ),
    )
    crossings = (_crossing(),)
    out = suggest_alternatives(
        crossings=crossings, subscriptions=subs, table=table
    )
    assert len(out) == 1
    assert out[0].alternative is None
    assert "no alternative" in out[0].reason


def test_unknown_fallback_task_returns_empty() -> None:
    table = _table()
    subs = (
        _sub(
            "claude_pro",
            "claude-code",
            windows=(_window(is_over=True),),
        ),
    )
    crossings = (_crossing(),)
    out = suggest_alternatives(
        crossings=crossings,
        subscriptions=subs,
        table=table,
        fallback_task="not-a-task",
    )
    assert out == ()


def test_empty_crossings_returns_empty() -> None:
    table = _table()
    subs = (_sub("cursor_pro", "cursor-ide", windows=(_window(pct_used=0.1),)),)
    out = suggest_alternatives(crossings=(), subscriptions=subs, table=table)
    assert out == ()


def test_clock_is_irrelevant() -> None:
    """Regression smoke test: the function must not depend on ``datetime.now``."""

    # Not used by suggest_alternatives, but the call itself should succeed
    # irrespective of time.
    _ = datetime.now(UTC)
    table = _table()
    subs = (
        _sub("claude_pro", "claude-code", windows=(_window(is_over=True),)),
        _sub("cursor_pro", "cursor-ide", windows=(_window(pct_used=0.2),)),
    )
    crossings = (_crossing(),)
    out = suggest_alternatives(
        crossings=crossings, subscriptions=subs, table=table
    )
    assert out and out[0].alternative is not None
