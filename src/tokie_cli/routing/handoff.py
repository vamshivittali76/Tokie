"""Handoff extractor: serialise recent work into a paste-ready briefing.

When the user runs ``tokie handoff`` or when the alert engine detects
a freshly-crossed 100% threshold, we need a short, self-contained
message that can be pasted into the *target* tool so the operator
doesn't re-explain the task from scratch.

This module stays pure: it takes a list of :class:`UsageEvent`s and
optional :class:`SubscriptionView`s and returns a :class:`HandoffBrief`
plus a :func:`render_handoff` formatter. No disk I/O, no network, no
LLM call. A markdown renderer is good enough and keeps the output
predictable.

The briefing is deliberately compact because most target tools have a
paste-size ceiling (ChatGPT's 4KB tooltip, Slack's block limit, etc.).
We keep the last ``max_events`` events and let the operator add detail.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from tokie_cli.dashboard.aggregator import SubscriptionView
from tokie_cli.routing.recommender import Recommendation
from tokie_cli.schema import UsageEvent

__all__ = [
    "HandoffBrief",
    "HandoffEvent",
    "build_handoff",
    "render_handoff",
]


@dataclass(frozen=True)
class HandoffEvent:
    """One recent usage row, reduced to the columns a human cares about."""

    occurred_at: datetime
    provider: str
    product: str
    model: str
    session_id: str | None
    project: str | None
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class HandoffBrief:
    """Everything ``tokie handoff`` produces.

    ``source`` describes where the operator was working (the
    subscription + tool that hit the limit) and ``target`` is the next
    tool the recommender suggests. ``events`` is the trailing context
    the operator can copy into the target tool. ``goal`` is a free-form
    string the operator passes via CLI; when empty we fall back to
    ``"Continue the previous session."``.
    """

    generated_at: datetime
    goal: str
    source_tool: str | None
    source_plan: str | None
    source_product: str | None
    target: Recommendation | None
    events: tuple[HandoffEvent, ...]
    reasons: tuple[str, ...]


def build_handoff(
    *,
    generated_at: datetime,
    events: Sequence[UsageEvent],
    source_subscription: SubscriptionView | None = None,
    target: Recommendation | None = None,
    goal: str | None = None,
    max_events: int = 8,
    session_id: str | None = None,
) -> HandoffBrief:
    """Assemble a :class:`HandoffBrief` from recent events.

    ``max_events`` caps how much context we include; the most recent
    events are kept so the brief stays chronological. Pass
    ``session_id`` to restrict the trail to a specific work session.
    """

    goal_text = (goal or "").strip() or "Continue the previous session."

    filtered = [e for e in events if session_id is None or e.session_id == session_id]
    filtered.sort(key=lambda e: e.occurred_at)
    trimmed = filtered[-max_events:]

    handoff_events = tuple(
        HandoffEvent(
            occurred_at=e.occurred_at,
            provider=e.provider,
            product=e.product,
            model=e.model,
            session_id=e.session_id,
            project=e.project,
            input_tokens=e.input_tokens,
            output_tokens=e.output_tokens,
            total_tokens=e.total_tokens,
        )
        for e in trimmed
    )

    reasons = _collect_reasons(source_subscription, target)

    source_tool = source_subscription.display_name if source_subscription else None
    source_plan = source_subscription.plan_id if source_subscription else None
    source_product = source_subscription.product if source_subscription else None

    return HandoffBrief(
        generated_at=generated_at,
        goal=goal_text,
        source_tool=source_tool,
        source_plan=source_plan,
        source_product=source_product,
        target=target,
        events=handoff_events,
        reasons=reasons,
    )


def render_handoff(brief: HandoffBrief, *, fmt: str = "markdown") -> str:
    """Render a :class:`HandoffBrief` to a paste-ready string.

    Formats:

    * ``"markdown"`` (default): nested headings, compact event table.
    * ``"plain"``: no markdown, safe for terminals without rich output.

    Any other value raises :class:`ValueError`.
    """

    if fmt == "markdown":
        return _render_markdown(brief)
    if fmt == "plain":
        return _render_plain(brief)
    raise ValueError(
        f"Unknown handoff format {fmt!r}; expected 'markdown' or 'plain'."
    )


def _collect_reasons(
    source: SubscriptionView | None,
    target: Recommendation | None,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if source is not None:
        worst = _worst_window_label(source)
        if worst:
            reasons.append(worst)
    if target is not None:
        reasons.append(
            f"Recommended next tool: {target.tool_display_name} "
            f"({target.plan_display_name}). {target.rationale}"
        )
    return tuple(reasons)


def _worst_window_label(sub: SubscriptionView) -> str | None:
    worst_pct = -1.0
    worst_text: str | None = None
    for window in sub.windows:
        if window.limit is None:
            continue
        if window.pct_used > worst_pct:
            worst_pct = window.pct_used
            worst_text = (
                f"{sub.display_name} is at {window.pct_used * 100:.0f}% of its "
                f"{window.window_type} limit."
            )
    return worst_text


def _render_markdown(brief: HandoffBrief) -> str:
    lines: list[str] = []
    lines.append("# Tokie handoff")
    lines.append("")
    lines.append(f"_Generated: {brief.generated_at.isoformat()}_")
    lines.append("")
    lines.append("## Goal")
    lines.append(brief.goal)
    lines.append("")

    if brief.source_tool or brief.source_plan:
        lines.append("## Coming from")
        lines.append(
            f"- Tool: **{brief.source_tool or 'unknown'}**"
        )
        if brief.source_plan:
            lines.append(f"- Subscription: `{brief.source_plan}`")
        if brief.source_product:
            lines.append(f"- Product: `{brief.source_product}`")
        lines.append("")

    if brief.target is not None:
        t = brief.target
        lines.append("## Going to")
        lines.append(
            f"- Tool: **{t.tool_display_name}** (tier {t.tier})"
        )
        lines.append(f"- Subscription: `{t.plan_id}` / account `{t.account_id}`")
        lines.append(f"- Reason: {t.rationale}")
        lines.append("")

    if brief.reasons:
        lines.append("## Why now")
        for reason in brief.reasons:
            lines.append(f"- {reason}")
        lines.append("")

    if brief.events:
        lines.append("## Recent context")
        lines.append("")
        lines.append("| when | tool | model | tokens |")
        lines.append("| --- | --- | --- | --- |")
        for event in brief.events:
            when = event.occurred_at.strftime("%Y-%m-%d %H:%M")
            tool = f"{event.provider}/{event.product}"
            lines.append(
                f"| {when} | {tool} | {event.model} | {event.total_tokens:,} |"
            )
        lines.append("")

    lines.append("## Prompt")
    lines.append("")
    lines.append("> " + brief.goal.replace("\n", "\n> "))
    lines.append("")
    lines.append(
        "Paste the context above and ask the target tool to pick up where we left off."
    )
    return "\n".join(lines).rstrip() + "\n"


def _render_plain(brief: HandoffBrief) -> str:
    lines: list[str] = []
    lines.append("TOKIE HANDOFF")
    lines.append(f"Generated: {brief.generated_at.isoformat()}")
    lines.append("")
    lines.append("Goal:")
    lines.append(f"  {brief.goal}")
    lines.append("")

    if brief.source_tool or brief.source_plan:
        lines.append("Coming from:")
        if brief.source_tool:
            lines.append(f"  Tool: {brief.source_tool}")
        if brief.source_plan:
            lines.append(f"  Subscription: {brief.source_plan}")
        if brief.source_product:
            lines.append(f"  Product: {brief.source_product}")
        lines.append("")

    if brief.target is not None:
        t = brief.target
        lines.append("Going to:")
        lines.append(f"  Tool: {t.tool_display_name} (tier {t.tier})")
        lines.append(f"  Subscription: {t.plan_id} (account {t.account_id})")
        lines.append(f"  Reason: {t.rationale}")
        lines.append("")

    if brief.reasons:
        lines.append("Why now:")
        for reason in brief.reasons:
            lines.append(f"  - {reason}")
        lines.append("")

    if brief.events:
        lines.append("Recent context:")
        for event in brief.events:
            when = event.occurred_at.strftime("%Y-%m-%d %H:%M")
            lines.append(
                f"  {when}  {event.provider}/{event.product}  "
                f"{event.model}  {event.total_tokens:,} tokens"
            )
        lines.append("")

    lines.append("Prompt:")
    lines.append(f"  {brief.goal}")
    return "\n".join(lines).rstrip() + "\n"
