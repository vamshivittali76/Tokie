"""Pure, transport-agnostic MCP tool handlers.

Split from :mod:`tokie_cli.mcp_server.server` so every handler can be
unit-tested by calling the function directly with a dict — no stdio,
no MCP SDK import, no async transport plumbing.

Each handler returns a JSON-serialisable dict. The stdio adapter in
:mod:`server` wraps that dict in an MCP ``TextContent`` block. This
separation exists because the MCP SDK's own ``Tool`` / ``CallToolResult``
types evolve faster than the handler contract needs to, and we want
unit tests to stay stable across SDK upgrades.

Design notes
------------
- **Read-only.** Handlers never call ``save_config``, ``insert_events``,
  or any network-bound code.
- **Per-call state.** Each handler rebuilds the dashboard payload from
  the config + DB on every invocation. MCP clients are typically LLM
  agents that don't care about microsecond latency, and a stale cache
  confusing an agent into the wrong recommendation would cost us far
  more than the extra I/O.
- **Structured errors.** Unknown tool names raise :class:`ToolNotFound`;
  malformed arguments raise :class:`ToolArgumentError`. The stdio
  adapter maps both to MCP error responses.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from typing import Any

from tokie_cli.config import TokieConfig, load_config
from tokie_cli.dashboard.aggregator import DashboardPayload, build_payload
from tokie_cli.db import connect, migrate, query_events
from tokie_cli.plans import PlanTemplate, load_plans
from tokie_cli.routing import (
    load_routing_table,
    recommend,
    suggest_alternatives,
)
from tokie_cli.schema import UsageEvent


class ToolNotFoundError(LookupError):
    """Raised when the client calls a tool name the server does not expose."""


class ToolArgumentError(ValueError):
    """Raised when tool arguments fail structural validation."""


def _default_events_loader(config: TokieConfig) -> list[UsageEvent]:
    """Load every recorded event from the configured SQLite DB.

    Returns an empty list when the DB does not exist yet — the MCP
    server must start cleanly even on a fresh install.
    """

    if not config.db_path.exists():
        return []
    conn = connect(config.db_path)
    try:
        migrate(conn)
        return list(query_events(conn))
    finally:
        conn.close()


def _jsonable(value: Any) -> Any:
    """Recursively convert dataclasses / datetimes into JSON-ready data."""

    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, tuple | list):
        return [_jsonable(v) for v in value]
    return value


def build_tool_catalog() -> list[dict[str, Any]]:
    """Return every tool definition as a JSON-serialisable dict.

    Used by both the MCP server (to populate ``list_tools``) and the
    test suite (to assert the schema surface). The MCP ``Tool`` type
    accepts this exact shape via keyword arguments, so the adapter in
    :mod:`server` only has to call ``Tool(**defn)`` for each entry.
    """

    return [
        {
            "name": "list_subscriptions",
            "description": (
                "List every configured Tokie subscription with its current "
                "saturation, remaining capacity, and window reset times. "
                "Read-only."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "get_usage",
            "description": (
                "Return aggregated usage (tokens, messages, cost USD) for a "
                "subscription. Omit plan_id + account_id to get totals for "
                "every configured subscription."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "plan_id": {
                        "type": ["string", "null"],
                        "description": "Plan id (e.g. 'claude_pro_personal'). "
                        "Omit or pass null to aggregate across all plans.",
                    },
                    "account_id": {
                        "type": ["string", "null"],
                        "description": "Account id. Omit to aggregate across "
                        "every account bound to the selected plan.",
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "get_remaining",
            "description": (
                "Return remaining capacity per window for a subscription "
                "(or every subscription when no filters are set)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "plan_id": {"type": ["string", "null"]},
                    "account_id": {"type": ["string", "null"]},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "suggest_tool",
            "description": (
                "Deterministically rank the user's subscriptions against a "
                "task type from task_routing.yaml. Returns the ranked list "
                "plus any auto-handoff suggestions when a sub is over its "
                "threshold. No LLM call, no randomness."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": (
                            "Task type id (e.g. 'code_generation', "
                            "'research'). Use list_subscriptions + the "
                            "routing catalog for allowed ids."
                        ),
                    }
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
    ]


def _build_payload(
    *,
    config: TokieConfig | None = None,
    plans_loader: Callable[[], Sequence[PlanTemplate]] | None = None,
    events_loader: Callable[[TokieConfig], Sequence[UsageEvent]] | None = None,
    now: Callable[[], datetime] | None = None,
) -> DashboardPayload:
    cfg = config if config is not None else load_config()
    plans = list((plans_loader or load_plans)())
    events = list((events_loader or _default_events_loader)(cfg))
    return build_payload(
        cfg.subscriptions,
        plans,
        events,
        now=(now or (lambda: datetime.now(tz=UTC)))(),
    )


def _filter_subscriptions(
    payload: DashboardPayload,
    *,
    plan_id: str | None,
    account_id: str | None,
) -> list[Any]:
    return [
        s
        for s in payload.subscriptions
        if (plan_id is None or s.plan_id == plan_id)
        and (account_id is None or s.account_id == account_id)
    ]


def _handle_list_subscriptions(
    _arguments: dict[str, Any],
    **context: Any,
) -> dict[str, Any]:
    payload = _build_payload(**context)
    return {"subscriptions": _jsonable(payload.subscriptions)}


def _handle_get_usage(
    arguments: dict[str, Any],
    **context: Any,
) -> dict[str, Any]:
    plan_id = arguments.get("plan_id") or None
    account_id = arguments.get("account_id") or None
    if plan_id is not None and not isinstance(plan_id, str):
        raise ToolArgumentError("plan_id must be a string or null")
    if account_id is not None and not isinstance(account_id, str):
        raise ToolArgumentError("account_id must be a string or null")

    payload = _build_payload(**context)
    matching = _filter_subscriptions(
        payload, plan_id=plan_id, account_id=account_id
    )
    totals: dict[str, float] = {
        "total_tokens": 0.0,
        "messages": 0.0,
        "cost_usd": 0.0,
    }
    subs: list[dict[str, Any]] = []
    for sub in matching:
        per_sub: dict[str, Any] = {
            "plan_id": sub.plan_id,
            "account_id": sub.account_id,
            "display_name": sub.display_name,
            "total_tokens": 0,
            "messages": 0,
            "cost_usd": 0.0,
            "windows": [],
        }
        for window in sub.windows:
            per_sub["total_tokens"] += int(window.total_tokens)
            per_sub["messages"] += int(window.messages)
            per_sub["cost_usd"] += float(window.cost_usd)
            per_sub["windows"].append(
                {
                    "window_type": window.window_type,
                    "total_tokens": int(window.total_tokens),
                    "messages": int(window.messages),
                    "cost_usd": float(window.cost_usd),
                }
            )
        subs.append(per_sub)
        totals["total_tokens"] += per_sub["total_tokens"]
        totals["messages"] += per_sub["messages"]
        totals["cost_usd"] += per_sub["cost_usd"]
    return {
        "filter": {"plan_id": plan_id, "account_id": account_id},
        "subscriptions": subs,
        "totals": {
            "total_tokens": int(totals["total_tokens"]),
            "messages": int(totals["messages"]),
            "cost_usd": float(totals["cost_usd"]),
        },
    }


def _handle_get_remaining(
    arguments: dict[str, Any],
    **context: Any,
) -> dict[str, Any]:
    plan_id = arguments.get("plan_id") or None
    account_id = arguments.get("account_id") or None
    payload = _build_payload(**context)
    matching = _filter_subscriptions(
        payload, plan_id=plan_id, account_id=account_id
    )
    result: list[dict[str, Any]] = []
    for sub in matching:
        result.append(
            {
                "plan_id": sub.plan_id,
                "account_id": sub.account_id,
                "display_name": sub.display_name,
                "windows": [
                    {
                        "window_type": w.window_type,
                        "limit": w.limit,
                        "used": w.used,
                        "remaining": w.remaining,
                        "pct_used": w.pct_used,
                        "limit_basis": w.limit_basis,
                        "is_over": w.is_over,
                        "resets_at": w.resets_at.isoformat()
                        if w.resets_at is not None
                        else None,
                    }
                    for w in sub.windows
                ],
            }
        )
    return {
        "filter": {"plan_id": plan_id, "account_id": account_id},
        "subscriptions": result,
    }


def _handle_suggest_tool(
    arguments: dict[str, Any],
    **context: Any,
) -> dict[str, Any]:
    task_id = arguments.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ToolArgumentError("task_id must be a non-empty string")

    try:
        table = load_routing_table()
    except Exception as exc:  # surface load errors as structured failures
        raise ToolArgumentError(f"failed to load routing table: {exc}") from exc
    try:
        table.task(task_id)
    except KeyError as exc:
        raise ToolArgumentError(str(exc)) from exc

    payload = _build_payload(**context)
    result = recommend(
        task_id=task_id,
        table=table,
        subscriptions=payload.subscriptions,
    )

    # Auto-handoff hints are useful for the agent even in steady state,
    # but we only populate them if the caller also has thresholds
    # configured — otherwise there are no crossings to react to.
    from tokie_cli.alerts.thresholds import (
        ThresholdRule,
        evaluate_thresholds,
    )

    cfg = context.get("config") or load_config()
    rules = [
        ThresholdRule(
            plan_id=r.plan_id,
            account_id=r.account_id,
            levels=tuple(r.levels),
            channels=tuple(r.channels),
        )
        for r in cfg.thresholds
    ]
    armed = evaluate_thresholds(payload.subscriptions, rules)
    suggestions = suggest_alternatives(
        crossings=armed,
        subscriptions=payload.subscriptions,
        table=table,
        fallback_task=task_id,
    )

    return {
        "task_id": result.task_id,
        "description": result.task_description,
        "recommendations": [
            {
                "tool_id": r.tool_id,
                "tool_display_name": r.tool_display_name,
                "plan_id": r.plan_id,
                "plan_display_name": r.plan_display_name,
                "account_id": r.account_id,
                "product": r.product,
                "tier": r.tier,
                "rationale": r.rationale,
                "saturation": r.saturation,
                "remaining_fraction": r.remaining_fraction,
                "worst_window_type": r.worst_window_type,
                "is_over": r.is_over,
            }
            for r in result.recommendations
        ],
        "missing_tools": list(result.missing_tools),
        "handoff_suggestions": [
            {
                "saturated_plan_id": s.saturated_plan_id,
                "saturated_display_name": s.saturated_display_name,
                "threshold_pct": s.threshold_pct,
                "alternative": (
                    None
                    if s.alternative is None
                    else {
                        "tool_id": s.alternative.tool_id,
                        "tool_display_name": s.alternative.tool_display_name,
                        "plan_id": s.alternative.plan_id,
                        "account_id": s.alternative.account_id,
                        "tier": s.alternative.tier,
                        "rationale": s.alternative.rationale,
                    }
                ),
                "reason": s.reason,
            }
            for s in suggestions
        ],
    }


_DISPATCH: dict[
    str,
    Callable[..., dict[str, Any]],
] = {
    "list_subscriptions": _handle_list_subscriptions,
    "get_usage": _handle_get_usage,
    "get_remaining": _handle_get_remaining,
    "suggest_tool": _handle_suggest_tool,
}


def handle_call_tool(
    name: str,
    arguments: dict[str, Any] | None,
    **context: Any,
) -> dict[str, Any]:
    """Dispatch ``name`` to its handler. The entry point used by the adapter.

    ``context`` lets tests inject ``config=``, ``plans_loader=``,
    ``events_loader=``, and ``now=``. Production callers omit
    everything and get real config + DB + plan catalog.
    """

    handler = _DISPATCH.get(name)
    if handler is None:
        raise ToolNotFoundError(f"unknown tool: {name}")
    return handler(arguments or {}, **context)


__all__ = [
    "ToolArgumentError",
    "ToolNotFoundError",
    "build_tool_catalog",
    "handle_call_tool",
]
