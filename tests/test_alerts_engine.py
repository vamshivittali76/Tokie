"""End-to-end tests for :func:`tokie_cli.alerts.check_alerts`.

We seed a real SQLite database with synthetic events, point a ``TokieConfig``
at it, and drive :func:`check_alerts` via its public injection points. No
network, no keyring, no desktop side effects.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tokie_cli.alerts import check_alerts
from tokie_cli.alerts.channels import WebhookConfig
from tokie_cli.alerts.thresholds import ThresholdRule
from tokie_cli.config import (
    SubscriptionBinding,
    ThresholdRuleConfig,
    TokieConfig,
)
from tokie_cli.db import connect, insert_events, migrate
from tokie_cli.plans import load_plans
from tokie_cli.schema import Confidence, UsageEvent


def _event(
    *,
    occurred_at: datetime,
    provider: str,
    product: str,
    input_tokens: int = 100,
    output_tokens: int = 50,
    account_id: str = "default",
    model: str = "claude-3-sonnet-20240229",
    source_suffix: str = "a",
) -> UsageEvent:
    raw = f"{occurred_at.isoformat()}:{provider}:{product}:{source_suffix}"
    return UsageEvent(
        id=hashlib.sha256(raw.encode()).hexdigest()[:32],
        collected_at=occurred_at,
        occurred_at=occurred_at,
        provider=provider,
        product=product,
        account_id=account_id,
        session_id=None,
        project=None,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=0,
        cache_write_tokens=0,
        reasoning_tokens=0,
        cost_usd=None,
        confidence=Confidence.EXACT,
        source=f"test-{source_suffix}",
        raw_hash=hashlib.sha256(raw.encode()).hexdigest(),
    )


def _find_claude_pro_plan() -> str:
    """Pick any bundled plan whose subscription has a concrete quota window.

    Tests should not hardcode a specific plan ID — the bundled catalog can
    change — so we search for a plan with at least one non-none window and
    return its ID. Fail loudly if none exist, because that would mean the
    catalog is empty and the whole alerting system is untestable.
    """

    for plan in load_plans():
        for window in plan.subscription.windows:
            if window.window_type.value == "none":
                continue
            has_limit = (
                window.limit_tokens is not None
                or window.limit_messages is not None
                or window.limit_usd is not None
            )
            if has_limit:
                return plan.id
    raise RuntimeError("no bundled plan with a real quota window")


def _config_for_db(db_path: Path, *, binding: SubscriptionBinding) -> TokieConfig:
    return TokieConfig(
        db_path=db_path,
        audit_log_path=db_path.parent / "audit.log",
        subscriptions=(binding,),
    )


def _seed_events(
    db_path: Path,
    events: list[UsageEvent],
) -> None:
    conn = connect(db_path)
    try:
        migrate(conn)
        insert_events(conn, events)
    finally:
        conn.close()


def _burst(
    *,
    plan_id: str,
    now: datetime,
    minutes: int = 30,
    events_count: int = 50,
    tokens_each: int = 10_000,
) -> list[UsageEvent]:
    """Generate a burst of events shaped to flow through the plan's own windows.

    Uses the first product from ``shared_with`` (when present) so Anthropic-
    style shared-quota plans count our synthetic events; otherwise falls back
    to the subscription's canonical product.
    """

    plans = {p.id: p for p in load_plans()}
    plan = plans[plan_id]
    sub = plan.subscription
    product = sub.product
    for window in sub.windows:
        if window.shared_with:
            product = window.shared_with[0]
            break
    out: list[UsageEvent] = []
    for i in range(events_count):
        ts = now - timedelta(minutes=minutes - (i * minutes // max(events_count, 1)))
        out.append(
            _event(
                occurred_at=ts,
                provider=sub.provider,
                product=product,
                input_tokens=tokens_each,
                output_tokens=0,
                source_suffix=f"burst-{i}",
            )
        )
    return out


def test_check_alerts_empty_db_returns_empty(tmp_path: Path) -> None:
    cfg = TokieConfig(
        db_path=tmp_path / "missing.db",
        audit_log_path=tmp_path / "audit.log",
    )
    result = check_alerts(cfg)
    assert result.armed == ()
    assert result.fired == ()
    assert result.banner_lines == ()


def test_check_alerts_reports_armed_but_not_fired_when_dry_run(tmp_path: Path) -> None:
    plan_id = _find_claude_pro_plan()
    binding = SubscriptionBinding(plan_id=plan_id, account_id="default")
    db_path = tmp_path / "tokie.db"
    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    _seed_events(db_path, _burst(plan_id=plan_id, now=now))

    cfg = _config_for_db(db_path, binding=binding)
    result = check_alerts(
        cfg,
        rules=[ThresholdRule(levels=(1,), channels=("banner",))],
        dry_run=True,
        now=now,
    )
    assert result.armed  # something crossed 1%
    # Dry-run still records fires (dedupe semantics preserved), but skips dispatch.
    assert result.dispatch_results == ()


def test_check_alerts_dispatches_only_new_crossings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_id = _find_claude_pro_plan()
    binding = SubscriptionBinding(plan_id=plan_id, account_id="default")
    db_path = tmp_path / "tokie.db"
    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    _seed_events(db_path, _burst(plan_id=plan_id, now=now, tokens_each=100_000))

    cfg = _config_for_db(db_path, binding=binding)
    rules = [ThresholdRule(levels=(1,), channels=("banner",))]

    # First run: should dispatch at least one fire.
    first = check_alerts(cfg, rules=rules, now=now)
    assert first.fired
    assert first.dispatch_results
    # Second run with no new events: dedupe means no new fires.
    second = check_alerts(cfg, rules=rules, now=now + timedelta(seconds=1))
    assert second.armed == first.armed
    assert second.fired == ()
    assert second.dispatch_results == ()


def test_check_alerts_falls_back_to_config_thresholds(tmp_path: Path) -> None:
    plan_id = _find_claude_pro_plan()
    binding = SubscriptionBinding(plan_id=plan_id, account_id="default")
    db_path = tmp_path / "tokie.db"
    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    _seed_events(db_path, _burst(plan_id=plan_id, now=now))

    cfg = TokieConfig(
        db_path=db_path,
        audit_log_path=tmp_path / "audit.log",
        subscriptions=(binding,),
        thresholds=(
            ThresholdRuleConfig(levels=(1,), channels=("banner",)),
        ),
    )
    result = check_alerts(cfg, dry_run=True, now=now)
    assert result.armed  # crossed the 1% line via config-driven rules


def test_check_alerts_dispatches_webhook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_id = _find_claude_pro_plan()
    binding = SubscriptionBinding(plan_id=plan_id, account_id="default")
    db_path = tmp_path / "tokie.db"
    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    _seed_events(db_path, _burst(plan_id=plan_id, now=now, tokens_each=100_000))

    cfg = _config_for_db(db_path, binding=binding)

    posts: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200

    def fake_post(
        url: str,
        *,
        content: bytes,
        headers: dict[str, str],
        timeout: float,
    ) -> FakeResponse:
        posts.append({"url": url, "content": content, "headers": headers})
        return FakeResponse()

    monkeypatch.setattr("httpx.post", fake_post)
    from tokie_cli.alerts import channels as ch_mod

    monkeypatch.setattr(
        ch_mod,
        "_load_webhook_url",
        lambda name: "https://hooks.slack.com/fake",
    )

    rules = [
        ThresholdRule(
            levels=(1,),
            channels=("banner", "webhook:team"),
        )
    ]
    result = check_alerts(
        cfg,
        rules=rules,
        webhook_configs=[WebhookConfig(name="team", format="slack")],
        now=now,
    )
    assert result.fired
    # At least one dispatch to webhook:team
    assert any(r.channel == "webhook:team" and r.ok for r in result.dispatch_results)
    assert any(p["url"] == "https://hooks.slack.com/fake" for p in posts)


def test_check_alerts_unknown_channel_surfaces_error(tmp_path: Path) -> None:
    plan_id = _find_claude_pro_plan()
    binding = SubscriptionBinding(plan_id=plan_id, account_id="default")
    db_path = tmp_path / "tokie.db"
    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    _seed_events(db_path, _burst(plan_id=plan_id, now=now, tokens_each=100_000))

    cfg = _config_for_db(db_path, binding=binding)
    rules = [ThresholdRule(levels=(1,), channels=("webhook:nonexistent",))]
    result = check_alerts(cfg, rules=rules, now=now)
    assert any(
        r.channel == "webhook:nonexistent" and not r.ok
        for r in result.dispatch_results
    )


def test_check_alerts_records_fires_persistently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tokie_cli.alerts.storage import AlertStorage, connect_alerts

    plan_id = _find_claude_pro_plan()
    binding = SubscriptionBinding(plan_id=plan_id, account_id="default")
    db_path = tmp_path / "tokie.db"
    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    _seed_events(db_path, _burst(plan_id=plan_id, now=now, tokens_each=100_000))

    cfg = _config_for_db(db_path, binding=binding)
    rules = [ThresholdRule(levels=(1,), channels=("banner",))]
    check_alerts(cfg, rules=rules, now=now)

    conn = connect_alerts(db_path)
    try:
        storage = AlertStorage(conn)
        assert len(storage.recent_fires()) >= 1
    finally:
        conn.close()
