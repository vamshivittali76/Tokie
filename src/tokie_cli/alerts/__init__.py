"""Tokie alert engine.

The alert layer is split into four concerns, each in its own submodule so the
pure parts stay trivially testable and the I/O parts stay swappable:

- :mod:`tokie_cli.alerts.thresholds` — pure-function threshold evaluation.
- :mod:`tokie_cli.alerts.storage` — SQLite-backed de-dup log of fires.
- :mod:`tokie_cli.alerts.channels` — Desktop, webhook, and banner delivery.
- :mod:`tokie_cli.alerts.engine` — orchestrates evaluate → dedupe → dispatch.

Nothing in :mod:`thresholds` touches the database, the network, or the
filesystem; everything user-observable lives in :mod:`channels` and
:mod:`engine`.
"""

from tokie_cli.alerts.channels import (
    BannerChannel,
    BannerLine,
    Channel,
    ChannelDispatchResult,
    DesktopChannel,
    WebhookChannel,
    build_channels,
    render_banner,
)
from tokie_cli.alerts.engine import AlertRunResult, check_alerts
from tokie_cli.alerts.storage import (
    AlertStorage,
    FireRecord,
    connect_alerts,
)
from tokie_cli.alerts.thresholds import (
    DEFAULT_LEVELS,
    ThresholdCrossing,
    ThresholdRule,
    evaluate_thresholds,
    matches_binding,
    merge_rules_for_binding,
    normalise_levels,
)

__all__ = [
    "DEFAULT_LEVELS",
    "AlertRunResult",
    "AlertStorage",
    "BannerChannel",
    "BannerLine",
    "Channel",
    "ChannelDispatchResult",
    "DesktopChannel",
    "FireRecord",
    "ThresholdCrossing",
    "ThresholdRule",
    "WebhookChannel",
    "build_channels",
    "check_alerts",
    "connect_alerts",
    "evaluate_thresholds",
    "matches_binding",
    "merge_rules_for_binding",
    "normalise_levels",
    "render_banner",
]
