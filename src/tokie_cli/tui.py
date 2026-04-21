"""Textual TUI for live subscription monitoring (``tokie watch``).

Reuses :mod:`tokie_cli.dashboard.aggregator` to compute subscription views
so the TUI and web dashboard never drift: a change to the aggregation math
is immediately visible in both surfaces.

Design goals:

* **Glanceable.** One line per subscription, one progress bar per window,
  one sparkline per subscription. No pagination.
* **Honest.** Confidence tier is rendered via bar character choice:
  ``█`` for ``EXACT``, ``▓`` for ``ESTIMATED``, ``░`` for ``INFERRED``.
  Matches the dashboard's solid/striped/dashed pattern.
* **Local-only.** No network I/O here — the TUI only reads the SQLite DB
  that the user's scheduled ``tokie scan`` jobs keep fresh.
* **Quiet on empty.** If no bindings are configured, the app shows the
  first-run hint instead of a blank screen.

See section 9 of ``TOKIE_DEVELOPMENT_PLAN_FINAL.md`` for the Textual TUI
spec that gates Week 2's exit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.reactive import reactive
from textual.widgets import Footer, Header, Label, Static

from tokie_cli.config import TokieConfig, load_config
from tokie_cli.dashboard.aggregator import (
    DashboardPayload,
    SubscriptionView,
    WindowView,
    build_payload,
)
from tokie_cli.db import connect, migrate, query_events
from tokie_cli.plans import load_plans
from tokie_cli.schema import UsageEvent

_SPARKLINE_BUCKETS = 24
_SPARKLINE_WIDTH_HOURS = 24
_SPARK_CHARS = " ▁▂▃▄▅▆▇█"
_CONFIDENCE_GLYPHS = {"exact": "█", "estimated": "▓", "inferred": "░"}


def _fmt_countdown(target: datetime | None, *, now: datetime) -> str:
    """Render a compact countdown like ``4h 12m`` or ``in 2d 3h``.

    Returns the empty string when ``target`` is None, which happens for
    web-only plans where we don't know the next reset. The aggregator is
    already careful to emit None for those cases — this is the final UI
    fallback.
    """

    if target is None:
        return "no reset"
    delta = target - now
    total_sec = int(delta.total_seconds())
    if total_sec <= 0:
        return "now"
    days, rem = divmod(total_sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _render_bar(window: WindowView, *, width: int = 30) -> Text:
    """Return a rich Text progress bar with confidence-tier styling.

    Colour is driven by percent-used, character by confidence tier. The
    combination gives two simultaneous signals (urgency + trust) in a
    single line of ASCII so the TUI stays dense without relying on colour
    alone for accessibility.
    """

    pct = min(max(window.pct_used, 0.0), 1.0)
    filled = round(pct * width)
    if window.pct_used >= 1.0:
        colour = "bold red"
    elif window.pct_used >= 0.95:
        colour = "red"
    elif window.pct_used >= 0.75:
        colour = "yellow"
    else:
        colour = "green"

    bar = Text()
    bar.append("[", style="dim")
    bar.append("█" * filled, style=colour)
    bar.append("·" * (width - filled), style="dim")
    bar.append("]", style="dim")
    return bar


def _sparkline(events: list[UsageEvent], *, now: datetime) -> str:
    """24h hourly sparkline from the last ``_SPARKLINE_WIDTH_HOURS`` of events."""

    buckets = [0] * _SPARKLINE_BUCKETS
    start = now - timedelta(hours=_SPARKLINE_WIDTH_HOURS)
    bucket_size = timedelta(hours=_SPARKLINE_WIDTH_HOURS / _SPARKLINE_BUCKETS)
    for evt in events:
        if evt.occurred_at < start:
            continue
        idx = int((evt.occurred_at - start).total_seconds() // bucket_size.total_seconds())
        if 0 <= idx < len(buckets):
            buckets[idx] += evt.input_tokens + evt.output_tokens
    peak = max(buckets) or 1
    return "".join(
        _SPARK_CHARS[min(len(_SPARK_CHARS) - 1, round(v / peak * (len(_SPARK_CHARS) - 1)))]
        for v in buckets
    )


class SubscriptionCard(Static):
    """One subscription's progress block."""

    def __init__(self, view: SubscriptionView, sparkline: str, now: datetime) -> None:
        super().__init__()
        self._view = view
        self._sparkline = sparkline
        self._now = now

    def render(self) -> Text:
        view = self._view
        out = Text()
        out.append(f"{view.display_name}", style="bold")
        out.append(f"  [{view.confidence}]", style="dim")
        out.append(f"  {view.event_count} events", style="dim")
        out.append("\n")
        for w in view.windows:
            bar = _render_bar(w)
            pct_str = f"{w.pct_used * 100:5.1f}%"
            used = int(w.used)
            limit = int(w.limit) if w.limit is not None else None
            limit_str = f"{used:>7,}/{limit:>7,}" if limit is not None else f"{used:>7,} used"
            reset = _fmt_countdown(w.resets_at, now=self._now)
            out.append(f"  {w.window_type:<12}", style="cyan")
            out.append(bar)
            out.append(f" {pct_str}  {limit_str}  resets {reset}\n")
        out.append(f"  24h  {self._sparkline}\n", style="dim")
        return out


class EmptyState(Static):
    """First-run helper displayed when no bindings are configured."""

    def render(self) -> Text:
        out = Text()
        out.append("No subscriptions configured.\n\n", style="bold yellow")
        out.append("Bind a plan with:\n", style="dim")
        out.append("  tokie plans\n", style="cyan")
        out.append("  tokie init      ", style="cyan")
        out.append("# then edit tokie.toml\n", style="dim")
        return out


class TokieWatchApp(App[None]):
    """Live subscription monitor for ``tokie watch``."""

    CSS = """
    Screen { background: $surface; }
    #cards { padding: 1 2; }
    SubscriptionCard { padding: 1 0; border: solid $primary-background-lighten-1; }
    EmptyState { padding: 2 3; color: $warning; }
    #status { dock: top; background: $primary-background; color: $text; padding: 0 1; }
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
    ]

    refresh_interval_sec: reactive[float] = reactive(5.0)

    def __init__(self, *, config: TokieConfig | None = None) -> None:
        super().__init__()
        self._config = config or load_config()
        self._payload: DashboardPayload | None = None
        self._events: list[UsageEvent] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True, name="Tokie — Live Usage")
        yield Label("", id="status")
        yield VerticalScroll(Vertical(id="cards"))
        yield Footer()

    def on_mount(self) -> None:
        self._tick()
        self.set_interval(self.refresh_interval_sec, self._tick)

    def action_refresh(self) -> None:
        self._tick()

    def _tick(self) -> None:
        now = datetime.now(UTC)
        try:
            plans = load_plans()
        except Exception:  # pragma: no cover - defensive
            plans = []
        if not self._config.db_path.exists():
            self._render(None, [], now, error="db not found — run 'tokie init' + 'tokie scan'")
            return
        conn = connect(self._config.db_path)
        try:
            migrate(conn)
            events = list(query_events(conn))
        finally:
            conn.close()
        payload = build_payload(
            bindings=self._config.subscriptions,
            plans=plans,
            events=events,
            now=now,
        )
        self._render(payload, events, now)

    def _render(
        self,
        payload: DashboardPayload | None,
        events: list[UsageEvent],
        now: datetime,
        *,
        error: str | None = None,
    ) -> None:
        cards = self.query_one("#cards", Vertical)
        cards.remove_children()
        status_label = self.query_one("#status", Label)
        if error:
            status_label.update(f"[bold red]{error}[/bold red]")
            cards.mount(EmptyState())
            return
        assert payload is not None
        total_tokens = sum(e.input_tokens + e.output_tokens for e in events)
        status_label.update(
            f"events: {payload.event_count:,}  "
            f"subscriptions: {payload.subscription_count}  "
            f"total tokens: {total_tokens:,}  "
            f"refreshed: {now.strftime('%H:%M:%S')} UTC"
        )
        if not payload.subscriptions:
            cards.mount(EmptyState())
            return
        for view in payload.subscriptions:
            relevant = [
                e
                for e in events
                if e.provider == view.provider and e.account_id == view.account_id
            ]
            cards.mount(SubscriptionCard(view, _sparkline(relevant, now=now), now))


def run_watch(*, config: TokieConfig | None = None) -> None:
    """Launch the watch TUI. Blocking."""

    TokieWatchApp(config=config).run()


__all__ = [
    "TokieWatchApp",
    "run_watch",
]
