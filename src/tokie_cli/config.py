"""Tokie configuration and platform paths.

Config lives at ``$CONFIG_DIR/tokie/tokie.toml`` (per ``platformdirs``); user
data lives at ``$DATA_DIR/tokie/`` (``tokie.db`` + ``audit.log``). Secrets never
live in the config — they go through the OS keyring. ``tokie.toml`` holds only
non-sensitive structural settings.

See sections 5 and 12 of TOKIE_DEVELOPMENT_PLAN_FINAL.md for the security
constraints this module enforces.
"""

from __future__ import annotations

import contextlib
import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import tomli_w
from platformdirs import PlatformDirs

_PLATFORM = PlatformDirs(appname="tokie", appauthor=False, roaming=False)


def config_dir() -> Path:
    """Return the per-user config directory (respects ``TOKIE_CONFIG_HOME``)."""

    override = os.environ.get("TOKIE_CONFIG_HOME")
    if override:
        return Path(override).expanduser()
    return Path(_PLATFORM.user_config_dir)


def data_dir() -> Path:
    """Return the per-user data directory (respects ``TOKIE_DATA_HOME``)."""

    override = os.environ.get("TOKIE_DATA_HOME")
    if override:
        return Path(override).expanduser()
    return Path(_PLATFORM.user_data_dir)


def default_config_path() -> Path:
    return config_dir() / "tokie.toml"


def default_db_path() -> Path:
    return data_dir() / "tokie.db"


def default_audit_log_path() -> Path:
    return data_dir() / "audit.log"


@dataclass(frozen=True)
class CollectorConfig:
    """Per-collector settings block stored in ``tokie.toml``.

    ``settings`` is a free-form dict for collector-specific tuning (e.g. the
    Codex sessions directory, an OpenAI-compatible base URL). Secrets never
    live here — collectors that need them pull from the OS keyring.
    """

    name: str
    enabled: bool = True
    settings: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SubscriptionBinding:
    """Links a local ``account_id`` to a bundled plan template."""

    plan_id: str
    account_id: str


@dataclass(frozen=True)
class ThresholdRuleConfig:
    """User-editable threshold rule stored under ``[[thresholds]]``.

    Mirrors :class:`tokie_cli.alerts.thresholds.ThresholdRule` but stays in
    this module so ``tokie.toml`` can be parsed without importing the alerts
    package (which pulls in ``httpx`` / ``desktop_notifier``). The engine
    converts these to runtime :class:`ThresholdRule` objects on demand.
    """

    plan_id: str | None = None
    account_id: str | None = None
    levels: tuple[int, ...] = (75, 95, 100)
    channels: tuple[str, ...] = ("banner",)


@dataclass(frozen=True)
class WebhookConfigEntry:
    """One ``[[channels.webhook]]`` entry: name + format.

    The secret URL lives in the OS keyring (``tokie-webhook/<name>``) and
    never in the TOML file; see :mod:`tokie_cli.alerts.channels` for the
    lookup contract.
    """

    name: str
    format: str = "slack"


@dataclass(frozen=True)
class TokieConfig:
    """Root config object. Immutable — use :func:`replace` to edit."""

    db_path: Path
    audit_log_path: Path
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 7878
    collectors: tuple[CollectorConfig, ...] = ()
    subscriptions: tuple[SubscriptionBinding, ...] = ()
    thresholds: tuple[ThresholdRuleConfig, ...] = ()
    webhooks: tuple[WebhookConfigEntry, ...] = ()
    alerts_desktop_enabled: bool = False

    def with_collector(self, collector: CollectorConfig) -> TokieConfig:
        """Return a new config with ``collector`` appended or replaced by name."""

        kept = tuple(c for c in self.collectors if c.name != collector.name)
        return replace(self, collectors=(*kept, collector))

    def with_subscription(self, binding: SubscriptionBinding) -> TokieConfig:
        """Return a new config with ``binding`` appended or replaced by id."""

        kept = tuple(
            b
            for b in self.subscriptions
            if not (b.plan_id == binding.plan_id and b.account_id == binding.account_id)
        )
        return replace(self, subscriptions=(*kept, binding))

    def with_threshold(self, rule: ThresholdRuleConfig) -> TokieConfig:
        """Return a new config with ``rule`` appended (or replaced by identity).

        Identity is ``(plan_id, account_id)`` — editing a rule targeted at a
        specific binding overwrites in place; adding a new global rule just
        appends.
        """

        kept = tuple(
            r
            for r in self.thresholds
            if not (r.plan_id == rule.plan_id and r.account_id == rule.account_id)
        )
        return replace(self, thresholds=(*kept, rule))

    def without_threshold(
        self, *, plan_id: str | None, account_id: str | None
    ) -> TokieConfig:
        """Remove the rule that exactly matches this ``(plan_id, account_id)``."""

        kept = tuple(
            r
            for r in self.thresholds
            if not (r.plan_id == plan_id and r.account_id == account_id)
        )
        return replace(self, thresholds=kept)


class ConfigError(Exception):
    """Raised when the config file is malformed or references invalid values."""


def default_config() -> TokieConfig:
    """A bare, defaults-only config suitable for first-run bootstrap."""

    return TokieConfig(
        db_path=default_db_path(),
        audit_log_path=default_audit_log_path(),
    )


def _parse_collectors(raw: Any) -> tuple[CollectorConfig, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("'collectors' must be an array of tables")
    out: list[CollectorConfig] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"collectors[{i}] must be a table")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"collectors[{i}].name must be a non-empty string")
        enabled = bool(entry.get("enabled", True))
        settings_raw = entry.get("settings", {})
        if not isinstance(settings_raw, dict):
            raise ConfigError(f"collectors[{i}].settings must be a table")
        settings = {str(k): str(v) for k, v in settings_raw.items()}
        out.append(CollectorConfig(name=name, enabled=enabled, settings=settings))
    return tuple(out)


def _parse_subscriptions(raw: Any) -> tuple[SubscriptionBinding, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("'subscriptions' must be an array of tables")
    out: list[SubscriptionBinding] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"subscriptions[{i}] must be a table")
        plan_id = entry.get("plan_id")
        account_id = entry.get("account_id")
        if not isinstance(plan_id, str) or not plan_id:
            raise ConfigError(f"subscriptions[{i}].plan_id must be a non-empty string")
        if not isinstance(account_id, str) or not account_id:
            raise ConfigError(f"subscriptions[{i}].account_id must be a non-empty string")
        out.append(SubscriptionBinding(plan_id=plan_id, account_id=account_id))
    return tuple(out)


def _parse_thresholds(raw: Any) -> tuple[ThresholdRuleConfig, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("'thresholds' must be an array of tables")
    out: list[ThresholdRuleConfig] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"thresholds[{i}] must be a table")
        plan_id = entry.get("plan_id")
        if plan_id is not None and not isinstance(plan_id, str):
            raise ConfigError(f"thresholds[{i}].plan_id must be a string or omitted")
        account_id = entry.get("account_id")
        if account_id is not None and not isinstance(account_id, str):
            raise ConfigError(f"thresholds[{i}].account_id must be a string or omitted")
        levels_raw = entry.get("levels", [75, 95, 100])
        if not isinstance(levels_raw, list):
            raise ConfigError(f"thresholds[{i}].levels must be an array of integers")
        levels: list[int] = []
        for lvl in levels_raw:
            if not isinstance(lvl, int) or isinstance(lvl, bool):
                raise ConfigError(f"thresholds[{i}].levels entries must be integers")
            levels.append(lvl)
        channels_raw = entry.get("channels", ["banner"])
        if not isinstance(channels_raw, list) or not all(
            isinstance(c, str) for c in channels_raw
        ):
            raise ConfigError(f"thresholds[{i}].channels must be an array of strings")
        out.append(
            ThresholdRuleConfig(
                plan_id=plan_id or None,
                account_id=account_id or None,
                levels=tuple(levels),
                channels=tuple(channels_raw),
            )
        )
    return tuple(out)


def _parse_channels(raw: Any) -> tuple[tuple[WebhookConfigEntry, ...], bool]:
    """Return ``(webhooks, alerts_desktop_enabled)`` for the ``[channels]`` table.

    Shape::

        [channels]
        desktop = true   # opt in to desktop notifications

        [[channels.webhook]]
        name = "team-slack"
        format = "slack"
    """

    if raw is None:
        return ((), False)
    if not isinstance(raw, dict):
        raise ConfigError("'channels' must be a table")
    desktop_enabled = bool(raw.get("desktop", False))
    webhook_raw = raw.get("webhook", [])
    if not isinstance(webhook_raw, list):
        raise ConfigError("'channels.webhook' must be an array of tables")
    webhooks: list[WebhookConfigEntry] = []
    for i, entry in enumerate(webhook_raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"channels.webhook[{i}] must be a table")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"channels.webhook[{i}].name must be a non-empty string")
        fmt = entry.get("format", "slack")
        if not isinstance(fmt, str) or fmt.lower() not in {"slack", "discord", "raw"}:
            raise ConfigError(
                f"channels.webhook[{i}].format must be one of: slack, discord, raw"
            )
        webhooks.append(WebhookConfigEntry(name=name, format=fmt.lower()))
    return (tuple(webhooks), desktop_enabled)


def load_config(path: Path | str | None = None) -> TokieConfig:
    """Load config from ``path`` or the default location.

    Returns :func:`default_config` if the file does not exist.
    Raises :class:`ConfigError` if the file is malformed.
    """

    target = Path(path) if path else default_config_path()
    if not target.exists():
        return default_config()
    try:
        data = tomllib.loads(target.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {target}: {exc}") from exc

    core = data.get("core", {})
    if not isinstance(core, dict):
        raise ConfigError("'core' must be a table")

    db_path_str = core.get("db_path")
    audit_path_str = core.get("audit_log_path")
    db_path = Path(db_path_str).expanduser() if db_path_str else default_db_path()
    audit_path = Path(audit_path_str).expanduser() if audit_path_str else default_audit_log_path()

    host = core.get("dashboard_host", "127.0.0.1")
    port = core.get("dashboard_port", 7878)
    if not isinstance(host, str):
        raise ConfigError("'core.dashboard_host' must be a string")
    if not isinstance(port, int) or not (0 < port < 65536):
        raise ConfigError("'core.dashboard_port' must be an integer in (0, 65536)")

    webhooks, desktop_enabled = _parse_channels(data.get("channels"))
    return TokieConfig(
        db_path=db_path,
        audit_log_path=audit_path,
        dashboard_host=host,
        dashboard_port=port,
        collectors=_parse_collectors(data.get("collectors")),
        subscriptions=_parse_subscriptions(data.get("subscriptions")),
        thresholds=_parse_thresholds(data.get("thresholds")),
        webhooks=webhooks,
        alerts_desktop_enabled=desktop_enabled,
    )


def save_config(config: TokieConfig, path: Path | str | None = None) -> Path:
    """Write the config to disk (creating parent dirs) with mode 0600 on POSIX.

    Returns the path that was written.
    """

    target = Path(path) if path else default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {
        "core": {
            "db_path": str(config.db_path),
            "audit_log_path": str(config.audit_log_path),
            "dashboard_host": config.dashboard_host,
            "dashboard_port": config.dashboard_port,
        },
        "collectors": [
            {"name": c.name, "enabled": c.enabled, "settings": c.settings}
            for c in config.collectors
        ],
        "subscriptions": [
            {"plan_id": b.plan_id, "account_id": b.account_id} for b in config.subscriptions
        ],
        "thresholds": [
            {
                **({"plan_id": r.plan_id} if r.plan_id is not None else {}),
                **({"account_id": r.account_id} if r.account_id is not None else {}),
                "levels": list(r.levels),
                "channels": list(r.channels),
            }
            for r in config.thresholds
        ],
        "channels": {
            "desktop": bool(config.alerts_desktop_enabled),
            "webhook": [
                {"name": w.name, "format": w.format} for w in config.webhooks
            ],
        },
    }

    target.write_bytes(tomli_w.dumps(payload).encode("utf-8"))
    if hasattr(os, "chmod") and os.name == "posix":
        with contextlib.suppress(OSError):
            os.chmod(target, 0o600)
    return target


__all__ = [
    "CollectorConfig",
    "ConfigError",
    "SubscriptionBinding",
    "ThresholdRuleConfig",
    "TokieConfig",
    "WebhookConfigEntry",
    "config_dir",
    "data_dir",
    "default_audit_log_path",
    "default_config",
    "default_config_path",
    "default_db_path",
    "load_config",
    "save_config",
]
