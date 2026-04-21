"""Unit tests for the handoff extractor."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tokie_cli.dashboard.aggregator import SubscriptionView, WindowView
from tokie_cli.routing.handoff import build_handoff, render_handoff
from tokie_cli.routing.recommender import Recommendation
from tokie_cli.schema import Confidence, UsageEvent


def _event(
    occurred_at: datetime,
    *,
    session_id: str | None = None,
    product: str = "claude-code",
    provider: str = "anthropic",
    total: int = 2000,
) -> UsageEvent:
    return UsageEvent(
        id=f"ev-{occurred_at.isoformat()}",
        collected_at=occurred_at,
        occurred_at=occurred_at,
        provider=provider,
        product=product,
        account_id="default",
        session_id=session_id,
        project="demo",
        model="claude-sonnet",
        input_tokens=total // 2,
        output_tokens=total // 2,
        confidence=Confidence.EXACT,
        source="test",
        raw_hash=f"hash-{occurred_at.isoformat()}",
    )


def _view() -> SubscriptionView:
    return SubscriptionView(
        plan_id="claude_pro_personal",
        display_name="Claude Pro",
        provider="anthropic",
        product="claude-code",
        plan="pro",
        account_id="default",
        trackability="local_exact",
        confidence="exact",
        event_count=4,
        windows=(
            WindowView(
                window_type="rolling_5h",
                starts_at=None,
                resets_at=None,
                limit_basis="messages",
                used=40,
                limit=45,
                remaining=5,
                pct_used=40 / 45,
                is_over=False,
                shared_with=(),
                messages=40,
                total_tokens=0,
                cost_usd=0.0,
            ),
        ),
    )


def _target() -> Recommendation:
    return Recommendation(
        tool_id="cursor-ide",
        tool_display_name="Cursor IDE",
        plan_id="cursor_pro_personal",
        plan_display_name="Cursor Pro",
        account_id="default",
        product="cursor-ide",
        tier=1,
        rationale="IDE integration.",
        saturation=0.1,
        remaining_fraction=0.9,
        worst_window_type="monthly",
        is_over=False,
    )


def test_build_handoff_trims_to_max_events() -> None:
    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    events = [_event(now.replace(hour=h)) for h in range(12)]
    brief = build_handoff(
        generated_at=now,
        events=events,
        max_events=5,
    )
    assert len(brief.events) == 5
    assert brief.events[0].occurred_at.hour == 7
    assert brief.events[-1].occurred_at.hour == 11


def test_build_handoff_filters_by_session() -> None:
    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    events = [
        _event(now.replace(hour=10), session_id="s1"),
        _event(now.replace(hour=11), session_id="s2"),
        _event(now.replace(hour=11, minute=30), session_id="s1"),
    ]
    brief = build_handoff(
        generated_at=now, events=events, session_id="s1", max_events=10
    )
    assert all(e.session_id == "s1" for e in brief.events)
    assert len(brief.events) == 2


def test_build_handoff_default_goal() -> None:
    now = datetime.now(UTC)
    brief = build_handoff(generated_at=now, events=[])
    assert brief.goal == "Continue the previous session."


def test_build_handoff_with_source_and_target_has_reasons() -> None:
    now = datetime.now(UTC)
    brief = build_handoff(
        generated_at=now,
        events=[],
        source_subscription=_view(),
        target=_target(),
        goal="finish the refactor",
    )
    assert brief.goal == "finish the refactor"
    assert brief.source_plan == "claude_pro_personal"
    assert brief.target is not None
    assert len(brief.reasons) >= 2


def test_render_handoff_markdown_has_sections() -> None:
    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    brief = build_handoff(
        generated_at=now,
        events=[_event(now)],
        source_subscription=_view(),
        target=_target(),
        goal="refactor plans loader",
    )
    rendered = render_handoff(brief, fmt="markdown")
    assert "# Tokie handoff" in rendered
    assert "## Goal" in rendered
    assert "## Going to" in rendered
    assert "## Recent context" in rendered
    assert "Cursor IDE" in rendered
    assert "claude-code" in rendered


def test_render_handoff_plain_has_no_markdown_headers() -> None:
    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    brief = build_handoff(
        generated_at=now, events=[_event(now)], goal="test"
    )
    rendered = render_handoff(brief, fmt="plain")
    assert "# " not in rendered
    assert "TOKIE HANDOFF" in rendered
    assert "Prompt:" in rendered


def test_render_handoff_unknown_format_raises() -> None:
    brief = build_handoff(generated_at=datetime.now(UTC), events=[])
    with pytest.raises(ValueError):
        render_handoff(brief, fmt="rst")
