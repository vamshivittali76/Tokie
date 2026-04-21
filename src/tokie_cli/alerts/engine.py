"""End-to-end orchestration: evaluate, de-dupe, dispatch.

One entry point (:func:`check_alerts`) that the CLI and the dashboard both
call. It does the boring glue that the pure modules shouldn't do:

1. Read config + plans + events from disk.
2. Build the subscription view models via
   :func:`tokie_cli.dashboard.aggregator.build_subscription_views`.
3. Evaluate thresholds.
4. Record only the *new* crossings in the SQLite dedupe table.
5. Dispatch the new crossings through every configured channel.
6. Return an :class:`AlertRunResult` for callers to log / render.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from tokie_cli.alerts.channels import (
    BannerLine,
    Channel,
    ChannelDispatchResult,
    WebhookConfig,
    build_channels,
    render_banner,
)
from tokie_cli.alerts.storage import AlertStorage, connect_alerts
from tokie_cli.alerts.thresholds import (
    ThresholdCrossing,
    ThresholdRule,
    evaluate_thresholds,
)
from tokie_cli.config import ThresholdRuleConfig, TokieConfig, WebhookConfigEntry
from tokie_cli.dashboard.aggregator import (
    SubscriptionView,
    build_subscription_views,
)
from tokie_cli.db import query_events
from tokie_cli.plans import load_plans


@dataclass(frozen=True)
class AlertRunResult:
    """Everything the CLI / dashboard needs to report after one tick.

    ``armed`` is *every* currently-crossed threshold (used to render the
    banner). ``fired`` is the subset the engine just de-duped in and actually
    dispatched. ``dispatch_results`` zips back to ``fired`` via ``plan_id``
    / ``channel`` but we surface it separately so UI code doesn't have to
    dig.
    """

    ran_at: datetime
    armed: tuple[ThresholdCrossing, ...]
    fired: tuple[ThresholdCrossing, ...]
    dispatch_results: tuple[ChannelDispatchResult, ...]
    banner_lines: tuple[BannerLine, ...]


def check_alerts(
    config: TokieConfig,
    *,
    rules: Sequence[ThresholdRule] | None = None,
    enable_desktop: bool | None = None,
    webhook_configs: Sequence[WebhookConfig] | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> AlertRunResult:
    """Run one alert tick.

    Parameters
    ----------
    config:
        The loaded :class:`TokieConfig` (for DB path and subscription bindings).
    rules:
        Explicit rule list — overrides ``config.thresholds``. This is how
        tests inject a custom ruleset without round-tripping through TOML.
    enable_desktop, webhook_configs:
        Channel overrides, same story: if ``None`` we fall back to the
        config's ``channels`` section.
    dry_run:
        When ``True``, evaluate and record-as-new but *skip* every channel
        side effect. Useful for ``tokie alerts check --dry-run`` and for
        seeding a fresh DB without spamming Slack.
    now:
        Injectable clock for tests. Also forwarded to
        :func:`tokie_cli.alerts.storage.AlertStorage.record_fires`.
    """

    tick_at = now or datetime.now(UTC)

    if rules is not None:
        effective_rules: tuple[ThresholdRule, ...] = tuple(rules)
    else:
        effective_rules = tuple(_to_runtime_rule(r) for r in config.thresholds)
    if webhook_configs is not None:
        effective_webhooks: tuple[WebhookConfig, ...] = tuple(webhook_configs)
    else:
        effective_webhooks = tuple(_to_runtime_webhook(w) for w in config.webhooks)
    effective_desktop = (
        enable_desktop if enable_desktop is not None else config.alerts_desktop_enabled
    )

    subscriptions = _load_subscription_views(config, now=tick_at)
    armed = evaluate_thresholds(subscriptions, effective_rules)
    banner_lines = tuple(render_banner(armed))

    if not armed:
        return AlertRunResult(
            ran_at=tick_at,
            armed=(),
            fired=(),
            dispatch_results=(),
            banner_lines=banner_lines,
        )

    conn = connect_alerts(config.db_path)
    try:
        storage = AlertStorage(conn)
        new_fires = storage.record_fires(armed, now=tick_at)
    finally:
        conn.close()

    if dry_run or not new_fires:
        return AlertRunResult(
            ran_at=tick_at,
            armed=tuple(armed),
            fired=tuple(new_fires),
            dispatch_results=(),
            banner_lines=banner_lines,
        )

    channels = build_channels(
        enable_desktop=bool(effective_desktop),
        webhooks=list(effective_webhooks),
    )
    results = _dispatch(new_fires, channels)

    return AlertRunResult(
        ran_at=tick_at,
        armed=tuple(armed),
        fired=tuple(new_fires),
        dispatch_results=tuple(results),
        banner_lines=banner_lines,
    )


def _load_subscription_views(
    config: TokieConfig, *, now: datetime
) -> list[SubscriptionView]:
    """Re-build the dashboard aggregator output for the alert engine.

    We intentionally re-use the same code path the dashboard and TUI use, so
    the numbers the user sees on one surface are exactly the numbers that
    trigger alerts on another. No separate math, no drift.
    """

    if not config.db_path.exists():
        return []
    plans = load_plans()
    from tokie_cli.db import connect, migrate

    conn = connect(config.db_path)
    try:
        migrate(conn)
        events = query_events(conn)
    finally:
        conn.close()
    return list(
        build_subscription_views(
            bindings=config.subscriptions,
            plans=plans,
            events=events,
            now=now,
        )
    )


def _dispatch(
    crossings: Sequence[ThresholdCrossing],
    channels: dict[str, Channel],
) -> list[ChannelDispatchResult]:
    """Fan out each crossing to every channel it asked for.

    Unknown channel names are reported once per crossing as a failed
    dispatch so the operator sees "webhook:unknown -> channel disabled:
    not configured" instead of silent loss.
    """

    results: list[ChannelDispatchResult] = []
    for crossing in crossings:
        for channel_name in crossing.channels:
            channel = channels.get(channel_name)
            if channel is None:
                results.append(
                    ChannelDispatchResult(
                        channel=channel_name,
                        ok=False,
                        message="channel not configured",
                        dispatched_at=datetime.now(UTC),
                    )
                )
                continue
            results.append(channel.dispatch(crossing))
    return results


def _to_runtime_rule(cfg: ThresholdRuleConfig) -> ThresholdRule:
    """Convert config-level threshold entry to the runtime rule dataclass."""

    return ThresholdRule(
        plan_id=cfg.plan_id,
        account_id=cfg.account_id,
        levels=tuple(cfg.levels),
        channels=tuple(cfg.channels),
    )


def _to_runtime_webhook(cfg: WebhookConfigEntry) -> WebhookConfig:
    """Convert config-level webhook entry to the runtime webhook config."""

    return WebhookConfig(name=cfg.name, format=cfg.format)


__all__ = ["AlertRunResult", "check_alerts"]
