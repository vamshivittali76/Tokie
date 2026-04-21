"""Tests for :mod:`tokie_cli.dashboard.aggregator`.

Pure-function tests only — no DB, no HTTP, no filesystem.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tokie_cli.config import SubscriptionBinding
from tokie_cli.dashboard.aggregator import (
    build_daily_bars,
    build_payload,
    build_provider_breakdown,
    build_recent_events,
    build_subscription_views,
)
from tokie_cli.plans import PlanTemplate, Trackability
from tokie_cli.schema import (
    Confidence,
    LimitWindow,
    Subscription,
    UsageEvent,
    WindowType,
    compute_raw_hash,
)

NOW = datetime(2026, 4, 20, 12, tzinfo=UTC)


def _event(
    *,
    when: datetime,
    provider: str = "anthropic",
    product: str = "claude-code",
    account_id: str = "default",
    model: str = "claude-sonnet-4",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cost_usd: float | None = 0.01,
    confidence: Confidence = Confidence.EXACT,
    cache_read: int = 0,
    reasoning: int = 0,
) -> UsageEvent:
    raw = compute_raw_hash(f"{when.isoformat()}-{product}-{input_tokens}-{output_tokens}")
    return UsageEvent(
        id=raw[:16],
        collected_at=NOW,
        occurred_at=when,
        provider=provider,
        product=product,
        account_id=account_id,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        reasoning_tokens=reasoning,
        cost_usd=cost_usd,
        confidence=confidence,
        source=f"test:{product}",
        raw_hash=raw,
    )


def _claude_plan() -> PlanTemplate:
    return PlanTemplate(
        id="claude_pro_personal",
        display_name="Claude Pro",
        source_url="https://example.com",
        notes=None,
        subscription=Subscription(
            id="claude_pro_personal",
            provider="anthropic",
            product="claude-pro",
            plan="pro",
            account_id="default",
            windows=[
                LimitWindow(
                    window_type=WindowType.ROLLING_5H,
                    limit_messages=45,
                    shared_with=["claude-code", "claude-web", "claude-desktop"],
                ),
                LimitWindow(
                    window_type=WindowType.WEEKLY,
                    limit_messages=900,
                    shared_with=["claude-code", "claude-web", "claude-desktop"],
                ),
            ],
        ),
        trackability=Trackability.LOCAL_EXACT,
    )


def _manus_plan() -> PlanTemplate:
    return PlanTemplate(
        id="manus_personal",
        display_name="Manus",
        source_url="https://example.com",
        notes="web-only",
        subscription=Subscription(
            id="manus_personal",
            provider="manus",
            product="manus-web",
            plan="personal",
            account_id="default",
            windows=[LimitWindow(window_type=WindowType.MONTHLY)],
        ),
        trackability=Trackability.WEB_ONLY_MANUAL,
    )


def test_claude_rolling_5h_counts_shared_products() -> None:
    events = [
        _event(when=NOW - timedelta(hours=1), product="claude-code"),
        _event(when=NOW - timedelta(hours=2), product="claude-web"),
        _event(when=NOW - timedelta(hours=3), product="claude-desktop"),
        _event(when=NOW - timedelta(hours=6), product="claude-code"),  # outside 5h
    ]
    views = build_subscription_views(
        [SubscriptionBinding(plan_id="claude_pro_personal", account_id="default")],
        [_claude_plan()],
        events,
        now=NOW,
    )
    assert len(views) == 1
    rolling = views[0].windows[0]
    assert rolling.window_type == "rolling_5h"
    assert rolling.messages == 3  # the three events inside the 5h rolling window
    assert rolling.limit == 45
    assert rolling.pct_used == 3 / 45


def test_weekly_window_includes_events_within_7_days() -> None:
    events = [_event(when=NOW - timedelta(days=i)) for i in range(0, 10)]
    views = build_subscription_views(
        [SubscriptionBinding(plan_id="claude_pro_personal", account_id="default")],
        [_claude_plan()],
        events,
        now=NOW,
    )
    weekly = views[0].windows[1]
    assert weekly.window_type == "weekly"
    assert 7 <= weekly.messages <= 8  # 7-day window, event-anchored


def test_unknown_binding_is_silently_skipped() -> None:
    views = build_subscription_views(
        [SubscriptionBinding(plan_id="does-not-exist", account_id="default")],
        [_claude_plan()],
        [],
        now=NOW,
    )
    assert views == ()


def test_web_only_forces_inferred_confidence() -> None:
    events = [
        _event(
            when=NOW - timedelta(hours=1),
            provider="manus",
            product="manus-web",
            confidence=Confidence.EXACT,  # force upgrade attempt
        )
    ]
    views = build_subscription_views(
        [SubscriptionBinding(plan_id="manus_personal", account_id="default")],
        [_manus_plan()],
        events,
        now=NOW,
    )
    assert len(views) == 1
    assert views[0].confidence == "inferred"
    assert views[0].trackability == "web_only_manual"


def test_weakest_confidence_wins() -> None:
    events = [
        _event(when=NOW - timedelta(hours=1), confidence=Confidence.EXACT),
        _event(
            when=NOW - timedelta(hours=2),
            confidence=Confidence.ESTIMATED,
            input_tokens=200,
        ),
    ]
    views = build_subscription_views(
        [SubscriptionBinding(plan_id="claude_pro_personal", account_id="default")],
        [_claude_plan()],
        events,
        now=NOW,
    )
    assert views[0].confidence == "estimated"


def test_no_cap_window_returns_basis_none() -> None:
    events = [
        _event(
            when=NOW - timedelta(hours=1),
            provider="manus",
            product="manus-web",
            confidence=Confidence.INFERRED,
        )
    ]
    views = build_subscription_views(
        [SubscriptionBinding(plan_id="manus_personal", account_id="default")],
        [_manus_plan()],
        events,
        now=NOW,
    )
    monthly = views[0].windows[0]
    assert monthly.window_type == "monthly"
    assert monthly.limit_basis == "none"
    assert monthly.limit is None
    assert monthly.pct_used == 0.0


def test_account_isolation() -> None:
    events = [
        _event(when=NOW - timedelta(hours=1), account_id="work"),
        _event(when=NOW - timedelta(hours=2), account_id="personal"),
    ]
    views = build_subscription_views(
        [SubscriptionBinding(plan_id="claude_pro_personal", account_id="personal")],
        [_claude_plan()],
        events,
        now=NOW,
    )
    rolling = views[0].windows[0]
    assert rolling.messages == 1


def test_recent_events_are_sorted_newest_first() -> None:
    events = [
        _event(when=NOW - timedelta(hours=5)),
        _event(when=NOW - timedelta(hours=1)),
        _event(when=NOW - timedelta(hours=3)),
    ]
    recent = build_recent_events(events, limit=10)
    assert [e.occurred_at for e in recent] == sorted([e.occurred_at for e in events], reverse=True)


def test_recent_events_respects_limit() -> None:
    events = [_event(when=NOW - timedelta(minutes=i)) for i in range(50)]
    recent = build_recent_events(events, limit=5)
    assert len(recent) == 5


def test_daily_bars_14_days_always_present() -> None:
    bars = build_daily_bars([], now=NOW, days_back=14)
    assert len(bars) == 14
    assert bars[-1].date == NOW.date().isoformat()
    assert all(b.total_tokens == 0 for b in bars)


def test_daily_bars_group_by_utc_date() -> None:
    events = [
        _event(when=NOW - timedelta(days=1), input_tokens=100, output_tokens=50),
        _event(when=NOW - timedelta(days=1), input_tokens=200, output_tokens=100),
        _event(when=NOW, input_tokens=10, output_tokens=5),
    ]
    bars = build_daily_bars(events, now=NOW, days_back=14)
    today = next(b for b in bars if b.date == NOW.date().isoformat())
    yesterday = next(b for b in bars if b.date == (NOW - timedelta(days=1)).date().isoformat())
    assert today.input_tokens == 10
    assert yesterday.input_tokens == 300


def test_provider_breakdown_sums_correctly() -> None:
    events = [
        _event(when=NOW, provider="anthropic", product="claude-code", cost_usd=0.10),
        _event(when=NOW, provider="openai", product="chatgpt-web", cost_usd=None),
        _event(when=NOW, provider="anthropic", product="claude-code", cost_usd=0.20),
    ]
    breakdown = build_provider_breakdown(events)
    anthropic_row = next(row for row in breakdown if row["provider"] == "anthropic")
    assert anthropic_row["events"] == 2
    assert anthropic_row["cost_usd"] == pytest.approx(0.30)


def test_build_payload_fills_every_section() -> None:
    events = [
        _event(when=NOW - timedelta(hours=1)),
        _event(when=NOW - timedelta(days=2), input_tokens=500),
    ]
    payload = build_payload(
        [SubscriptionBinding(plan_id="claude_pro_personal", account_id="default")],
        [_claude_plan()],
        events,
        now=NOW,
    )
    assert payload.event_count == 2
    assert payload.subscription_count == 1
    assert len(payload.daily_bars) == 14
    assert len(payload.recent_events) == 2
    assert len(payload.provider_breakdown) == 1
