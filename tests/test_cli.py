"""Tests for the Typer-based CLI.

We use Typer's :class:`CliRunner` so each command is invoked exactly the way
the installed ``tokie`` console script would be.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tokie_cli import __version__
from tokie_cli.cli import app


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect every CLI call into ``tmp_path`` so tests never touch real state."""

    monkeypatch.setenv("TOKIE_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("TOKIE_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("TOKIE_CLAUDE_SESSION_ROOT", raising=False)
    monkeypatch.delenv("TOKIE_CODEX_SESSION_ROOT", raising=False)
    monkeypatch.delenv("TOKIE_GEMINI_LOG", raising=False)
    monkeypatch.delenv("TOKIE_MANUAL_LOG", raising=False)
    monkeypatch.delenv("TOKIE_OPENAI_COMPAT_LOG", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_version_plain(runner: CliRunner) -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_version_json(runner: CliRunner) -> None:
    result = runner.invoke(app, ["version", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["tokie"] == __version__
    assert payload["plans_in_catalog"] is not None and payload["plans_in_catalog"] > 10


def test_paths_json(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["paths", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["config_dir"].startswith(str(tmp_path / "cfg"))
    assert payload["data_dir"].startswith(str(tmp_path / "data"))
    assert payload["manual_drop_dir"].endswith("manual")


def test_init_creates_config_and_db(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.stdout
    cfg_path = tmp_path / "cfg" / "tokie.toml"
    assert cfg_path.exists()
    manual_dir = tmp_path / "data" / "manual"
    assert manual_dir.exists()
    db_path = tmp_path / "data" / "tokie.db"
    assert db_path.exists()


def test_init_idempotent_without_force(runner: CliRunner) -> None:
    first = runner.invoke(app, ["init"])
    assert first.exit_code == 0
    second = runner.invoke(app, ["init"])
    assert second.exit_code == 0
    assert "already exists" in second.stdout.lower()


def test_init_force_overwrites(runner: CliRunner) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["init", "--force"])
    assert result.exit_code == 0
    assert "wrote" in result.stdout.lower()


def test_doctor_json_lists_every_collector(runner: CliRunner) -> None:
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    names = {row["collector"] for row in payload["collectors"]}
    assert {
        "claude-code",
        "codex",
        "anthropic-api",
        "openai-api",
        "gemini-api",
        "openai-compat",
        "manual",
    } <= names


def test_doctor_never_leaks_keyring_secrets(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    import keyring

    secret = "sk-admin-SHOULD-NEVER-LEAK-abc123"
    monkeypatch.setattr(keyring, "get_password", lambda service, username: secret)
    monkeypatch.setattr(
        "tokie_cli.collectors.api_anthropic.keyring.get_password",
        lambda service, username: secret,
    )
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert secret not in result.stdout
    assert secret not in (result.stderr or "")


def test_status_without_db_is_graceful(runner: CliRunner) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "no database" in result.stdout.lower()


def test_status_after_init_shows_empty_table(runner: CliRunner) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "no events yet" in result.stdout.lower()


def test_status_json_returns_structured_payload(runner: CliRunner) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["grand_total_events"] == 0
    assert payload["totals"] == []


def test_scan_unknown_collector_is_rejected(runner: CliRunner) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["scan", "--collector", "does-not-exist"])
    assert result.exit_code != 0


def test_scan_with_since_invalid_is_rejected(runner: CliRunner) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(
        app,
        ["scan", "--collector", "manual", "--since", "not-a-date"],
    )
    assert result.exit_code != 0


def test_scan_manual_happy_path(runner: CliRunner, tmp_path: Path) -> None:
    runner.invoke(app, ["init"])
    manual_dir = tmp_path / "data" / "manual"
    manual_dir.mkdir(parents=True, exist_ok=True)
    (manual_dir / "log.csv").write_text(
        "occurred_at,provider,product,account_id,model,input_tokens,output_tokens,cost_usd,notes,messages\n"
        "2026-04-20T10:00:00Z,manus,manus-web,default,manus-v2,0,0,0.5,example,\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["scan", "--collector", "manual"])
    assert result.exit_code == 0, result.stdout
    assert "total new events: 1" in result.stdout

    status_result = runner.invoke(app, ["status", "--json"])
    payload = json.loads(status_result.stdout)
    assert payload["grand_total_events"] == 1
    assert payload["totals"][0]["provider"] == "manus"


def test_plans_lists_bundled_catalog(runner: CliRunner) -> None:
    result = runner.invoke(app, ["plans", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    ids = {p["id"] for p in payload["plans"]}
    assert "claude_pro_personal" in ids
    assert "manus_personal" in ids
    assert "google_gemini_api" in ids


def test_plans_filter_by_web_only_manual(runner: CliRunner) -> None:
    result = runner.invoke(app, ["plans", "--tier", "web_only_manual", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert all(p["trackability"] == "web_only_manual" for p in payload["plans"])
    assert len(payload["plans"]) >= 8


def test_plans_rejects_unknown_tier(runner: CliRunner) -> None:
    result = runner.invoke(app, ["plans", "--tier", "telepathy"])
    assert result.exit_code != 0


def test_no_args_shows_help(runner: CliRunner) -> None:
    result = runner.invoke(app, [])
    assert result.exit_code == 0 or result.exit_code == 2
    combined = result.stdout + (result.stderr or "")
    assert "tokie" in combined.lower()


def test_dashboard_rejects_non_loopback_without_remote(runner: CliRunner) -> None:
    result = runner.invoke(app, ["dashboard", "--host", "0.0.0.0", "--no-open"])
    assert result.exit_code == 2
    combined = result.stdout + (result.stderr or "")
    assert "refusing to bind" in combined.lower()


def test_dashboard_starts_server_on_loopback(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: dict[str, object] = {}

    def fake_run(
        host: str = "127.0.0.1",
        port: int = 7878,
        *,
        allow_remote: bool = False,
        config: object | None = None,
    ) -> None:
        called.update(host=host, port=port, allow_remote=allow_remote)

    monkeypatch.setattr("tokie_cli.dashboard.server.run", fake_run)
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["dashboard", "--host", "127.0.0.1", "--port", "7878", "--no-open"])
    assert result.exit_code == 0, result.stdout
    assert called["host"] == "127.0.0.1"
    assert called["port"] == 7878
    assert called["allow_remote"] is False


def test_dashboard_remote_flag_prints_warning(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(**kwargs: object) -> None:
        return None

    monkeypatch.setattr("tokie_cli.dashboard.server.run", fake_run)
    runner.invoke(app, ["init"])
    result = runner.invoke(
        app,
        [
            "dashboard",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
            "--remote",
            "--no-open",
        ],
    )
    assert result.exit_code == 0, result.stdout
    combined = result.stdout + (result.stderr or "")
    assert "non-loopback" in combined.lower() or "no auth" in combined.lower()
