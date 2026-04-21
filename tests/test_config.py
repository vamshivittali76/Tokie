"""Config load/save roundtrip and validation tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tokie_cli.config import (
    CollectorConfig,
    ConfigError,
    SubscriptionBinding,
    TokieConfig,
    config_dir,
    data_dir,
    default_config,
    default_config_path,
    load_config,
    save_config,
)


def test_default_config_uses_platform_paths() -> None:
    cfg = default_config()
    assert cfg.dashboard_host == "127.0.0.1"
    assert cfg.dashboard_port == 7878
    assert cfg.collectors == ()
    assert cfg.subscriptions == ()
    assert cfg.db_path.name == "tokie.db"
    assert cfg.audit_log_path.name == "audit.log"


def test_config_dir_respects_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKIE_CONFIG_HOME", str(tmp_path))
    assert config_dir() == tmp_path


def test_data_dir_respects_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKIE_DATA_HOME", str(tmp_path))
    assert data_dir() == tmp_path


def test_load_config_returns_default_when_file_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope.toml"
    cfg = load_config(missing)
    assert cfg.dashboard_port == 7878


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    cfg = TokieConfig(
        db_path=tmp_path / "db.sqlite",
        audit_log_path=tmp_path / "audit.log",
        dashboard_host="127.0.0.1",
        dashboard_port=9999,
        collectors=(
            CollectorConfig(name="claude-code", enabled=True),
            CollectorConfig(
                name="anthropic-api",
                enabled=False,
                settings={"base_url": "https://api.anthropic.com"},
            ),
        ),
        subscriptions=(SubscriptionBinding(plan_id="claude_pro_personal", account_id="me"),),
    )
    target = tmp_path / "tokie.toml"
    written = save_config(cfg, target)
    assert written == target

    loaded = load_config(target)
    assert loaded.dashboard_port == 9999
    assert [c.name for c in loaded.collectors] == ["claude-code", "anthropic-api"]
    assert loaded.collectors[1].enabled is False
    assert loaded.collectors[1].settings == {"base_url": "https://api.anthropic.com"}
    assert loaded.subscriptions[0].plan_id == "claude_pro_personal"


def test_with_collector_replaces_by_name() -> None:
    cfg = default_config().with_collector(CollectorConfig(name="openai-api", enabled=True))
    cfg2 = cfg.with_collector(CollectorConfig(name="openai-api", enabled=False))
    assert len(cfg2.collectors) == 1
    assert cfg2.collectors[0].enabled is False


def test_with_subscription_replaces_by_composite_key() -> None:
    cfg = default_config().with_subscription(
        SubscriptionBinding(plan_id="claude_pro_personal", account_id="me")
    )
    cfg2 = cfg.with_subscription(
        SubscriptionBinding(plan_id="claude_pro_personal", account_id="me")
    )
    assert len(cfg2.subscriptions) == 1


def test_load_config_rejects_bad_port(tmp_path: Path) -> None:
    path = tmp_path / "tokie.toml"
    path.write_text("[core]\ndashboard_port = 0\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_load_config_rejects_bad_host(tmp_path: Path) -> None:
    path = tmp_path / "tokie.toml"
    path.write_text("[core]\ndashboard_host = 42\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_load_config_rejects_invalid_toml(tmp_path: Path) -> None:
    path = tmp_path / "tokie.toml"
    path.write_text("not = = valid", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_load_config_rejects_bad_collector_shape(tmp_path: Path) -> None:
    path = tmp_path / "tokie.toml"
    path.write_text('[[collectors]]\nname = ""\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_load_config_rejects_bad_subscription_shape(tmp_path: Path) -> None:
    path = tmp_path / "tokie.toml"
    path.write_text('[[subscriptions]]\nplan_id = "x"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only file permissions")
def test_save_config_sets_0600_on_posix(tmp_path: Path) -> None:
    target = tmp_path / "tokie.toml"
    save_config(default_config(), target)
    mode = os.stat(target).st_mode & 0o777
    assert mode == 0o600


def test_default_config_path_points_into_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TOKIE_CONFIG_HOME", str(tmp_path))
    assert default_config_path() == tmp_path / "tokie.toml"
