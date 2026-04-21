"""Dashboard aggregation layer.

Pure functions that turn raw :class:`UsageEvent` rows + bundled plan
templates + user-chosen :class:`SubscriptionBinding`\\ s into the
view-models the FastAPI routes serialize as JSON and the HTMX template
renders into progress bars.

Nothing here talks to the DB, FastAPI, or the filesystem. That keeps the
math trivial to unit-test and cheap to re-run on every HTTP request.

Source: section 7 (window math), section 11.1 (plans.yaml shape), and
section 4 (confidence-tier rendering) of TOKIE_DEVELOPMENT_PLAN_FINAL.md.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from tokie_cli.config import SubscriptionBinding
from tokie_cli.plans import PlanTemplate, Trackability
from tokie_cli.schema import Confidence, LimitWindow, UsageEvent, WindowType
from tokie_cli.windows import (
    aggregate_events,
    capacity,
    window_bounds,
)


@dataclass(frozen=True)
class WindowView:
    """Dashboard-ready view of one :class:`LimitWindow`.

    ``window_type`` is serialized as the StrEnum value (e.g. ``rolling_5h``)
    so the front end can drive styling off the raw string without importing
    the Python enum class.
    """

    window_type: str
    starts_at: datetime | None
    resets_at: datetime | None
    limit_basis: str
    used: float
    limit: float | None
    remaining: float | None
    pct_used: float
    is_over: bool
    shared_with: tuple[str, ...]
    messages: int
    total_tokens: int
    cost_usd: float


@dataclass(frozen=True)
class SubscriptionView:
    """Dashboard-ready view of one configured :class:`SubscriptionBinding`.

    ``confidence`` is the *weakest* confidence across events that contributed
    to any of this subscription's windows — so a single INFERRED event never
    gets silently upgraded to EXACT by a solid bar next to it.
    """

    plan_id: str
    display_name: str
    provider: str
    product: str
    plan: str
    account_id: str
    trackability: str
    confidence: str
    event_count: int
    windows: tuple[WindowView, ...]


@dataclass(frozen=True)
class RecentEventView:
    """Dashboard-ready row for the recent-sessions table."""

    occurred_at: datetime
    provider: str
    product: str
    model: str
    session_id: str | None
    project: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    reasoning_tokens: int
    total_tokens: int
    cost_usd: float | None
    confidence: str
    source: str


@dataclass(frozen=True)
class DailyBar:
    """One bar on the 14-day stacked bar chart."""

    date: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    reasoning_tokens: int
    total_tokens: int
    events: int


@dataclass(frozen=True)
class DashboardPayload:
    """Top-level payload served by ``GET /api/status``."""

    generated_at: datetime
    event_count: int
    subscription_count: int
    provider_breakdown: tuple[dict[str, Any], ...]
    subscriptions: tuple[SubscriptionView, ...]
    recent_events: tuple[RecentEventView, ...]
    daily_bars: tuple[DailyBar, ...]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_RECENT_LIMIT = 25
_DEFAULT_DAYS_BACK = 14


def _confidence_rank(value: Confidence) -> int:
    # Lower number = stronger signal; used to pick the weakest confidence
    # across a set of events ("one inferred row taints the whole window").
    return {
        Confidence.EXACT: 0,
        Confidence.ESTIMATED: 1,
        Confidence.INFERRED: 2,
    }[value]


def _weakest(confidences: Iterable[Confidence]) -> Confidence:
    worst = Confidence.EXACT
    seen = False
    for c in confidences:
        seen = True
        if _confidence_rank(c) > _confidence_rank(worst):
            worst = c
    return worst if seen else Confidence.EXACT


def _filter_relevant(
    events: Iterable[UsageEvent],
    *,
    provider: str,
    account_id: str,
    products: Sequence[str],
) -> list[UsageEvent]:
    """Filter events to those that count against a given subscription window.

    ``products`` is the effective product set: either the window's
    ``shared_with`` list, or a single-element list with the subscription's
    own product when ``shared_with`` is empty.
    """

    product_set = set(products)
    return [
        e
        for e in events
        if e.provider == provider and e.account_id == account_id and e.product in product_set
    ]


def _resolve_session_start(
    window_type: WindowType,
    relevant: Sequence[UsageEvent],
    now: datetime,
) -> datetime:
    """Best-effort session anchor for ROLLING_5H and WEEKLY.

    Anthropic's rolling-5h bucket starts at the user's first message inside
    the past 5 hours; the weekly bucket starts at the first message inside
    the past 7 days. For DAILY / MONTHLY / NONE the anchor is ignored by
    :func:`window_bounds`, so we default to ``now`` as a harmless sentinel.
    """

    if window_type is WindowType.ROLLING_5H:
        cutoff = now - timedelta(hours=5)
    elif window_type is WindowType.WEEKLY:
        cutoff = now - timedelta(days=7)
    else:
        return now

    recent = [e.occurred_at for e in relevant if e.occurred_at >= cutoff]
    return min(recent) if recent else now


def _window_view(
    window: LimitWindow,
    *,
    subscription_product: str,
    subscription_provider: str,
    account_id: str,
    events: Sequence[UsageEvent],
    now: datetime,
) -> tuple[WindowView, list[UsageEvent]]:
    """Build one :class:`WindowView` and return it with the events it used."""

    products: Sequence[str] = window.shared_with or (subscription_product,)
    relevant = _filter_relevant(
        events,
        provider=subscription_provider,
        account_id=account_id,
        products=products,
    )

    if window.window_type is WindowType.NONE:
        totals = aggregate_events(
            relevant,
            start=datetime.min.replace(tzinfo=UTC),
            end=datetime.max.replace(tzinfo=UTC),
        )
        cap = capacity(window, totals)
        return (
            WindowView(
                window_type=window.window_type.value,
                starts_at=None,
                resets_at=None,
                limit_basis=cap.limit_basis,
                used=cap.used,
                limit=cap.limit,
                remaining=cap.remaining,
                pct_used=cap.pct_used,
                is_over=cap.is_over,
                shared_with=tuple(window.shared_with),
                messages=totals.total_messages,
                total_tokens=totals.total_tokens,
                cost_usd=totals.total_cost_usd,
            ),
            list(relevant),
        )

    session_start = _resolve_session_start(window.window_type, relevant, now)
    bounds = window_bounds(window.window_type, session_start, now)
    if bounds is None:  # pragma: no cover - guarded by NONE branch above
        raise RuntimeError(f"unreachable: {window.window_type}")
    start, end = bounds
    totals = aggregate_events(relevant, start=start, end=end)
    cap = capacity(window, totals)
    contributing = [e for e in relevant if start <= e.occurred_at < end]
    return (
        WindowView(
            window_type=window.window_type.value,
            starts_at=start,
            resets_at=end,
            limit_basis=cap.limit_basis,
            used=cap.used,
            limit=cap.limit,
            remaining=cap.remaining,
            pct_used=cap.pct_used,
            is_over=cap.is_over,
            shared_with=tuple(window.shared_with),
            messages=totals.total_messages,
            total_tokens=totals.total_tokens,
            cost_usd=totals.total_cost_usd,
        ),
        contributing,
    )


def build_subscription_views(
    bindings: Sequence[SubscriptionBinding],
    plans: Sequence[PlanTemplate],
    events: Sequence[UsageEvent],
    *,
    now: datetime,
) -> tuple[SubscriptionView, ...]:
    """Render every user-chosen subscription as a :class:`SubscriptionView`.

    Bindings whose ``plan_id`` doesn't exist in ``plans`` are silently
    skipped — the caller (usually a route) should warn, but the aggregator
    stays pure.
    """

    plan_index = {plan.id: plan for plan in plans}
    out: list[SubscriptionView] = []

    for binding in bindings:
        plan = plan_index.get(binding.plan_id)
        if plan is None:
            continue
        sub = plan.subscription
        window_views: list[WindowView] = []
        contributing_confidences: list[Confidence] = []
        contributing_count = 0

        for window in sub.windows:
            view, contrib = _window_view(
                window,
                subscription_product=sub.product,
                subscription_provider=sub.provider,
                account_id=binding.account_id,
                events=events,
                now=now,
            )
            window_views.append(view)
            contributing_count += len(contrib)
            contributing_confidences.extend(e.confidence for e in contrib)

        confidence = _downgrade_for_trackability(
            _weakest(contributing_confidences), plan.trackability
        )

        out.append(
            SubscriptionView(
                plan_id=plan.id,
                display_name=plan.display_name,
                provider=sub.provider,
                product=sub.product,
                plan=sub.plan,
                account_id=binding.account_id,
                trackability=plan.trackability.value,
                confidence=confidence.value,
                event_count=contributing_count,
                windows=tuple(window_views),
            )
        )
    return tuple(out)


def _downgrade_for_trackability(
    observed: Confidence,
    trackability: Trackability,
) -> Confidence:
    """Force INFERRED on web-only plans regardless of what the events claim.

    Every event from the manual collector is already INFERRED, but this
    guard covers the edge case where a future collector mistakenly tags a
    web-only subscription as EXACT.
    """

    if trackability is Trackability.WEB_ONLY_MANUAL:
        return Confidence.INFERRED
    return observed


def build_recent_events(
    events: Sequence[UsageEvent],
    *,
    limit: int = _DEFAULT_RECENT_LIMIT,
) -> tuple[RecentEventView, ...]:
    """Return the N most recent events, newest first."""

    ordered = sorted(events, key=lambda e: e.occurred_at, reverse=True)[:limit]
    return tuple(
        RecentEventView(
            occurred_at=e.occurred_at,
            provider=e.provider,
            product=e.product,
            model=e.model,
            session_id=e.session_id,
            project=e.project,
            input_tokens=e.input_tokens,
            output_tokens=e.output_tokens,
            cache_read_tokens=e.cache_read_tokens,
            reasoning_tokens=e.reasoning_tokens,
            total_tokens=e.total_tokens,
            cost_usd=e.cost_usd,
            confidence=e.confidence.value,
            source=e.source,
        )
        for e in ordered
    )


def build_daily_bars(
    events: Sequence[UsageEvent],
    *,
    now: datetime,
    days_back: int = _DEFAULT_DAYS_BACK,
) -> tuple[DailyBar, ...]:
    """14-day (configurable) token totals keyed by UTC date."""

    midnight = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    days = [midnight - timedelta(days=i) for i in range(days_back - 1, -1, -1)]
    buckets: dict[str, dict[str, int]] = {
        d.date().isoformat(): {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "reasoning_tokens": 0,
            "events": 0,
        }
        for d in days
    }

    for evt in events:
        key = evt.occurred_at.astimezone(UTC).date().isoformat()
        if key not in buckets:
            continue
        bucket = buckets[key]
        bucket["input_tokens"] += evt.input_tokens
        bucket["output_tokens"] += evt.output_tokens
        bucket["cache_read_tokens"] += evt.cache_read_tokens
        bucket["reasoning_tokens"] += evt.reasoning_tokens
        bucket["events"] += 1

    return tuple(
        DailyBar(
            date=day,
            input_tokens=counts["input_tokens"],
            output_tokens=counts["output_tokens"],
            cache_read_tokens=counts["cache_read_tokens"],
            reasoning_tokens=counts["reasoning_tokens"],
            total_tokens=(
                counts["input_tokens"]
                + counts["output_tokens"]
                + counts["cache_read_tokens"]
                + counts["reasoning_tokens"]
            ),
            events=counts["events"],
        )
        for day, counts in buckets.items()
    )


def build_provider_breakdown(events: Sequence[UsageEvent]) -> tuple[dict[str, Any], ...]:
    """Group totals by (provider, product) for the summary header cards."""

    groups: dict[tuple[str, str], dict[str, int | float]] = {}
    for evt in events:
        key = (evt.provider, evt.product)
        bucket = groups.setdefault(
            key,
            {
                "events": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "reasoning_tokens": 0,
                "cost_usd": 0.0,
            },
        )
        bucket["events"] = int(bucket["events"]) + 1
        bucket["input_tokens"] = int(bucket["input_tokens"]) + evt.input_tokens
        bucket["output_tokens"] = int(bucket["output_tokens"]) + evt.output_tokens
        bucket["cache_read_tokens"] = int(bucket["cache_read_tokens"]) + evt.cache_read_tokens
        bucket["reasoning_tokens"] = int(bucket["reasoning_tokens"]) + evt.reasoning_tokens
        if evt.cost_usd is not None:
            bucket["cost_usd"] = float(bucket["cost_usd"]) + evt.cost_usd

    return tuple(
        {"provider": provider, "product": product, **counts}
        for (provider, product), counts in sorted(groups.items())
    )


def build_payload(
    bindings: Sequence[SubscriptionBinding],
    plans: Sequence[PlanTemplate],
    events: Sequence[UsageEvent],
    *,
    now: datetime,
    recent_limit: int = _DEFAULT_RECENT_LIMIT,
    days_back: int = _DEFAULT_DAYS_BACK,
) -> DashboardPayload:
    """Combine every view-builder into the single JSON payload the UI fetches."""

    subscriptions = build_subscription_views(bindings, plans, events, now=now)
    recent = build_recent_events(events, limit=recent_limit)
    bars = build_daily_bars(events, now=now, days_back=days_back)
    provider = build_provider_breakdown(events)
    return DashboardPayload(
        generated_at=now,
        event_count=len(events),
        subscription_count=len(subscriptions),
        provider_breakdown=provider,
        subscriptions=subscriptions,
        recent_events=recent,
        daily_bars=bars,
    )


__all__ = [
    "DailyBar",
    "DashboardPayload",
    "RecentEventView",
    "SubscriptionView",
    "WindowView",
    "build_daily_bars",
    "build_payload",
    "build_provider_breakdown",
    "build_recent_events",
    "build_subscription_views",
]
