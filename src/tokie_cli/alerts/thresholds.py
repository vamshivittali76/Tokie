"""Pure-function threshold evaluation.

Given a set of :class:`SubscriptionView` rows (produced by the dashboard
aggregator) and a set of user-configured :class:`ThresholdRule` s, this module
computes the list of :class:`ThresholdCrossing` s that are currently armed.

De-duplication ("don't spam the user every minute") is **not** handled here —
that's the job of :mod:`tokie_cli.alerts.storage`. This module is stateless so
the engine can re-evaluate on every tick without paying I/O cost and so the
test matrix stays small.

The canonical de-dup key is::

    (plan_id, account_id, window_type, window_starts_at_iso, threshold_pct)

``window_starts_at_iso`` is ``""`` for :class:`WindowType.NONE` (no-limit
subscriptions), which keeps the key stable across DB round-trips regardless of
whether ``None`` would have been serialized as ``null`` or ``""``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Final

from tokie_cli.dashboard.aggregator import SubscriptionView, WindowView

DEFAULT_LEVELS: Final[tuple[int, ...]] = (75, 95, 100)
"""Default thresholds: "warming up", "act now", and "over limit"."""

DEFAULT_CHANNELS: Final[tuple[str, ...]] = ("banner",)
"""Default channel set when a rule omits ``channels``.

``banner`` is always safe: it just renders text in ``tokie status``. Desktop
notifications and webhooks are opt-in because they carry side effects (OS
notification popup, outbound HTTP request) that a first-run user hasn't
consented to yet.
"""


@dataclass(frozen=True)
class ThresholdRule:
    """A user-configured rule for when to fire an alert.

    Parameters
    ----------
    plan_id, account_id:
        Restrict the rule to a single subscription. ``None`` means "match any".
        A rule with ``plan_id=None`` and ``account_id=None`` applies
        everywhere — this is how the default rule is expressed.
    levels:
        Percent thresholds, in ``[0, 100]``, that fire (at most once per
        window) when crossed. We keep integers because real-world plans only
        ever quote integer percentages and floats would make the de-dup key
        fragile.
    channels:
        Named delivery channels: ``banner``, ``desktop``, or
        ``webhook:<name>``. The alert engine looks up the bound
        :class:`Channel` by name at dispatch time; unknown channels are
        ignored with a warning rather than crashing the engine.
    """

    plan_id: str | None = None
    account_id: str | None = None
    levels: tuple[int, ...] = DEFAULT_LEVELS
    channels: tuple[str, ...] = DEFAULT_CHANNELS

    def __post_init__(self) -> None:
        object.__setattr__(self, "levels", normalise_levels(self.levels))
        if not self.channels:
            object.__setattr__(self, "channels", DEFAULT_CHANNELS)


@dataclass(frozen=True)
class ThresholdCrossing:
    """A concrete "subscription X just crossed Y%" event.

    ``window_starts_at_iso`` is ``""`` for windowless subscriptions so the
    de-dup key stays a plain string-tuple and doesn't need nullable handling
    at storage time.
    """

    plan_id: str
    account_id: str
    display_name: str
    provider: str
    product: str
    window_type: str
    window_starts_at_iso: str
    window_resets_at_iso: str
    threshold_pct: int
    pct_used: float
    used: float
    limit: float | None
    remaining: float | None
    channels: tuple[str, ...] = field(default_factory=tuple)

    @property
    def dedupe_key(self) -> tuple[str, str, str, str, int]:
        """Stable key used by storage to recognise duplicate fires."""

        return (
            self.plan_id,
            self.account_id,
            self.window_type,
            self.window_starts_at_iso,
            self.threshold_pct,
        )

    def severity(self) -> str:
        """Coarse severity tag for UI colouring.

        ``over`` = actually exceeded (100+), ``high`` = 95 range,
        ``medium`` = 75 range, ``low`` otherwise. Channels that want richer
        semantics can read ``threshold_pct`` directly; this helper exists so
        callers that don't care about the exact number can just pick a colour.
        """

        if self.threshold_pct >= 100:
            return "over"
        if self.threshold_pct >= 95:
            return "high"
        if self.threshold_pct >= 75:
            return "medium"
        return "low"


def normalise_levels(levels: Iterable[int]) -> tuple[int, ...]:
    """Return levels sorted ascending, deduped, and clipped to ``[0, 100]``.

    We clip instead of raise because config is user-editable and we'd rather
    keep the alert loop running on bad input than fall over. Out-of-range
    values are silently clamped; callers that care about strict validation
    can check themselves before calling.
    """

    cleaned: set[int] = set()
    for raw in levels:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        cleaned.add(max(0, min(100, value)))
    return tuple(sorted(cleaned))


def matches_binding(
    rule: ThresholdRule, *, plan_id: str, account_id: str
) -> bool:
    """Return True when ``rule`` should apply to this binding."""

    if rule.plan_id is not None and rule.plan_id != plan_id:
        return False
    return not (rule.account_id is not None and rule.account_id != account_id)


def merge_rules_for_binding(
    rules: Sequence[ThresholdRule],
    *,
    plan_id: str,
    account_id: str,
) -> tuple[tuple[int, ...], tuple[str, ...]]:
    """Combine every matching rule into one effective (levels, channels) pair.

    We take the union of levels and channels across matching rules. That's the
    friendliest behaviour: if a user defines a global 75/95/100 rule and then
    a plan-specific rule adding 50, we fire on 50/75/95/100 rather than
    replacing one ruleset with the other.

    Returns ``((), ())`` when no rules apply, which tells the evaluator to
    skip this subscription entirely.
    """

    levels: set[int] = set()
    channels: list[str] = []
    for rule in rules:
        if not matches_binding(rule, plan_id=plan_id, account_id=account_id):
            continue
        levels.update(rule.levels)
        for channel in rule.channels:
            if channel not in channels:
                channels.append(channel)
    if not levels:
        return ((), ())
    return (tuple(sorted(levels)), tuple(channels))


def evaluate_thresholds(
    subscriptions: Sequence[SubscriptionView],
    rules: Sequence[ThresholdRule],
) -> list[ThresholdCrossing]:
    """Return every threshold currently crossed, sorted by severity desc.

    Windows with no limit (``pct_used`` is ``0.0`` when ``limit`` is missing
    from the aggregator path) are skipped — no limit means nothing to alert
    on. Subscriptions with no matching rules are skipped entirely.
    """

    out: list[ThresholdCrossing] = []
    for sub in subscriptions:
        levels, channels = merge_rules_for_binding(
            rules, plan_id=sub.plan_id, account_id=sub.account_id
        )
        if not levels:
            continue
        for window in sub.windows:
            if window.limit is None or window.limit <= 0:
                continue
            pct = window.pct_used * 100.0
            for level in levels:
                if pct + 1e-9 < level:
                    continue
                out.append(_crossing(sub, window, level, channels))
    out.sort(key=lambda c: (-c.threshold_pct, c.plan_id, c.account_id))
    return out


def _crossing(
    sub: SubscriptionView,
    window: WindowView,
    level: int,
    channels: tuple[str, ...],
) -> ThresholdCrossing:
    return ThresholdCrossing(
        plan_id=sub.plan_id,
        account_id=sub.account_id,
        display_name=sub.display_name,
        provider=sub.provider,
        product=sub.product,
        window_type=window.window_type,
        window_starts_at_iso=(
            window.starts_at.isoformat() if window.starts_at is not None else ""
        ),
        window_resets_at_iso=(
            window.resets_at.isoformat() if window.resets_at is not None else ""
        ),
        threshold_pct=level,
        pct_used=window.pct_used,
        used=window.used,
        limit=window.limit,
        remaining=window.remaining,
        channels=channels,
    )


__all__ = [
    "DEFAULT_CHANNELS",
    "DEFAULT_LEVELS",
    "ThresholdCrossing",
    "ThresholdRule",
    "evaluate_thresholds",
    "matches_binding",
    "merge_rules_for_binding",
    "normalise_levels",
]
