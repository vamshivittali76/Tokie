"""Unit tests for :mod:`tokie_cli.mcp_server.handlers`.

We test the pure handler layer without spinning up stdio — each handler
is a straight function that takes arguments + injected context and
returns a JSON-ready dict. The MCP SDK itself is exercised separately
in ``test_mcp_server.py``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tokie_cli.config import SubscriptionBinding, TokieConfig
from tokie_cli.mcp_server.handlers import (
    ToolArgumentError,
    ToolNotFoundError,
    build_tool_catalog,
    handle_call_tool,
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
    when: datetime = NOW,
    provider: str = "anthropic",
    product: str = "claude-code",
    tokens: int = 100,
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
        input_tokens=tokens,
        output_tokens=tokens // 2,
        confidence=Confidence.EXACT,
        source="test",
        raw_hash=raw,
    )


def _plan(
    plan_id: str,
    product: str,
    *,
    provider: str = "anthropic",
    shared: list[str] | None = None,
) -> PlanTemplate:
    return PlanTemplate(
        id=plan_id,
        display_name=plan_id.replace("_", " ").title(),
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


def _context(
    *,
    tmp_path: Path,
    events: Sequence[UsageEvent] = (),
    plans: Sequence[PlanTemplate] = (),
    bindings: tuple[SubscriptionBinding, ...] = (
        SubscriptionBinding(plan_id="claude_pro_personal", account_id="default"),
    ),
) -> dict[str, object]:
    config = TokieConfig(
        db_path=tmp_path / "tokie.db",
        audit_log_path=tmp_path / "audit.log",
        subscriptions=bindings,
    )
    return {
        "config": config,
        "plans_loader": lambda: list(plans),
        "events_loader": lambda _cfg: list(events),
        "now": lambda: NOW,
    }


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def test_build_tool_catalog_lists_every_tool() -> None:
    names = {t["name"] for t in build_tool_catalog()}
    assert names == {
        "list_subscriptions",
        "get_usage",
        "get_remaining",
        "suggest_tool",
    }


def test_build_tool_catalog_has_schemas() -> None:
    for tool in build_tool_catalog():
        assert tool["description"].strip()
        assert tool["inputSchema"]["type"] == "object"


# ---------------------------------------------------------------------------
# list_subscriptions
# ---------------------------------------------------------------------------


def test_list_subscriptions_returns_configured_plans(tmp_path: Path) -> None:
    ctx = _context(
        tmp_path=tmp_path,
        plans=[_plan("claude_pro_personal", "claude-code")],
    )
    result = handle_call_tool("list_subscriptions", None, **ctx)
    assert len(result["subscriptions"]) == 1
    sub = result["subscriptions"][0]
    assert sub["plan_id"] == "claude_pro_personal"
    assert sub["account_id"] == "default"


# ---------------------------------------------------------------------------
# get_usage
# ---------------------------------------------------------------------------


def test_get_usage_totals_match_events(tmp_path: Path) -> None:
    events = [_event(when=NOW - timedelta(minutes=i)) for i in range(5)]
    ctx = _context(
        tmp_path=tmp_path,
        events=events,
        plans=[_plan("claude_pro_personal", "claude-code")],
    )
    result = handle_call_tool("get_usage", {}, **ctx)
    assert result["totals"]["messages"] == 5
    assert len(result["subscriptions"]) == 1


def test_get_usage_filters_by_plan(tmp_path: Path) -> None:
    ctx = _context(
        tmp_path=tmp_path,
        plans=[_plan("claude_pro_personal", "claude-code")],
    )
    result = handle_call_tool(
        "get_usage", {"plan_id": "nonexistent"}, **ctx
    )
    assert result["subscriptions"] == []
    assert result["filter"]["plan_id"] == "nonexistent"


def test_get_usage_rejects_non_string_plan_id(tmp_path: Path) -> None:
    ctx = _context(tmp_path=tmp_path)
    with pytest.raises(ToolArgumentError, match="plan_id"):
        handle_call_tool("get_usage", {"plan_id": 42}, **ctx)


# ---------------------------------------------------------------------------
# get_remaining
# ---------------------------------------------------------------------------


def test_get_remaining_reports_window_capacity(tmp_path: Path) -> None:
    events = [_event(when=NOW - timedelta(minutes=i)) for i in range(10)]
    ctx = _context(
        tmp_path=tmp_path,
        events=events,
        plans=[
            _plan(
                "claude_pro_personal",
                "claude-code",
                shared=["claude-code", "claude-web"],
            )
        ],
    )
    result = handle_call_tool("get_remaining", {}, **ctx)
    sub = result["subscriptions"][0]
    win = sub["windows"][0]
    assert win["window_type"] == "rolling_5h"
    assert win["limit"] == 45.0
    assert win["used"] == 10.0
    assert win["remaining"] == 35.0


# ---------------------------------------------------------------------------
# suggest_tool
# ---------------------------------------------------------------------------


def test_suggest_tool_returns_recommendations(tmp_path: Path) -> None:
    ctx = _context(
        tmp_path=tmp_path,
        plans=[_plan("claude_pro_personal", "claude-code")],
    )
    result = handle_call_tool(
        "suggest_tool", {"task_id": "code_generation"}, **ctx
    )
    assert result["task_id"] == "code_generation"
    assert result["recommendations"]
    assert result["recommendations"][0]["plan_id"] == "claude_pro_personal"


def test_suggest_tool_rejects_unknown_task(tmp_path: Path) -> None:
    ctx = _context(tmp_path=tmp_path)
    with pytest.raises(ToolArgumentError):
        handle_call_tool("suggest_tool", {"task_id": "does-not-exist"}, **ctx)


def test_suggest_tool_rejects_empty_task_id(tmp_path: Path) -> None:
    ctx = _context(tmp_path=tmp_path)
    with pytest.raises(ToolArgumentError, match="non-empty"):
        handle_call_tool("suggest_tool", {"task_id": ""}, **ctx)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_unknown_tool_raises_tool_not_found(tmp_path: Path) -> None:
    ctx = _context(tmp_path=tmp_path)
    with pytest.raises(ToolNotFoundError, match="unknown tool"):
        handle_call_tool("not_a_real_tool", {}, **ctx)
