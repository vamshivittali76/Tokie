"""Task-to-tool routing: loader, recommender, handoff extractor.

This package is the Week 4 "suggest the next tool" surface. Everything
here is deterministic and pure (no network, no LLM call); the recommender
ranks user-owned subscriptions against a hand-curated matrix shipped in
``task_routing.yaml`` and the handoff extractor serialises the last N
usage events into a paste-ready briefing.

The three submodules are:

* :mod:`tokie_cli.routing.table`       — load ``task_routing.yaml``.
* :mod:`tokie_cli.routing.recommender` — rank subscriptions per task type.
* :mod:`tokie_cli.routing.handoff`     — extract a paste-ready briefing
  from recent :class:`UsageEvent` rows.

Callers usually import :func:`recommend` and :func:`build_handoff` and
ignore the submodule layout.
"""

from __future__ import annotations

from tokie_cli.routing.auto_handoff import HandoffSuggestion, suggest_alternatives
from tokie_cli.routing.handoff import (
    HandoffBrief,
    HandoffEvent,
    build_handoff,
    render_handoff,
)
from tokie_cli.routing.recommender import (
    Recommendation,
    RecommendationResult,
    available_task_types,
    recommend,
)
from tokie_cli.routing.table import (
    DEFAULT_ROUTING_FILENAME,
    RoutingTable,
    RoutingTableError,
    TaskEntry,
    TaskRecommendationEntry,
    ToolEntry,
    bundled_routing_path,
    load_routing_table,
)

__all__ = [
    "DEFAULT_ROUTING_FILENAME",
    "HandoffBrief",
    "HandoffEvent",
    "HandoffSuggestion",
    "Recommendation",
    "RecommendationResult",
    "RoutingTable",
    "RoutingTableError",
    "TaskEntry",
    "TaskRecommendationEntry",
    "ToolEntry",
    "available_task_types",
    "build_handoff",
    "bundled_routing_path",
    "load_routing_table",
    "recommend",
    "render_handoff",
    "suggest_alternatives",
]
