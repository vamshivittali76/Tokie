"""Unit tests for the deterministic recommender."""

from __future__ import annotations

from tokie_cli.dashboard.aggregator import SubscriptionView, WindowView
from tokie_cli.routing.recommender import recommend
from tokie_cli.routing.table import (
    RoutingTable,
    TaskEntry,
    TaskRecommendationEntry,
    ToolEntry,
)


def _window(
    window_type: str = "rolling_5h",
    pct_used: float = 0.5,
    shared_with: tuple[str, ...] = (),
    is_over: bool = False,
    limit: float | None = 100.0,
) -> WindowView:
    return WindowView(
        window_type=window_type,
        starts_at=None,
        resets_at=None,
        limit_basis="messages",
        used=pct_used * (limit or 0.0),
        limit=limit,
        remaining=(limit or 0.0) - pct_used * (limit or 0.0),
        pct_used=pct_used if not is_over else 1.2,
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
    display_name: str | None = None,
    account_id: str = "default",
    windows: tuple[WindowView, ...] = (),
) -> SubscriptionView:
    return SubscriptionView(
        plan_id=plan_id,
        display_name=display_name or plan_id,
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
        updated="2026-04-20",
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
            ToolEntry(
                id="openai-api",
                display_name="OpenAI API",
                products=("openai-api",),
                notes=None,
            ),
        ),
        tasks=(
            TaskEntry(
                id="code_generation",
                description="Writing new code.",
                preferred=(
                    TaskRecommendationEntry(
                        tool_id="claude-code", tier=1, rationale="Best overall."
                    ),
                    TaskRecommendationEntry(
                        tool_id="cursor-ide", tier=1, rationale="Great IDE."
                    ),
                    TaskRecommendationEntry(
                        tool_id="openai-api", tier=2, rationale="Fallback."
                    ),
                ),
            ),
        ),
    )


def test_recommend_ranks_by_tier_then_saturation() -> None:
    table = _table()
    subs = (
        _sub("claude_pro", "claude-code", windows=(_window(pct_used=0.9),)),
        _sub("cursor_pro", "cursor-ide", windows=(_window(pct_used=0.1),)),
        _sub("openai_tier1", "openai-api", windows=(_window(pct_used=0.05),)),
    )
    result = recommend(task_id="code_generation", table=table, subscriptions=subs)
    assert result.task_id == "code_generation"
    plans = [r.plan_id for r in result.recommendations]
    assert plans == ["cursor_pro", "claude_pro", "openai_tier1"]
    assert result.recommendations[0].tier == 1
    assert result.recommendations[2].tier == 2


def test_recommend_skips_tools_the_user_does_not_own() -> None:
    table = _table()
    subs = (_sub("cursor_pro", "cursor-ide", windows=(_window(pct_used=0.1),)),)
    result = recommend(task_id="code_generation", table=table, subscriptions=subs)
    assert len(result.recommendations) == 1
    assert result.recommendations[0].plan_id == "cursor_pro"
    assert "claude-code" in result.missing_tools
    assert "openai-api" in result.missing_tools


def test_over_limit_crosses_after_fresh_tier() -> None:
    table = _table()
    subs = (
        _sub(
            "claude_pro",
            "claude-code",
            windows=(_window(is_over=True, pct_used=1.2),),
        ),
        _sub("cursor_pro", "cursor-ide", windows=(_window(pct_used=0.3),)),
    )
    result = recommend(task_id="code_generation", table=table, subscriptions=subs)
    plans = [r.plan_id for r in result.recommendations]
    assert plans[0] == "cursor_pro"
    over = [r for r in result.recommendations if r.is_over]
    assert over[0].plan_id == "claude_pro"
    assert "over limit" in over[0].rationale


def test_shared_with_allows_cross_product_match() -> None:
    table = RoutingTable(
        version=1,
        updated="2026",
        tools=(
            ToolEntry(
                id="claude-web",
                display_name="Claude Web",
                products=("claude-web", "claude-desktop"),
                notes=None,
            ),
        ),
        tasks=(
            TaskEntry(
                id="brainstorming",
                description="x",
                preferred=(
                    TaskRecommendationEntry(
                        tool_id="claude-web", tier=1, rationale="chat UX"
                    ),
                ),
            ),
        ),
    )
    subs = (
        _sub(
            "claude_pro",
            product="claude-code",
            windows=(_window(shared_with=("claude-code", "claude-web"), pct_used=0.2),),
        ),
    )
    result = recommend(task_id="brainstorming", table=table, subscriptions=subs)
    assert len(result.recommendations) == 1
    assert result.recommendations[0].plan_id == "claude_pro"


def test_no_limits_sorts_first_within_tier() -> None:
    table = _table()
    subs = (
        _sub(
            "claude_pro",
            "claude-code",
            windows=(_window(pct_used=0.5, limit=None),),
        ),
        _sub("cursor_pro", "cursor-ide", windows=(_window(pct_used=0.2),)),
    )
    result = recommend(task_id="code_generation", table=table, subscriptions=subs)
    plans = [r.plan_id for r in result.recommendations]
    assert plans[0] == "claude_pro"
    assert plans[1] == "cursor_pro"


def test_deterministic() -> None:
    table = _table()
    subs = (
        _sub("claude_pro", "claude-code", windows=(_window(pct_used=0.5),)),
        _sub("cursor_pro", "cursor-ide", windows=(_window(pct_used=0.5),)),
    )
    r1 = recommend(task_id="code_generation", table=table, subscriptions=subs)
    r2 = recommend(task_id="code_generation", table=table, subscriptions=subs)
    assert [r.plan_id for r in r1.recommendations] == [
        r.plan_id for r in r2.recommendations
    ]
