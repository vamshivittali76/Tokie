"""Tests for the pure-function threshold evaluator."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from tokie_cli.alerts.thresholds import (
    DEFAULT_LEVELS,
    ThresholdCrossing,
    ThresholdRule,
    evaluate_thresholds,
    matches_binding,
    merge_rules_for_binding,
    normalise_levels,
)
from tokie_cli.dashboard.aggregator import SubscriptionView, WindowView


def _window(
    *,
    pct: float = 0.5,
    limit: float | None = 1000.0,
    window_type: str = "rolling_5h",
    starts_at: datetime | None = None,
    resets_at: datetime | None = None,
) -> WindowView:
    return WindowView(
        window_type=window_type,
        starts_at=starts_at or datetime(2026, 4, 20, 10, 0, tzinfo=UTC),
        resets_at=resets_at or datetime(2026, 4, 20, 15, 0, tzinfo=UTC),
        limit_basis="tokens",
        used=pct * (limit or 0),
        limit=limit,
        remaining=(limit - pct * limit) if limit is not None else None,
        pct_used=pct,
        is_over=pct >= 1.0,
        shared_with=(),
        messages=0,
        total_tokens=int(pct * (limit or 0)),
        cost_usd=0.0,
    )


def _sub(
    *,
    plan_id: str = "claude_pro",
    account_id: str = "default",
    windows: tuple[WindowView, ...] = (),
) -> SubscriptionView:
    return SubscriptionView(
        plan_id=plan_id,
        display_name=plan_id,
        provider="anthropic",
        product="claude",
        plan="pro",
        account_id=account_id,
        trackability="local_exact",
        confidence="exact",
        event_count=len(windows),
        windows=windows,
    )


def test_normalise_levels_clips_and_dedupes() -> None:
    result = normalise_levels([75, 95, 200, -10, 75, 50])
    assert result == (0, 50, 75, 95, 100)


def test_normalise_levels_handles_strings() -> None:
    # Forgiving on bad input: non-int entries skipped, rest preserved.
    result = normalise_levels([75, "banana", None, 100])  # type: ignore[list-item]
    assert result == (75, 100)


def test_threshold_rule_defaults_use_75_95_100() -> None:
    rule = ThresholdRule()
    assert rule.levels == DEFAULT_LEVELS
    assert rule.channels == ("banner",)


def test_threshold_rule_post_init_normalises() -> None:
    rule = ThresholdRule(levels=(100, 100, 50, 250), channels=())
    assert rule.levels == (50, 100)
    assert rule.channels == ("banner",)


def test_matches_binding_wildcards() -> None:
    rule = ThresholdRule()
    assert matches_binding(rule, plan_id="claude_pro", account_id="default")

    pinned = ThresholdRule(plan_id="claude_pro")
    assert matches_binding(pinned, plan_id="claude_pro", account_id="default")
    assert not matches_binding(pinned, plan_id="codex_plus", account_id="default")

    fully_pinned = ThresholdRule(plan_id="claude_pro", account_id="alice")
    assert matches_binding(fully_pinned, plan_id="claude_pro", account_id="alice")
    assert not matches_binding(fully_pinned, plan_id="claude_pro", account_id="bob")


def test_merge_rules_for_binding_unions_levels() -> None:
    rules = [
        ThresholdRule(levels=(75, 95)),
        ThresholdRule(plan_id="claude_pro", levels=(50,), channels=("desktop",)),
    ]
    levels, channels = merge_rules_for_binding(
        rules, plan_id="claude_pro", account_id="alice"
    )
    assert levels == (50, 75, 95)
    assert set(channels) == {"banner", "desktop"}


def test_merge_rules_for_binding_returns_empty_when_no_match() -> None:
    rules = [ThresholdRule(plan_id="other_plan")]
    levels, channels = merge_rules_for_binding(
        rules, plan_id="claude_pro", account_id="alice"
    )
    assert levels == ()
    assert channels == ()


def test_evaluate_thresholds_fires_every_level_crossed() -> None:
    sub = _sub(windows=(_window(pct=1.02),))
    result = evaluate_thresholds([sub], [ThresholdRule()])
    assert [c.threshold_pct for c in result] == [100, 95, 75]
    # Highest-severity first.
    assert result[0].threshold_pct == 100

    # 98% crosses 95 + 75 but not 100.
    sub98 = _sub(windows=(_window(pct=0.98),))
    r98 = evaluate_thresholds([sub98], [ThresholdRule()])
    assert [c.threshold_pct for c in r98] == [95, 75]


def test_evaluate_thresholds_skips_windows_without_limit() -> None:
    window = _window(pct=0.99, limit=None)
    sub = _sub(windows=(window,))
    assert evaluate_thresholds([sub], [ThresholdRule()]) == []


def test_evaluate_thresholds_skips_subs_without_rules() -> None:
    sub = _sub(windows=(_window(pct=0.99),))
    rules = [ThresholdRule(plan_id="other_plan")]
    assert evaluate_thresholds([sub], rules) == []


def test_evaluate_thresholds_respects_window_starts_at() -> None:
    starts = datetime(2026, 4, 20, 10, 0, tzinfo=UTC)
    window = _window(pct=0.76, starts_at=starts)
    sub = _sub(windows=(window,))
    [crossing] = evaluate_thresholds([sub], [ThresholdRule(levels=(75,))])
    assert crossing.window_starts_at_iso == starts.isoformat()
    assert crossing.severity() == "medium"


def test_threshold_crossing_dedupe_key_is_stable() -> None:
    c = ThresholdCrossing(
        plan_id="claude_pro",
        account_id="alice",
        display_name="Claude Pro",
        provider="anthropic",
        product="claude",
        window_type="rolling_5h",
        window_starts_at_iso="2026-04-20T10:00:00+00:00",
        window_resets_at_iso="2026-04-20T15:00:00+00:00",
        threshold_pct=95,
        pct_used=0.97,
        used=970.0,
        limit=1000.0,
        remaining=30.0,
    )
    assert c.dedupe_key == (
        "claude_pro",
        "alice",
        "rolling_5h",
        "2026-04-20T10:00:00+00:00",
        95,
    )


def test_threshold_crossing_severity_buckets() -> None:
    args = dict(
        plan_id="p",
        account_id="a",
        display_name="P",
        provider="pr",
        product="pd",
        window_type="daily",
        window_starts_at_iso="",
        window_resets_at_iso="",
        pct_used=0.0,
        used=0.0,
        limit=None,
        remaining=None,
    )
    cross_low = ThresholdCrossing(**{**args, "threshold_pct": 25})  # type: ignore[arg-type]
    cross_mid = ThresholdCrossing(**{**args, "threshold_pct": 75})  # type: ignore[arg-type]
    cross_high = ThresholdCrossing(**{**args, "threshold_pct": 95})  # type: ignore[arg-type]
    cross_over = ThresholdCrossing(**{**args, "threshold_pct": 100})  # type: ignore[arg-type]
    assert cross_low.severity() == "low"
    assert cross_mid.severity() == "medium"
    assert cross_high.severity() == "high"
    assert cross_over.severity() == "over"


def test_evaluate_thresholds_emits_empty_iso_for_none_window() -> None:
    window = replace(
        _window(pct=0.99),
        window_type="none",
        starts_at=None,
        resets_at=None,
    )
    sub = _sub(windows=(window,))
    [crossing] = evaluate_thresholds([sub], [ThresholdRule(levels=(95,))])
    assert crossing.window_starts_at_iso == ""
    assert crossing.window_resets_at_iso == ""


def test_evaluate_thresholds_orders_by_severity_then_plan_then_account() -> None:
    subs = [
        _sub(plan_id="a", account_id="z", windows=(_window(pct=0.8),)),
        _sub(plan_id="b", account_id="y", windows=(_window(pct=0.96),)),
    ]
    rules = [ThresholdRule()]
    results = evaluate_thresholds(subs, rules)
    # b hits 95 and 75, a hits 75. Highest threshold first.
    assert [(c.plan_id, c.threshold_pct) for c in results[:2]] == [
        ("b", 95),
        ("a", 75),
    ]


def test_evaluate_thresholds_float_fuzz_boundary() -> None:
    sub = _sub(windows=(_window(pct=0.75),))
    [crossing] = evaluate_thresholds([sub], [ThresholdRule(levels=(75,))])
    assert crossing.threshold_pct == 75


def test_window_starts_at_none_is_only_omitted_for_ungated_windows() -> None:
    window = replace(
        _window(pct=0.99),
        starts_at=None,
        resets_at=None,
    )
    sub = _sub(windows=(window,))
    [crossing] = evaluate_thresholds([sub], [ThresholdRule(levels=(95,))])
    assert crossing.window_starts_at_iso == ""


def test_channels_default_to_banner_when_rule_passes_empty_tuple() -> None:
    rule = ThresholdRule(channels=())
    levels, channels = merge_rules_for_binding(
        [rule], plan_id="claude_pro", account_id="default"
    )
    assert levels == DEFAULT_LEVELS
    assert channels == ("banner",)


@pytest.mark.parametrize("bad", [-5, 150, 0, 100])
def test_normalise_levels_boundary_cases(bad: int) -> None:
    got = normalise_levels([bad])
    if bad == 0:
        assert got == (0,)
    elif bad == 100:
        assert got == (100,)
    else:
        assert got == (max(0, min(100, bad)),)


def test_evaluate_thresholds_ignores_zero_limit() -> None:
    window = _window(pct=0.99, limit=0.0)
    sub = _sub(windows=(window,))
    assert evaluate_thresholds([sub], [ThresholdRule()]) == []


def test_dedupe_key_unchanged_across_rerenders() -> None:
    """A window's dedupe key should not change when we re-evaluate mid-window."""

    starts = datetime(2026, 4, 20, 10, 0, tzinfo=UTC)
    resets = starts + timedelta(hours=5)
    sub = _sub(windows=(_window(pct=0.8, starts_at=starts, resets_at=resets),))
    r1 = evaluate_thresholds([sub], [ThresholdRule(levels=(75,))])
    r2 = evaluate_thresholds([sub], [ThresholdRule(levels=(75,))])
    assert r1[0].dedupe_key == r2[0].dedupe_key
