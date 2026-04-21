"""Bridge from alert crossings to recommended next tools.

The alert engine (:mod:`tokie_cli.alerts.engine`) is deliberately
routing-agnostic: it knows *when* a threshold is crossed but has no
opinion about *where the user should go next*. This module closes the
loop by taking a list of :class:`ThresholdCrossing`s and the user's
:class:`SubscriptionView`s, then returning a list of
:class:`HandoffSuggestion`s — one per over-limit crossing — containing
the top alternative subscription the routing table knows about.

Keeping this bridge out of the alert engine avoids a circular import
(alerts -> routing -> aggregator -> alerts) and keeps the "suggest an
alternative" logic optional: callers that only want banner lines can
ignore it entirely.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from tokie_cli.alerts.thresholds import ThresholdCrossing
from tokie_cli.dashboard.aggregator import SubscriptionView
from tokie_cli.routing.recommender import Recommendation, recommend
from tokie_cli.routing.table import RoutingTable

__all__ = [
    "HandoffSuggestion",
    "suggest_alternatives",
]


@dataclass(frozen=True)
class HandoffSuggestion:
    """One "your X is saturated, try Y" recommendation."""

    saturated_plan_id: str
    saturated_account_id: str
    saturated_display_name: str
    threshold_pct: int
    task_id: str | None
    alternative: Recommendation | None
    reason: str


def suggest_alternatives(
    *,
    crossings: Sequence[ThresholdCrossing],
    subscriptions: Sequence[SubscriptionView],
    table: RoutingTable,
    fallback_task: str = "code_generation",
    only_over: bool = True,
) -> tuple[HandoffSuggestion, ...]:
    """Produce one suggestion per qualifying crossing.

    By default we only suggest alternatives when the user is already
    *over* their limit (``threshold_pct == 100`` *and* ``is_over``) so
    the banner stays calm at 75% and 95%. Set ``only_over=False`` to
    also suggest at 95%+ crossings — useful for the "warn me before I
    blow through the last mile" flow.

    ``fallback_task`` is used when we have no task signal from the
    caller: it anchors recommendations to the most common "I'm coding"
    pool, which is the dominant use-case for v0.4. Callers that know
    the task can iterate this function themselves with different
    ``fallback_task`` values.
    """

    if not crossings:
        return ()

    try:
        table.task(fallback_task)
    except KeyError:
        return ()

    by_key: dict[tuple[str, str, int], ThresholdCrossing] = {}
    for crossing in crossings:
        is_over = crossing.pct_used >= 1.0 or crossing.threshold_pct >= 100
        if only_over and not is_over:
            continue
        key = (crossing.plan_id, crossing.account_id, crossing.threshold_pct)
        prev = by_key.get(key)
        if prev is None or crossing.pct_used > prev.pct_used:
            by_key[key] = crossing

    if not by_key:
        return ()

    result = recommend(
        task_id=fallback_task, table=table, subscriptions=subscriptions
    )
    ranked = [r for r in result.recommendations if not r.is_over]

    suggestions: list[HandoffSuggestion] = []
    for crossing in by_key.values():
        alt: Recommendation | None = next(
            (
                r
                for r in ranked
                if not (
                    r.plan_id == crossing.plan_id
                    and r.account_id == crossing.account_id
                )
            ),
            None,
        )
        if alt is None:
            reason = (
                f"{crossing.display_name} is at {crossing.pct_used * 100:.0f}% — "
                "no alternative subscription is registered yet."
            )
        else:
            reason = (
                f"{crossing.display_name} hit {crossing.pct_used * 100:.0f}% of its "
                f"{crossing.window_type} limit; "
                f"{alt.tool_display_name} ({alt.plan_display_name}) has "
                f"{alt.remaining_fraction * 100:.0f}% headroom."
            )
        suggestions.append(
            HandoffSuggestion(
                saturated_plan_id=crossing.plan_id,
                saturated_account_id=crossing.account_id,
                saturated_display_name=crossing.display_name,
                threshold_pct=crossing.threshold_pct,
                task_id=fallback_task,
                alternative=alt,
                reason=reason,
            )
        )
    return tuple(suggestions)
