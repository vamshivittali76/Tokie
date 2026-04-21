"""Deterministic task-to-subscription recommender.

Given:

* a :class:`RoutingTable` (``task_routing.yaml``),
* the user's active subscriptions (``SubscriptionView`` from the
  dashboard aggregator), and
* a task type id (e.g. ``code_generation``),

produce a ranked list of :class:`Recommendation`s: which subscription to
use *right now*, with a reason and the current worst-window saturation.

Algorithm:

1. Look up the task's ``preferred`` list in the routing table.
2. For every recommendation, find all subscriptions that satisfy the
   underlying tool (either their ``product`` or one of their window's
   ``shared_with`` products matches the tool's ``products`` set).
3. Compute each candidate's "saturation": the highest ``pct_used`` among
   the subscription's windows (``is_over`` counts as 2.0 so it sorts
   below everything else). Subscriptions without a real limit
   (``window_type == "none"``) get ``0.0``.
4. Sort: first by routing tier (ascending), then by saturation
   (ascending), then by remaining-fraction (descending) as a tiebreak.
5. Attach a human-readable reason to each entry.

There is zero LLM call and zero randomness: two identical inputs always
produce the same ordering. That keeps the recommender auditable and
keeps "I asked Tokie what to use" conversations reproducible.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from tokie_cli.dashboard.aggregator import SubscriptionView, WindowView
from tokie_cli.routing.table import RoutingTable, TaskRecommendationEntry, ToolEntry

__all__ = [
    "Recommendation",
    "RecommendationResult",
    "available_task_types",
    "recommend",
]


_OVER_PENALTY: float = 2.0
"""Saturation value used for windows already over their limit.

Anything over-limit should sort after every "still has room" option, but
ahead of "unknown / no limit" so the UI still surfaces it as a candidate
(the user may explicitly want to burn the remaining allowance).
"""


@dataclass(frozen=True)
class Recommendation:
    """One ranked suggestion for a given task."""

    tool_id: str
    tool_display_name: str
    plan_id: str
    plan_display_name: str
    account_id: str
    product: str
    tier: int
    rationale: str
    saturation: float
    remaining_fraction: float
    worst_window_type: str
    is_over: bool

    @property
    def score(self) -> tuple[int, float, float]:
        """Lexicographic sort key: tier asc, saturation asc, -remaining asc."""

        return (self.tier, self.saturation, -self.remaining_fraction)


@dataclass(frozen=True)
class RecommendationResult:
    """Output of :func:`recommend`. Always safe to serialise."""

    task_id: str
    task_description: str
    recommendations: tuple[Recommendation, ...]
    missing_tools: tuple[str, ...]


def available_task_types(table: RoutingTable) -> tuple[str, ...]:
    """Sorted list of task ids the routing table supports."""

    return tuple(sorted(t.id for t in table.tasks))


def recommend(
    *,
    task_id: str,
    table: RoutingTable,
    subscriptions: Sequence[SubscriptionView],
) -> RecommendationResult:
    """Return a ranked recommendation list for ``task_id``.

    ``subscriptions`` is the output of
    :func:`tokie_cli.dashboard.aggregator.build_subscription_views`. Pass
    the already-aggregated list so the recommender stays a pure function
    and can be unit-tested with fake data.
    """

    task = table.task(task_id)
    tool_index = {tool.id: tool for tool in table.tools}

    recs: list[Recommendation] = []
    missing: list[str] = []
    seen_keys: set[tuple[str, str, str]] = set()

    for entry in task.preferred:
        tool = tool_index.get(entry.tool_id)
        if tool is None:
            missing.append(entry.tool_id)
            continue

        matches = [sub for sub in subscriptions if _subscription_satisfies(sub, tool)]
        if not matches:
            missing.append(entry.tool_id)
            continue

        for sub in matches:
            key = (tool.id, sub.plan_id, sub.account_id)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            saturation, remaining, worst_type, is_over = _worst_window(sub.windows)
            recs.append(
                Recommendation(
                    tool_id=tool.id,
                    tool_display_name=tool.display_name,
                    plan_id=sub.plan_id,
                    plan_display_name=sub.display_name,
                    account_id=sub.account_id,
                    product=sub.product,
                    tier=entry.tier,
                    rationale=_compose_rationale(entry, sub, is_over),
                    saturation=saturation,
                    remaining_fraction=remaining,
                    worst_window_type=worst_type,
                    is_over=is_over,
                )
            )

    ranked = tuple(sorted(recs, key=lambda r: r.score))
    return RecommendationResult(
        task_id=task.id,
        task_description=task.description,
        recommendations=ranked,
        missing_tools=tuple(dict.fromkeys(missing)),
    )


def _subscription_satisfies(sub: SubscriptionView, tool: ToolEntry) -> bool:
    """True when ``sub`` can actually run ``tool``."""

    products = set(tool.products)
    if sub.product in products:
        return True
    return any(
        any(p in products for p in window.shared_with) for window in sub.windows
    )


def _worst_window(windows: Iterable[WindowView]) -> tuple[float, float, str, bool]:
    """Return ``(saturation, remaining_fraction, window_type, is_over)``.

    ``saturation`` is ``pct_used`` for the busiest metered window, or
    :data:`_OVER_PENALTY` for an over-limit one. Unlimited subscriptions
    (``window_type == "none"`` or ``limit`` is ``None``) default to
    ``0.0`` saturation and ``1.0`` remaining so they float to the top of
    the tier they occupy.
    """

    worst_pct = -1.0
    worst_remaining = 1.0
    worst_type = "none"
    is_over = False

    for window in windows:
        if window.limit is None:
            continue
        pct = window.pct_used
        if window.is_over:
            pct = _OVER_PENALTY
        if pct > worst_pct:
            worst_pct = pct
            worst_remaining = (
                max(0.0, 1.0 - window.pct_used) if not window.is_over else 0.0
            )
            worst_type = window.window_type
            is_over = window.is_over

    if worst_pct < 0:
        return (0.0, 1.0, "none", False)
    return (worst_pct, worst_remaining, worst_type, is_over)


def _compose_rationale(
    entry: TaskRecommendationEntry,
    sub: SubscriptionView,
    is_over: bool,
) -> str:
    """Blend the routing rationale with the subscription's current state."""

    base = entry.rationale or f"Tier-{entry.tier} pick for this task."
    if is_over:
        return f"{base} (over limit — only use if you accept overage)"
    return base
