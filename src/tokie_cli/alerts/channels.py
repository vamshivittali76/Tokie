"""Delivery channels for fired thresholds.

Every channel implements the :class:`Channel` protocol: given a concrete
:class:`ThresholdCrossing`, do the side-effect (desktop notification, webhook
POST, banner emit) and return a :class:`ChannelDispatchResult` the engine can
log.

The registry (:func:`build_channels`) only instantiates channels the operator
asked for and that actually have the credentials/deps to run. Anything
missing is returned as a disabled :class:`_MissingChannel` so the engine can
surface a one-line warning without crashing.

Secrets never appear in logs, tracebacks, or result fields — see
:meth:`WebhookChannel._format_slack` / ``_format_discord``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import httpx

try:  # pragma: no cover - import-time branch
    import keyring
    import keyring.errors as keyring_errors

    _HAS_KEYRING = True
except Exception:  # pragma: no cover - keyring is an install-time dep
    keyring = None  # type: ignore[assignment]
    keyring_errors = None  # type: ignore[assignment]
    _HAS_KEYRING = False

try:  # pragma: no cover - import-time branch
    from desktop_notifier import DesktopNotifier, Urgency

    _HAS_DESKTOP_NOTIFIER = True
except Exception:  # pragma: no cover
    DesktopNotifier = None  # type: ignore[assignment,misc]
    Urgency = None  # type: ignore[assignment,misc]
    _HAS_DESKTOP_NOTIFIER = False

from tokie_cli.alerts.thresholds import ThresholdCrossing

_WEBHOOK_KEYRING_SERVICE = "tokie-webhook"
_WEBHOOK_HTTP_TIMEOUT = 10.0
_BANNER_CHANNEL_NAME = "banner"
_DESKTOP_CHANNEL_NAME = "desktop"

_logger = logging.getLogger("tokie.alerts.channels")


@runtime_checkable
class Channel(Protocol):
    """Every channel is identified by ``name`` and knows how to dispatch."""

    @property
    def name(self) -> str:
        ...

    def dispatch(self, crossing: ThresholdCrossing) -> ChannelDispatchResult:
        ...


@dataclass(frozen=True)
class ChannelDispatchResult:
    """One attempted delivery; success or (non-fatal) error.

    The engine logs every result and never re-raises, because a broken Slack
    webhook should never silently break the desktop notification that the
    same threshold triggered.
    """

    channel: str
    ok: bool
    message: str
    dispatched_at: datetime


@dataclass(frozen=True)
class BannerLine:
    """One renderable banner line (``tokie status`` / dashboard header)."""

    text: str
    severity: str  # "low" / "medium" / "high" / "over"


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------


class BannerChannel:
    """Banner "dispatch" is a no-op; the banner is re-rendered from state.

    We still keep it as a :class:`Channel` so the registry stays uniform and
    so the engine can record that a fired crossing *is* expected to appear in
    the banner rendering — users explicitly asked for it in their rule.
    """

    name = _BANNER_CHANNEL_NAME

    def dispatch(self, crossing: ThresholdCrossing) -> ChannelDispatchResult:
        return ChannelDispatchResult(
            channel=self.name,
            ok=True,
            message="banner queued (re-rendered live)",
            dispatched_at=datetime.now(UTC),
        )


def render_banner(
    crossings: list[ThresholdCrossing],
    *,
    max_lines: int = 5,
) -> list[BannerLine]:
    """Compact multi-line banner: one line per crossing, severity-sorted.

    Used by ``tokie status`` and the dashboard header. We clip to ``max_lines``
    so a user with 12 subscriptions at 75% doesn't get an unreadable wall of
    warnings — the oldest-severity tail is collapsed into a single "+N more"
    line.
    """

    if not crossings:
        return []
    sorted_cr = sorted(
        crossings,
        key=lambda c: (-c.threshold_pct, c.plan_id, c.account_id),
    )
    lines: list[BannerLine] = []
    for crossing in sorted_cr[:max_lines]:
        pct = round(crossing.pct_used * 100)
        text = (
            f"{crossing.display_name} [{crossing.account_id}] "
            f"{crossing.window_type} at {pct}% "
            f"(armed ≥ {crossing.threshold_pct}%)"
        )
        lines.append(BannerLine(text=text, severity=crossing.severity()))
    extra = len(sorted_cr) - max_lines
    if extra > 0:
        lines.append(
            BannerLine(
                text=f"+{extra} more threshold(s) armed — run `tokie alerts check`",
                severity=sorted_cr[-1].severity(),
            )
        )
    return lines


# ---------------------------------------------------------------------------
# Desktop
# ---------------------------------------------------------------------------


class DesktopChannel:
    """Native OS notification via :mod:`desktop_notifier`.

    The library is asyncio-first, so we run a short-lived event loop per
    dispatch with :func:`asyncio.run`. That's fine for alert tick frequency
    (seconds-to-minutes) and avoids leaking a long-lived loop into the sync
    CLI path.
    """

    name = _DESKTOP_CHANNEL_NAME

    def __init__(
        self,
        notifier: DesktopNotifier | None = None,
        *,
        app_name: str = "Tokie",
    ) -> None:
        if not _HAS_DESKTOP_NOTIFIER:
            raise RuntimeError(
                "desktop-notifier is required for the 'desktop' channel"
            )
        self._notifier = notifier or DesktopNotifier(app_name=app_name)

    def dispatch(self, crossing: ThresholdCrossing) -> ChannelDispatchResult:
        title, body, urgency = _format_desktop(crossing)
        try:
            asyncio.run(
                self._notifier.send(title=title, message=body, urgency=urgency)
            )
        except Exception as exc:  # pragma: no cover - OS-specific failure paths
            _logger.warning("desktop notifier failed: %s", exc)
            return ChannelDispatchResult(
                channel=self.name,
                ok=False,
                message=f"{type(exc).__name__}: {exc}",
                dispatched_at=datetime.now(UTC),
            )
        return ChannelDispatchResult(
            channel=self.name,
            ok=True,
            message=f"desktop notification sent ({crossing.threshold_pct}%)",
            dispatched_at=datetime.now(UTC),
        )


def _format_desktop(crossing: ThresholdCrossing) -> tuple[str, str, Any]:
    """Return ``(title, body, urgency)`` for the OS notification.

    We keep the title short (most DEs crop at ~40 chars) and put the actual
    numbers in the body so the user sees them even if the title ends up
    ellipsised.
    """

    pct = round(crossing.pct_used * 100)
    title = f"Tokie: {crossing.display_name} at {pct}%"
    body_lines = [
        f"{crossing.account_id} • {crossing.window_type}",
        f"used: {crossing.used:.0f}",
    ]
    if crossing.limit is not None:
        body_lines.append(f"limit: {crossing.limit:.0f}")
    if crossing.remaining is not None:
        body_lines.append(f"remaining: {crossing.remaining:.0f}")
    if crossing.window_resets_at_iso:
        body_lines.append(f"resets at: {crossing.window_resets_at_iso}")
    urgency = _desktop_urgency(crossing.severity())
    return title, "\n".join(body_lines), urgency


def _desktop_urgency(severity: str) -> Any:
    """Map our severity tag to the notifier's urgency level."""

    if not _HAS_DESKTOP_NOTIFIER:  # pragma: no cover - import-time guard
        return None
    mapping = {
        "low": Urgency.Low,
        "medium": Urgency.Normal,
        "high": Urgency.Normal,
        "over": Urgency.Critical,
    }
    return mapping.get(severity, Urgency.Normal)


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WebhookSpec:
    """Structural description of a configured webhook.

    Kept separate from :class:`WebhookChannel` so tests can feed a static
    ``url`` without touching the keyring.
    """

    name: str
    format: str  # "slack" | "discord" | "raw"
    url: str
    custom_headers: dict[str, str] = field(default_factory=dict)


class WebhookChannel:
    """Outbound HTTP notification (Slack, Discord, or raw JSON).

    The URL (which is effectively a bearer token) **must** be stored in the
    OS keyring under ``tokie-webhook/<name>``; the config file only ever
    references the name. That keeps the TOML on disk copy-pasteable without
    leaking secrets.

    Instantiate via :meth:`from_config` so the keyring lookup and the
    format/headers plumbing happen in one place; the raw ``__init__`` is for
    tests that want to inject a :class:`WebhookSpec` directly.
    """

    def __init__(self, spec: WebhookSpec) -> None:
        self._spec = spec

    @property
    def name(self) -> str:
        return f"webhook:{self._spec.name}"

    @property
    def spec(self) -> WebhookSpec:
        return self._spec

    @classmethod
    def from_config(
        cls,
        *,
        name: str,
        format: str = "slack",
        custom_headers: dict[str, str] | None = None,
    ) -> WebhookChannel:
        url = _load_webhook_url(name)
        if not url:
            raise LookupError(
                f"no webhook URL stored for {_WEBHOOK_KEYRING_SERVICE}/{name}"
            )
        return cls(
            WebhookSpec(
                name=name,
                format=format.lower(),
                url=url,
                custom_headers=custom_headers or {},
            )
        )

    def dispatch(self, crossing: ThresholdCrossing) -> ChannelDispatchResult:
        payload = self._format_payload(crossing)
        headers = {"content-type": "application/json", **self._spec.custom_headers}
        try:
            response = httpx.post(
                self._spec.url,
                content=json.dumps(payload).encode("utf-8"),
                headers=headers,
                timeout=_WEBHOOK_HTTP_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            return ChannelDispatchResult(
                channel=self.name,
                ok=False,
                message=f"network error: {type(exc).__name__}",
                dispatched_at=datetime.now(UTC),
            )

        if response.status_code >= 400:
            return ChannelDispatchResult(
                channel=self.name,
                ok=False,
                message=f"http {response.status_code}",
                dispatched_at=datetime.now(UTC),
            )
        return ChannelDispatchResult(
            channel=self.name,
            ok=True,
            message=f"http {response.status_code}",
            dispatched_at=datetime.now(UTC),
        )

    def _format_payload(self, crossing: ThresholdCrossing) -> dict[str, Any]:
        if self._spec.format == "slack":
            return _format_slack(crossing)
        if self._spec.format == "discord":
            return _format_discord(crossing)
        return _format_raw(crossing)


def _format_slack(crossing: ThresholdCrossing) -> dict[str, Any]:
    """Slack incoming-webhook shape: ``text`` + optional ``attachments``."""

    pct = round(crossing.pct_used * 100)
    colour = {
        "low": "good",
        "medium": "warning",
        "high": "warning",
        "over": "danger",
    }[crossing.severity()]
    fields: list[dict[str, Any]] = [
        {"title": "used", "value": f"{crossing.used:.0f}", "short": True},
    ]
    if crossing.limit is not None:
        fields.append(
            {"title": "limit", "value": f"{crossing.limit:.0f}", "short": True}
        )
    if crossing.remaining is not None:
        fields.append(
            {
                "title": "remaining",
                "value": f"{crossing.remaining:.0f}",
                "short": True,
            }
        )
    if crossing.window_resets_at_iso:
        fields.append(
            {
                "title": "resets",
                "value": crossing.window_resets_at_iso,
                "short": False,
            }
        )
    return {
        "text": (
            f"Tokie • *{crossing.display_name}* [{crossing.account_id}] "
            f"{crossing.window_type} at *{pct}%* "
            f"(armed ≥ {crossing.threshold_pct}%)"
        ),
        "attachments": [
            {
                "color": colour,
                "fields": fields,
                "footer": f"tokie • {crossing.provider}/{crossing.product}",
            }
        ],
    }


def _format_discord(crossing: ThresholdCrossing) -> dict[str, Any]:
    """Discord incoming-webhook shape: ``content`` + optional ``embeds``."""

    pct = round(crossing.pct_used * 100)
    colour = {
        "low": 0x4CAF50,
        "medium": 0xFFC107,
        "high": 0xFF9800,
        "over": 0xF44336,
    }[crossing.severity()]
    fields: list[dict[str, Any]] = [
        {"name": "used", "value": f"{crossing.used:.0f}", "inline": True},
    ]
    if crossing.limit is not None:
        fields.append(
            {"name": "limit", "value": f"{crossing.limit:.0f}", "inline": True}
        )
    if crossing.remaining is not None:
        fields.append(
            {
                "name": "remaining",
                "value": f"{crossing.remaining:.0f}",
                "inline": True,
            }
        )
    if crossing.window_resets_at_iso:
        fields.append(
            {
                "name": "resets",
                "value": crossing.window_resets_at_iso,
                "inline": False,
            }
        )
    return {
        "content": (
            f"**Tokie** • `{crossing.display_name}` [{crossing.account_id}] "
            f"{crossing.window_type} at **{pct}%** "
            f"(armed ≥ {crossing.threshold_pct}%)"
        ),
        "embeds": [
            {
                "title": f"{crossing.provider}/{crossing.product}",
                "color": colour,
                "fields": fields,
            }
        ],
    }


def _format_raw(crossing: ThresholdCrossing) -> dict[str, Any]:
    """Minimal generic JSON payload for custom integrations."""

    return {
        "plan_id": crossing.plan_id,
        "account_id": crossing.account_id,
        "display_name": crossing.display_name,
        "provider": crossing.provider,
        "product": crossing.product,
        "window_type": crossing.window_type,
        "window_starts_at": crossing.window_starts_at_iso,
        "window_resets_at": crossing.window_resets_at_iso,
        "threshold_pct": crossing.threshold_pct,
        "pct_used": crossing.pct_used,
        "used": crossing.used,
        "limit": crossing.limit,
        "remaining": crossing.remaining,
        "severity": crossing.severity(),
    }


def _load_webhook_url(name: str) -> str | None:
    if not _HAS_KEYRING:  # pragma: no cover - keyring is an install-time dep
        return None
    try:
        return keyring.get_password(_WEBHOOK_KEYRING_SERVICE, name)
    except Exception as exc:  # pragma: no cover - backend-specific failures
        _logger.warning("keyring read failed for webhook %s: %s", name, exc)
        return None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WebhookConfig:
    """User-configured webhook entry from ``tokie.toml``.

    The URL itself is looked up in the keyring under
    ``tokie-webhook/<name>``; this dataclass only holds non-sensitive
    structural metadata.
    """

    name: str
    format: str = "slack"


class _MissingChannel:
    """Disabled placeholder channel — never dispatches; warns on lookup."""

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self._reason = reason

    def dispatch(self, crossing: ThresholdCrossing) -> ChannelDispatchResult:
        _ = crossing
        return ChannelDispatchResult(
            channel=self.name,
            ok=False,
            message=f"channel disabled: {self._reason}",
            dispatched_at=datetime.now(UTC),
        )


def build_channels(
    *,
    enable_desktop: bool,
    webhooks: list[WebhookConfig] | tuple[WebhookConfig, ...] = (),
) -> dict[str, Channel]:
    """Return a ``name -> Channel`` map usable by the engine.

    We always include the banner channel because it has no side effect and is
    safe by default. Desktop is opt-in (``enable_desktop=True``) and degrades
    gracefully to a :class:`_MissingChannel` if ``desktop-notifier`` can't
    load. Webhooks are included per-entry; each one that fails to resolve
    its keyring secret becomes a disabled placeholder so the rest still work.
    """

    channels: dict[str, Channel] = {_BANNER_CHANNEL_NAME: BannerChannel()}

    if enable_desktop:
        try:
            channels[_DESKTOP_CHANNEL_NAME] = DesktopChannel()
        except Exception as exc:
            channels[_DESKTOP_CHANNEL_NAME] = _MissingChannel(
                _DESKTOP_CHANNEL_NAME, f"{type(exc).__name__}: {exc}"
            )

    for entry in webhooks:
        key = f"webhook:{entry.name}"
        try:
            channels[key] = WebhookChannel.from_config(
                name=entry.name, format=entry.format
            )
        except LookupError as exc:
            channels[key] = _MissingChannel(key, str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            channels[key] = _MissingChannel(
                key, f"{type(exc).__name__}: {exc}"
            )
    return channels


__all__ = [
    "BannerChannel",
    "BannerLine",
    "Channel",
    "ChannelDispatchResult",
    "DesktopChannel",
    "WebhookChannel",
    "WebhookConfig",
    "WebhookSpec",
    "build_channels",
    "render_banner",
]
