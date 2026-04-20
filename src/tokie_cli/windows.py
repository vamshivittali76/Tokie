"""Pure-function quota window math.

This module is the single source of truth for "what does a quota window
look like right now?" — given a :class:`WindowType`, a session anchor, and
the current moment, it answers:

* where does the window start and end?
* when does it reset?
* which events fall inside it?
* how full is it, according to the most constrained of the declared limits?

Every function is pure: no I/O, no clocks, no global state. All datetimes
must be tz-aware; passing a naive datetime raises :class:`ValueError`.

Source: section 3 (Claude Pro rolling-5h + weekly overlap) and section 6
(canonical schema) of TOKIE_DEVELOPMENT_PLAN_FINAL.md.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from tokie_cli.schema import LimitWindow, UsageEvent, WindowType

LimitBasis = Literal["tokens", "messages", "usd", "none"]


@dataclass(frozen=True)
class UsageAggregate:
    """Sum of :class:`UsageEvent` counters over some window."""

    total_input_tokens: int
    total_output_tokens: int
    total_cache_read_tokens: int
    total_cache_write_tokens: int
    total_reasoning_tokens: int
    total_cost_usd: float
    total_messages: int

    @property
    def total_tokens(self) -> int:
        """Grand total across every token sub-counter."""

        return (
            self.total_input_tokens
            + self.total_output_tokens
            + self.total_cache_read_tokens
            + self.total_cache_write_tokens
            + self.total_reasoning_tokens
        )


@dataclass(frozen=True)
class Capacity:
    """How full a window is, reduced to a single scalar ``pct_used``.

    When a window declares multiple limits (e.g. tokens *and* messages) the
    basis is whichever is the most constrained right now, so the UI never
    shows a reassuring token bar while a message quota is silently busted.
    """

    limit_basis: LimitBasis
    used: float
    limit: float | None
    remaining: float | None
    pct_used: float

    @property
    def is_over(self) -> bool:
        """True once :attr:`pct_used` crosses 1.0 (strictly >= 1.0)."""

        return self.pct_used >= 1.0


def _ensure_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{name} must be tz-aware, got naive datetime: {value!r}")


def _midnight_utc(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _first_of_month_utc(moment: datetime) -> datetime:
    midnight = _midnight_utc(moment)
    return midnight.replace(day=1)


def _first_of_next_month_utc(moment: datetime) -> datetime:
    start = _first_of_month_utc(moment)
    year = start.year + (start.month // 12)
    month = (start.month % 12) + 1
    return start.replace(year=year, month=month, day=1)


def window_bounds(
    window_type: WindowType,
    session_start: datetime,
    now: datetime,
) -> tuple[datetime, datetime] | None:
    """Return ``(start, end)`` of the current quota window, or ``None``.

    Semantics per :class:`WindowType`:

    * ``ROLLING_5H`` — ``(session_start, session_start + 5h)``.
    * ``DAILY`` — today's UTC midnight through tomorrow's. Ignores
      ``session_start``.
    * ``WEEKLY`` — ``(session_start, session_start + 7d)``. This matches
      Claude Pro's weekly window which is anchored on first use, not on a
      calendar week.
    * ``MONTHLY`` — first-of-month UTC through first-of-next-month UTC.
      Ignores ``session_start``.
    * ``NONE`` — returns ``None``.

    Both datetimes in the returned tuple are tz-aware UTC.
    """

    _ensure_aware(session_start, "session_start")
    _ensure_aware(now, "now")

    match window_type:
        case WindowType.ROLLING_5H:
            start = session_start.astimezone(UTC)
            return (start, start + timedelta(hours=5))
        case WindowType.DAILY:
            start = _midnight_utc(now)
            return (start, start + timedelta(days=1))
        case WindowType.WEEKLY:
            start = session_start.astimezone(UTC)
            return (start, start + timedelta(days=7))
        case WindowType.MONTHLY:
            start = _first_of_month_utc(now)
            return (start, _first_of_next_month_utc(now))
        case WindowType.NONE:
            return None


def next_reset_at(
    window_type: WindowType,
    session_start: datetime,
    now: datetime,
) -> datetime | None:
    """Return the end of the current window (``None`` for :attr:`WindowType.NONE`)."""

    bounds = window_bounds(window_type, session_start, now)
    if bounds is None:
        return None
    return bounds[1]


def aggregate_events(
    events: Iterable[UsageEvent],
    *,
    start: datetime,
    end: datetime,
) -> UsageAggregate:
    """Sum events whose ``occurred_at`` falls in ``[start, end)``.

    The half-open interval means an event right at ``start`` is included
    but an event right at ``end`` is not — this makes consecutive windows
    partition time cleanly with no double-counting.
    """

    _ensure_aware(start, "start")
    _ensure_aware(end, "end")

    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_write = 0
    total_reasoning = 0
    total_cost = 0.0
    total_messages = 0

    for evt in events:
        if evt.occurred_at < start or evt.occurred_at >= end:
            continue
        total_input += evt.input_tokens
        total_output += evt.output_tokens
        total_cache_read += evt.cache_read_tokens
        total_cache_write += evt.cache_write_tokens
        total_reasoning += evt.reasoning_tokens
        if evt.cost_usd is not None:
            total_cost += evt.cost_usd
        total_messages += 1

    return UsageAggregate(
        total_input_tokens=total_input,
        total_output_tokens=total_output,
        total_cache_read_tokens=total_cache_read,
        total_cache_write_tokens=total_cache_write,
        total_reasoning_tokens=total_reasoning,
        total_cost_usd=total_cost,
        total_messages=total_messages,
    )


def capacity(limit: LimitWindow, aggregate: UsageAggregate) -> Capacity:
    """Reduce an aggregate against a :class:`LimitWindow` to a single capacity.

    When multiple of ``limit_tokens`` / ``limit_messages`` / ``limit_usd``
    are set, the basis is whichever yields the highest ``used / limit``
    right now — the most constrained dimension wins so UIs never under-
    report pressure.
    """

    candidates: list[tuple[LimitBasis, float, float]] = []
    if limit.limit_tokens is not None:
        candidates.append(("tokens", float(aggregate.total_tokens), float(limit.limit_tokens)))
    if limit.limit_messages is not None:
        candidates.append(
            ("messages", float(aggregate.total_messages), float(limit.limit_messages))
        )
    if limit.limit_usd is not None:
        candidates.append(("usd", float(aggregate.total_cost_usd), float(limit.limit_usd)))

    if not candidates:
        return Capacity(
            limit_basis="none",
            used=0.0,
            limit=None,
            remaining=None,
            pct_used=0.0,
        )

    def _pct(row: tuple[LimitBasis, float, float]) -> float:
        _, used, cap = row
        if cap == 0.0:
            return float("inf") if used > 0 else 0.0
        return used / cap

    basis, used, cap_value = max(candidates, key=_pct)
    pct = _pct((basis, used, cap_value))
    remaining = max(cap_value - used, 0.0)
    return Capacity(
        limit_basis=basis,
        used=used,
        limit=cap_value,
        remaining=remaining,
        pct_used=pct,
    )


__all__ = [
    "Capacity",
    "UsageAggregate",
    "aggregate_events",
    "capacity",
    "next_reset_at",
    "window_bounds",
]
