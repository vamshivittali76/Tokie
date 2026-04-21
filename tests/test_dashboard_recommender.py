"""Tests for the ``/api/routing`` and ``/api/recommend`` dashboard endpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from tokie_cli.config import SubscriptionBinding, TokieConfig
from tokie_cli.dashboard.server import AppState, create_app
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
    when: datetime = NOW,
    *,
    provider: str = "anthropic",
    product: str = "claude-code",
) -> UsageEvent:
    raw = compute_raw_hash(f"{when.isoformat()}-{provider}-{product}")
    return UsageEvent(
        id=raw[:16],
        collected_at=NOW,
        occurred_at=when,
        provider=provider,
        product=product,
        account_id="default",
        model="m",
        input_tokens=100,
        output_tokens=50,
        confidence=Confidence.EXACT,
        source="test",
        raw_hash=raw,
    )


def _plan(
    plan_id: str,
    product: str,
    *,
    shared: list[str] | None = None,
    provider: str = "anthropic",
) -> PlanTemplate:
    return PlanTemplate(
        id=plan_id,
        display_name=plan_id,
        source_url="https://example.com",
        notes=None,
        subscription=Subscription(
            id=plan_id,
            provider=provider,
            product=product,
            plan=plan_id,
            account_id="default",
            windows=[
                LimitWindow(
                    window_type=WindowType.ROLLING_5H,
                    limit_messages=45,
                    shared_with=shared or [product],
                )
            ],
        ),
        trackability=Trackability.LOCAL_EXACT,
    )


def _make_client(
    *,
    tmp_path: Path,
    events: list[UsageEvent] | None = None,
    plans: list[PlanTemplate] | None = None,
    bindings: tuple[SubscriptionBinding, ...] = (
        SubscriptionBinding(plan_id="claude_pro_personal", account_id="default"),
        SubscriptionBinding(plan_id="cursor_pro_personal", account_id="default"),
    ),
) -> TestClient:
    config = TokieConfig(
        db_path=tmp_path / "tokie.db",
        audit_log_path=tmp_path / "audit.log",
        subscriptions=bindings,
    )
    state = AppState(
        config=config,
        plans_loader=lambda: plans
        if plans is not None
        else [
            _plan(
                "claude_pro_personal",
                "claude-code",
                shared=["claude-code", "claude-web"],
            ),
            _plan(
                "cursor_pro_personal", "cursor-ide", provider="cursor"
            ),
        ],
        events_loader=lambda _cfg: events or [],
        now=lambda: NOW,
    )
    return TestClient(create_app(state=state))


def test_routing_catalog_returns_tasks_and_tools(tmp_path: Path) -> None:
    client = _make_client(tmp_path=tmp_path)
    res = client.get("/api/routing")
    assert res.status_code == 200
    body = res.json()
    assert body["version"] >= 1
    task_ids = [t["id"] for t in body["tasks"]]
    assert "code_generation" in task_ids
    assert "debugging" in task_ids
    tool_ids = [t["id"] for t in body["tools"]]
    assert "claude-code" in tool_ids
    for task in body["tasks"]:
        for entry in task["preferred"]:
            assert entry["tool"] in tool_ids


def test_recommend_returns_ranked_user_subscriptions(tmp_path: Path) -> None:
    # No load yet -> everything is at 0% -> ordering is tier only.
    client = _make_client(tmp_path=tmp_path)
    res = client.get("/api/recommend", params={"task": "code_generation"})
    assert res.status_code == 200
    body = res.json()
    assert body["task_id"] == "code_generation"
    recs = body["recommendations"]
    assert recs, body
    plans = [r["plan_id"] for r in recs]
    assert "claude_pro_personal" in plans
    assert "cursor_pro_personal" in plans


def test_recommend_includes_handoff_suggestions_when_over_limit(
    tmp_path: Path,
) -> None:
    # Enough events to blow past the 45-msg Claude Pro limit.
    events = [
        _event(when=NOW - timedelta(minutes=i), product="claude-code")
        for i in range(60)
    ]
    client = _make_client(events=events, tmp_path=tmp_path)
    # Add a 100% threshold so the crossing registers.
    client.post(
        "/api/thresholds",
        json={
            "thresholds": [
                {
                    "plan_id": None,
                    "account_id": None,
                    "levels": [100],
                    "channels": ["banner"],
                }
            ]
        },
    )
    res = client.get("/api/recommend", params={"task": "code_generation"})
    assert res.status_code == 200
    body = res.json()
    assert body["handoff_suggestions"], body
    hint = body["handoff_suggestions"][0]
    assert hint["saturated_plan_id"] == "claude_pro_personal"
    assert hint["alternative"]["plan_id"] == "cursor_pro_personal"


def test_recommend_unknown_task_returns_404(tmp_path: Path) -> None:
    client = _make_client(tmp_path=tmp_path)
    res = client.get("/api/recommend", params={"task": "not-a-task"})
    assert res.status_code == 404
