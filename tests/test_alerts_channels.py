"""Tests for channel dispatch: banner, webhook, desktop registry."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import httpx
import pytest

from tokie_cli.alerts.channels import (
    BannerChannel,
    WebhookChannel,
    WebhookConfig,
    WebhookSpec,
    _format_discord,
    _format_raw,
    _format_slack,
    build_channels,
    render_banner,
)
from tokie_cli.alerts.thresholds import ThresholdCrossing


def _crossing(
    *,
    pct_used: float = 0.97,
    threshold_pct: int = 95,
    plan_id: str = "claude_pro",
) -> ThresholdCrossing:
    return ThresholdCrossing(
        plan_id=plan_id,
        account_id="default",
        display_name="Claude Pro",
        provider="anthropic",
        product="claude",
        window_type="rolling_5h",
        window_starts_at_iso="2026-04-20T10:00:00+00:00",
        window_resets_at_iso="2026-04-20T15:00:00+00:00",
        threshold_pct=threshold_pct,
        pct_used=pct_used,
        used=pct_used * 1000,
        limit=1000.0,
        remaining=(1 - pct_used) * 1000,
        channels=("banner",),
    )


def test_banner_channel_is_always_ok() -> None:
    channel = BannerChannel()
    result = channel.dispatch(_crossing())
    assert result.ok is True
    assert result.channel == "banner"


def test_render_banner_empty_for_no_crossings() -> None:
    assert render_banner([]) == []


def test_render_banner_collapses_overflow() -> None:
    crossings = [_crossing(plan_id=f"plan_{i}", threshold_pct=75) for i in range(8)]
    lines = render_banner(crossings, max_lines=3)
    assert len(lines) == 4  # 3 real + 1 summary
    assert "more threshold(s) armed" in lines[-1].text


def test_render_banner_sorts_by_severity() -> None:
    low = _crossing(pct_used=0.76, threshold_pct=75, plan_id="a")
    over = _crossing(pct_used=1.01, threshold_pct=100, plan_id="b")
    lines = render_banner([low, over])
    assert lines[0].severity == "over"
    assert lines[1].severity == "medium"


def test_slack_payload_shape() -> None:
    payload = _format_slack(_crossing())
    assert "text" in payload
    assert payload["attachments"][0]["color"] == "warning"
    fields = {f["title"]: f["value"] for f in payload["attachments"][0]["fields"]}
    assert fields["used"] == "970"
    assert fields["limit"] == "1000"


def test_discord_payload_shape() -> None:
    payload = _format_discord(_crossing())
    assert "content" in payload
    assert payload["embeds"][0]["color"] == 0xFF9800  # high
    names = {f["name"] for f in payload["embeds"][0]["fields"]}
    assert {"used", "limit", "remaining", "resets"} <= names


def test_raw_payload_is_flat_json() -> None:
    payload = _format_raw(_crossing())
    assert payload["plan_id"] == "claude_pro"
    assert payload["severity"] == "high"


def test_slack_payload_color_over() -> None:
    crossing = _crossing(pct_used=1.05, threshold_pct=100)
    payload = _format_slack(crossing)
    assert payload["attachments"][0]["color"] == "danger"


def test_webhook_channel_posts_slack_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: dict[str, Any] = {}

    class FakeResponse:
        status_code = 200

    def fake_post(
        url: str, *, content: bytes, headers: dict[str, str], timeout: float
    ) -> FakeResponse:
        posted["url"] = url
        posted["content"] = content
        posted["headers"] = headers
        return FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)
    spec = WebhookSpec(
        name="slack-test",
        format="slack",
        url="https://hooks.slack.com/services/XXX",
        custom_headers={},
    )
    channel = WebhookChannel(spec)
    result = channel.dispatch(_crossing())
    assert result.ok is True
    assert posted["url"].startswith("https://hooks.slack.com/")
    assert b"attachments" in posted["content"]


def test_webhook_channel_network_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(*args: Any, **kwargs: Any) -> Any:
        raise httpx.ConnectError("simulated")

    monkeypatch.setattr(httpx, "post", fake_post)
    channel = WebhookChannel(
        WebhookSpec(name="x", format="raw", url="https://example.invalid/hook")
    )
    result = channel.dispatch(_crossing())
    assert result.ok is False
    assert "network error" in result.message


def test_webhook_channel_4xx_is_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        status_code = 404

    def fake_post(*args: Any, **kwargs: Any) -> FakeResponse:
        return FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)
    channel = WebhookChannel(
        WebhookSpec(name="x", format="raw", url="https://example.invalid/hook")
    )
    result = channel.dispatch(_crossing())
    assert result.ok is False
    assert "404" in result.message


def test_build_channels_banner_always_present() -> None:
    channels = build_channels(enable_desktop=False)
    assert "banner" in channels


def test_build_channels_missing_webhook_secret_is_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tokie_cli.alerts import channels as mod

    monkeypatch.setattr(mod, "_load_webhook_url", lambda name: None)
    channels = build_channels(
        enable_desktop=False,
        webhooks=[WebhookConfig(name="orphan", format="slack")],
    )
    ch = channels["webhook:orphan"]
    # Dispatch returns a disabled result, doesn't raise.
    result = ch.dispatch(_crossing())
    assert result.ok is False
    assert "disabled" in result.message.lower()


def test_build_channels_configured_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    from tokie_cli.alerts import channels as mod

    monkeypatch.setattr(
        mod, "_load_webhook_url", lambda name: "https://hooks.slack.com/fake"
    )
    channels = build_channels(
        enable_desktop=False,
        webhooks=[WebhookConfig(name="team", format="slack")],
    )
    assert isinstance(channels["webhook:team"], WebhookChannel)
    assert channels["webhook:team"].spec.url == "https://hooks.slack.com/fake"


def test_webhook_spec_respects_custom_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class FakeResponse:
        status_code = 204

    def fake_post(
        url: str,
        *,
        content: bytes,
        headers: dict[str, str],
        timeout: float,
    ) -> FakeResponse:
        seen["headers"] = headers
        return FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)
    spec = WebhookSpec(
        name="x",
        format="raw",
        url="https://example.invalid",
        custom_headers={"x-tokie-trace": "yes"},
    )
    channel = WebhookChannel(spec)
    channel.dispatch(_crossing())
    assert seen["headers"]["x-tokie-trace"] == "yes"
    assert seen["headers"]["content-type"] == "application/json"


def test_banner_line_includes_pct_value() -> None:
    crossing = replace(_crossing(), pct_used=0.83)
    lines = render_banner([crossing])
    assert "83%" in lines[0].text
    assert "≥ 95%" in lines[0].text
