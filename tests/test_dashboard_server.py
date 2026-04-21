"""Tests for :mod:`tokie_cli.dashboard.server`.

Every route is exercised through FastAPI's :class:`TestClient` with an
in-memory app that injects synthetic events + plans, so no real DB, no
real keyring, no real filesystem reads are touched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tokie_cli.config import SubscriptionBinding, TokieConfig
from tokie_cli.dashboard.server import AppState, create_app, run
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
    when: datetime = NOW,
    provider: str = "anthropic",
    product: str = "claude-code",
    tokens: int = 100,
    cost: float | None = 0.01,
    confidence: Confidence = Confidence.EXACT,
) -> UsageEvent:
    raw = compute_raw_hash(f"{when.isoformat()}-{provider}-{product}-{tokens}")
    return UsageEvent(
        id=raw[:16],
        collected_at=NOW,
        occurred_at=when,
        provider=provider,
        product=product,
        account_id="default",
        model="test-model",
        input_tokens=tokens,
        output_tokens=tokens // 2,
        cost_usd=cost,
        confidence=confidence,
        source="test",
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
                    shared_with=["claude-code", "claude-web"],
                )
            ],
        ),
        trackability=Trackability.LOCAL_EXACT,
    )


def _make_client(
    *,
    events: list[UsageEvent] | None = None,
    plans: list[PlanTemplate] | None = None,
    bindings: tuple[SubscriptionBinding, ...] = (
        SubscriptionBinding(plan_id="claude_pro_personal", account_id="default"),
    ),
    tmp_path: Path,
) -> TestClient:
    config = TokieConfig(
        db_path=tmp_path / "tokie.db",
        audit_log_path=tmp_path / "audit.log",
        subscriptions=bindings,
    )
    state = AppState(
        config=config,
        plans_loader=lambda: plans if plans is not None else [_claude_plan()],
        events_loader=lambda cfg: events or [],
        now=lambda: NOW,
    )
    app = create_app(state=state)
    return TestClient(app)


def test_health_endpoint_returns_ok(tmp_path: Path) -> None:
    client = _make_client(tmp_path=tmp_path)
    res = client.get("/api/health")
    assert res.status_code == 200
    payload = res.json()
    assert payload["ok"] is True
    assert payload["db_present"] is False
    assert "version" in payload


def test_status_endpoint_with_no_events(tmp_path: Path) -> None:
    client = _make_client(tmp_path=tmp_path)
    res = client.get("/api/status")
    assert res.status_code == 200
    payload = res.json()
    assert payload["event_count"] == 0
    assert len(payload["daily_bars"]) == 14
    assert payload["subscriptions"][0]["plan_id"] == "claude_pro_personal"


def test_status_endpoint_with_events(tmp_path: Path) -> None:
    events = [
        _event(when=NOW - timedelta(hours=1)),
        _event(when=NOW - timedelta(hours=2), product="claude-web"),
    ]
    client = _make_client(events=events, tmp_path=tmp_path)
    res = client.get("/api/status")
    assert res.status_code == 200
    payload = res.json()
    assert payload["event_count"] == 2
    sub = payload["subscriptions"][0]
    assert sub["windows"][0]["messages"] == 2


def test_subscriptions_endpoint(tmp_path: Path) -> None:
    client = _make_client(tmp_path=tmp_path)
    res = client.get("/api/subscriptions")
    assert res.status_code == 200
    assert len(res.json()["subscriptions"]) == 1


def test_events_endpoint_respects_recency_order(tmp_path: Path) -> None:
    events = [
        _event(when=NOW - timedelta(hours=3), tokens=30),
        _event(when=NOW - timedelta(hours=1), tokens=10),
        _event(when=NOW - timedelta(hours=2), tokens=20),
    ]
    client = _make_client(events=events, tmp_path=tmp_path)
    res = client.get("/api/events")
    assert res.status_code == 200
    returned = res.json()["events"]
    assert [e["input_tokens"] for e in returned] == [10, 20, 30]


def test_daily_endpoint_returns_14_buckets(tmp_path: Path) -> None:
    client = _make_client(tmp_path=tmp_path)
    res = client.get("/api/daily")
    assert res.status_code == 200
    bars = res.json()["bars"]
    assert len(bars) == 14
    assert bars[-1]["date"] == NOW.date().isoformat()


def test_index_renders_html_with_payload(tmp_path: Path) -> None:
    events = [_event(when=NOW - timedelta(hours=1))]
    client = _make_client(events=events, tmp_path=tmp_path)
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    body = res.text
    assert "Tokie" in body
    assert "daily-chart" in body
    assert "tokieDashboard" in body
    assert "Claude Pro" in body


def test_index_shows_empty_state_when_no_events(tmp_path: Path) -> None:
    client = _make_client(tmp_path=tmp_path)
    res = client.get("/")
    assert res.status_code == 200
    assert "No usage yet" in res.text


def test_index_never_leaks_db_absolute_path(tmp_path: Path) -> None:
    events = [_event(when=NOW - timedelta(hours=1))]
    client = _make_client(events=events, tmp_path=tmp_path)
    res = client.get("/")
    body = res.text
    assert str(tmp_path) not in body  # config paths are not embedded in the HTML


def test_run_refuses_non_loopback_without_allow_remote() -> None:
    with pytest.raises(RuntimeError, match="refusing to bind"):
        run(host="0.0.0.0", allow_remote=False)


def test_run_accepts_localhost_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, object] = {}

    def fake_uvicorn_run(app: object, host: str, port: int, log_level: str) -> None:
        called.update(app=app, host=host, port=port)

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", fake_uvicorn_run)
    monkeypatch.setattr(
        "tokie_cli.dashboard.server.load_config",
        lambda: TokieConfig(
            db_path=Path("/tmp/ignored.db"), audit_log_path=Path("/tmp/ignored.log")
        ),
    )
    run(host="localhost", port=9999)
    assert called["host"] == "localhost"
    assert called["port"] == 9999
