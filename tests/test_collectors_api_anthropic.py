"""Tests for :mod:`tokie_cli.collectors.api_anthropic`.

Uses ``httpx.MockTransport`` so we never touch the network. Keyring is
monkeypatched at the module level — real system credentials are never
read or written.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from tokie_cli.collectors import api_anthropic
from tokie_cli.collectors.api_anthropic import AnthropicAPICollector
from tokie_cli.collectors.base import CollectorError

SECRET_KEY = "sk-ant-admin-DO-NOT-LEAK-abcdef0123456789"

SAMPLE_BUCKET: dict[str, Any] = {
    "starting_at": "2026-04-20T00:00:00Z",
    "ending_at": "2026-04-20T01:00:00Z",
    "results": [
        {
            "uncached_input_tokens": 1000,
            "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 500,
            "output_tokens": 300,
            "model": "claude-3-5-sonnet-20241022",
            "service_tier": "standard",
        }
    ],
}


Handler = Callable[[httpx.Request], httpx.Response]


def _make_collector(
    handler: Handler,
    *,
    api_key: str | None = SECRET_KEY,
    **kwargs: Any,
) -> AnthropicAPICollector:
    return AnthropicAPICollector(
        api_key=api_key,
        base_url="https://api.anthropic.com",
        _transport=httpx.MockTransport(handler),
        **kwargs,
    )


async def _drain(collector: AnthropicAPICollector, since: Any = None) -> list[Any]:
    return [e async for e in collector.scan(since=since)]


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize exponential backoff so retry tests don't actually sleep."""

    monkeypatch.setattr(api_anthropic, "BACKOFF_BASE_SEC", 0.0)


@pytest.fixture(autouse=True)
def _no_real_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no test ever touches the real OS keyring."""

    def _boom(*_a: Any, **_k: Any) -> str | None:
        raise AssertionError("real keyring access during tests is forbidden")

    monkeypatch.setattr("tokie_cli.collectors.api_anthropic.keyring.get_password", _boom)


def _patch_keyring(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    def _get(service: str, username: str) -> str | None:
        assert service == "tokie-anthropic"
        assert username == "admin_api_key"
        return value

    monkeypatch.setattr("tokie_cli.collectors.api_anthropic.keyring.get_password", _get)


async def test_success_single_page_emits_events() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == SECRET_KEY
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert request.url.path == "/v1/organizations/usage_report/messages"
        return httpx.Response(
            200, json={"data": [SAMPLE_BUCKET], "has_more": False, "next_page": None}
        )

    collector = _make_collector(handler, account_id="acme")
    events = await _drain(collector)

    assert len(events) == 1
    evt = events[0]
    assert evt.provider == "anthropic"
    assert evt.product == "anthropic-api"
    assert evt.account_id == "acme"
    assert evt.model == "claude-3-5-sonnet-20241022"
    assert evt.input_tokens == 1000
    assert evt.output_tokens == 300
    assert evt.cache_read_tokens == 500
    assert evt.cache_write_tokens == 200
    assert evt.reasoning_tokens == 0
    assert evt.cost_usd is None
    assert evt.confidence.value == "exact"
    assert evt.occurred_at.isoformat() == "2026-04-20T00:00:00+00:00"
    assert "claude-3-5-sonnet-20241022" in evt.source
    assert evt.raw_hash and len(evt.raw_hash) == 64


async def test_pagination_two_pages_stitched() -> None:
    second_bucket = {
        **SAMPLE_BUCKET,
        "starting_at": "2026-04-20T01:00:00Z",
        "ending_at": "2026-04-20T02:00:00Z",
    }
    calls: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        calls.append(page)
        if page is None:
            return httpx.Response(
                200,
                json={"data": [SAMPLE_BUCKET], "has_more": True, "next_page": "cursor-2"},
            )
        assert page == "cursor-2"
        return httpx.Response(
            200, json={"data": [second_bucket], "has_more": False, "next_page": None}
        )

    collector = _make_collector(handler)
    events = await _drain(collector)

    assert len(events) == 2
    assert calls == [None, "cursor-2"]
    assert events[0].occurred_at.isoformat() == "2026-04-20T00:00:00+00:00"
    assert events[1].occurred_at.isoformat() == "2026-04-20T01:00:00+00:00"


async def test_401_raises_collector_error_without_leaking_key() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    collector = _make_collector(handler)
    with pytest.raises(CollectorError) as excinfo:
        await _drain(collector)

    message = str(excinfo.value)
    assert "authentication failed" in message.lower()
    assert SECRET_KEY not in message


async def test_429_then_success_retries() -> None:
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(
            200, json={"data": [SAMPLE_BUCKET], "has_more": False, "next_page": None}
        )

    collector = _make_collector(handler)
    events = await _drain(collector)

    assert calls["n"] == 2
    assert len(events) == 1


async def test_persistent_429_raises_collector_error() -> None:
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, json={"error": "rate limited"})

    collector = _make_collector(handler)
    with pytest.raises(CollectorError) as excinfo:
        await _drain(collector)

    assert calls["n"] == 3
    assert "try again later" in str(excinfo.value).lower()
    assert SECRET_KEY not in str(excinfo.value)


async def test_network_timeout_raises_collector_error() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectTimeout("mocked connect timeout", request=request)

    collector = _make_collector(handler)
    with pytest.raises(CollectorError) as excinfo:
        await _drain(collector)

    assert calls["n"] == 3
    assert "unreachable" in str(excinfo.value).lower()
    assert SECRET_KEY not in str(excinfo.value)


async def test_since_is_forwarded_as_starting_at() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["starting_at"] = request.url.params["starting_at"]
        captured["bucket_width"] = request.url.params["bucket_width"]
        return httpx.Response(200, json={"data": [], "has_more": False, "next_page": None})

    from datetime import UTC, datetime

    since = datetime(2026, 4, 1, 12, 30, 0, tzinfo=UTC)
    collector = _make_collector(handler, bucket_width="1d")
    await _drain(collector, since=since)

    assert captured["starting_at"] == "2026-04-01T12:30:00Z"
    assert captured["bucket_width"] == "1d"


def test_detect_returns_true_when_credential_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_keyring(monkeypatch, SECRET_KEY)
    assert AnthropicAPICollector.detect() is True


def test_detect_returns_false_when_credential_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_keyring(monkeypatch, None)
    assert AnthropicAPICollector.detect() is False


def test_health_reports_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_keyring(monkeypatch, None)
    collector = AnthropicAPICollector()
    report = collector.health()
    assert report.detected is False
    assert report.ok is False
    assert "not configured" in report.message

    _patch_keyring(monkeypatch, SECRET_KEY)
    report = collector.health()
    assert report.detected is True
    assert report.ok is True


async def test_api_key_never_appears_in_any_error_message() -> None:
    secret_pattern = re.compile(re.escape(SECRET_KEY))
    messages: list[str] = []

    scenarios: list[Handler] = [
        lambda _r: httpx.Response(401, json={"error": {"message": "no"}}),
        lambda _r: httpx.Response(403, json={"error": {"message": "no"}}),
        lambda _r: httpx.Response(429, json={"error": {"message": "no"}}),
        lambda _r: httpx.Response(500, text="boom"),
    ]

    for handler in scenarios:
        collector = _make_collector(handler)
        with pytest.raises(CollectorError) as excinfo:
            await _drain(collector)
        messages.append(str(excinfo.value))

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    collector = _make_collector(timeout_handler)
    with pytest.raises(CollectorError) as excinfo:
        await _drain(collector)
    messages.append(str(excinfo.value))

    for msg in messages:
        assert secret_pattern.search(msg) is None, f"secret leaked in: {msg!r}"


async def test_raw_hash_is_stable_across_identical_runs() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [SAMPLE_BUCKET], "has_more": False, "next_page": None}
        )

    events_a = await _drain(_make_collector(handler))
    events_b = await _drain(_make_collector(handler))

    assert len(events_a) == 1 and len(events_b) == 1
    assert events_a[0].raw_hash == events_b[0].raw_hash


async def test_empty_response_yields_zero_events() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [], "has_more": False, "next_page": None})

    events = await _drain(_make_collector(handler))
    assert events == []


async def test_multiple_results_in_single_bucket_yield_multiple_events() -> None:
    bucket: dict[str, Any] = {
        "starting_at": "2026-04-20T00:00:00Z",
        "ending_at": "2026-04-20T01:00:00Z",
        "results": [
            {
                "uncached_input_tokens": 100,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 50,
                "model": "claude-3-5-sonnet-20241022",
                "service_tier": "standard",
            },
            {
                "uncached_input_tokens": 7,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 3,
                "model": "claude-3-5-haiku-20241022",
                "service_tier": "priority",
            },
        ],
    }

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [bucket], "has_more": False, "next_page": None})

    events = await _drain(_make_collector(handler))
    assert len(events) == 2
    models = sorted(e.model for e in events)
    assert models == ["claude-3-5-haiku-20241022", "claude-3-5-sonnet-20241022"]
    assert {e.raw_hash for e in events} == {events[0].raw_hash, events[1].raw_hash}
    assert events[0].raw_hash != events[1].raw_hash


async def test_missing_key_raises_helpful_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_keyring(monkeypatch, None)

    def handler(_req: httpx.Request) -> httpx.Response:  # pragma: no cover
        return httpx.Response(200, json={"data": []})

    collector = AnthropicAPICollector(api_key=None, _transport=httpx.MockTransport(handler))
    with pytest.raises(CollectorError, match="not configured"):
        await _drain(collector)
