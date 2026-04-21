"""Tests for :class:`tokie_cli.collectors.api_openai.OpenAIAPICollector`.

Every test drives the collector with :class:`httpx.MockTransport` so no real
HTTP traffic happens. The keyring is monkeypatched end-to-end — no test is
allowed to touch the host keychain.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from tokie_cli.collectors import api_openai
from tokie_cli.collectors.api_openai import OpenAIAPICollector
from tokie_cli.collectors.base import CollectorError
from tokie_cli.schema import Confidence, UsageEvent

SECRET_KEY = "sk-admin-THIS_MUST_NEVER_LEAK_0123456789"


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse retry backoff so tests don't actually wait seconds."""

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("tokie_cli.collectors.api_openai.asyncio.sleep", _instant)


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    """Replace ``keyring.get_password`` with a dict-backed fake."""

    store: dict[tuple[str, str], str] = {}

    class _FakeKeyring:
        @staticmethod
        def get_password(service: str, username: str) -> str | None:
            return store.get((service, username))

    monkeypatch.setattr(
        api_openai,
        "_load_api_key_from_keyring",
        lambda: store.get(("tokie-openai", "admin_api_key")),
    )
    return store


def _bucket(
    start_time: int,
    results: list[dict[str, Any]],
    end_time: int | None = None,
) -> dict[str, Any]:
    return {
        "object": "bucket",
        "start_time": start_time,
        "end_time": end_time or (start_time + 3600),
        "results": results,
    }


def _result(
    *,
    input_tokens: int = 1000,
    output_tokens: int = 500,
    input_cached_tokens: int = 0,
    model: str = "gpt-4o",
    project_id: str | None = None,
    user_id: str | None = None,
    api_key_id: str | None = None,
) -> dict[str, Any]:
    return {
        "object": "organization.usage.completions.result",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "input_cached_tokens": input_cached_tokens,
        "input_audio_tokens": 0,
        "output_audio_tokens": 0,
        "num_model_requests": 10,
        "project_id": project_id,
        "user_id": user_id,
        "api_key_id": api_key_id,
        "model": model,
        "batch": False,
    }


def _transport_from(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def _collect(collector: OpenAIAPICollector) -> list[UsageEvent]:
    events: list[UsageEvent] = []
    scanner: AsyncIterator[UsageEvent] = collector.scan()
    async for evt in scanner:
        events.append(evt)
    return events


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_single_page_happy_path_emits_one_event_per_result() -> None:
    start = 1_713_571_200
    body = {
        "object": "page",
        "data": [
            _bucket(
                start,
                [
                    _result(
                        input_tokens=1000,
                        output_tokens=500,
                        input_cached_tokens=200,
                        model="gpt-4o",
                        project_id="proj_abc",
                    )
                ],
            )
        ],
        "has_more": False,
        "next_page": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/organization/usage/completions"
        assert request.headers["Authorization"] == f"Bearer {SECRET_KEY}"
        return httpx.Response(200, json=body)

    collector = OpenAIAPICollector(
        api_key=SECRET_KEY,
        bucket_width="1h",
        account_id="acct-main",
        transport=_transport_from(handler),
    )
    events = await _collect(collector)

    assert len(events) == 1
    evt = events[0]
    assert evt.provider == "openai"
    assert evt.product == "openai-api"
    assert evt.account_id == "acct-main"
    assert evt.session_id is None
    assert evt.project == "proj_abc"
    assert evt.model == "gpt-4o"
    assert evt.input_tokens == 1000
    assert evt.output_tokens == 500
    assert evt.cache_read_tokens == 200
    assert evt.cache_write_tokens == 0
    assert evt.reasoning_tokens == 0
    assert evt.cost_usd is None
    assert evt.confidence is Confidence.EXACT
    assert evt.occurred_at == datetime.fromtimestamp(start, tz=UTC)
    assert evt.source == f"openai_api:1h:{start}:gpt-4o"
    assert len(evt.raw_hash) == 64


async def test_project_id_flows_through_to_event_project() -> None:
    start = 1_713_600_000
    body = {
        "object": "page",
        "data": [_bucket(start, [_result(project_id="proj_marketing")])],
        "has_more": False,
        "next_page": None,
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    collector = OpenAIAPICollector(api_key=SECRET_KEY, transport=_transport_from(handler))
    events = await _collect(collector)

    assert len(events) == 1
    assert events[0].project == "proj_marketing"


async def test_multiple_results_in_one_bucket_yield_multiple_events() -> None:
    start = 1_713_700_000
    body = {
        "object": "page",
        "data": [
            _bucket(
                start,
                [
                    _result(model="gpt-4o", input_tokens=100, output_tokens=50),
                    _result(model="gpt-4o-mini", input_tokens=200, output_tokens=75),
                    _result(model="o4-mini", input_tokens=10, output_tokens=5),
                ],
            )
        ],
        "has_more": False,
        "next_page": None,
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    collector = OpenAIAPICollector(api_key=SECRET_KEY, transport=_transport_from(handler))
    events = await _collect(collector)

    assert {e.model for e in events} == {"gpt-4o", "gpt-4o-mini", "o4-mini"}
    assert len(events) == 3


async def test_empty_data_array_yields_no_events() -> None:
    body = {"object": "page", "data": [], "has_more": False, "next_page": None}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    collector = OpenAIAPICollector(api_key=SECRET_KEY, transport=_transport_from(handler))
    events = await _collect(collector)

    assert events == []


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


async def test_two_page_pagination_followed_via_next_page_cursor() -> None:
    page1 = {
        "object": "page",
        "data": [_bucket(1_713_571_200, [_result(model="gpt-4o")])],
        "has_more": True,
        "next_page": "cursor-xyz",
    }
    page2 = {
        "object": "page",
        "data": [_bucket(1_713_574_800, [_result(model="gpt-4o-mini")])],
        "has_more": False,
        "next_page": None,
    }
    calls: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        calls.append(params)
        if "page" not in params:
            return httpx.Response(200, json=page1)
        assert params["page"] == "cursor-xyz"
        return httpx.Response(200, json=page2)

    collector = OpenAIAPICollector(api_key=SECRET_KEY, transport=_transport_from(handler))
    events = await _collect(collector)

    assert len(events) == 2
    assert [e.model for e in events] == ["gpt-4o", "gpt-4o-mini"]
    assert len(calls) == 2
    assert "page" not in calls[0]
    assert calls[1]["page"] == "cursor-xyz"


# ---------------------------------------------------------------------------
# since handling
# ---------------------------------------------------------------------------


async def test_since_converted_to_unix_start_time() -> None:
    since = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    expected = int(since.timestamp())
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(
            200,
            json={"object": "page", "data": [], "has_more": False, "next_page": None},
        )

    collector = OpenAIAPICollector(api_key=SECRET_KEY, transport=_transport_from(handler))
    async for _ in collector.scan(since=since):
        pass

    assert int(captured["start_time"]) == expected
    assert captured["bucket_width"] == "1h"


async def test_since_none_defaults_to_thirty_days_ago() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(
            200,
            json={"object": "page", "data": [], "has_more": False, "next_page": None},
        )

    collector = OpenAIAPICollector(api_key=SECRET_KEY, transport=_transport_from(handler))
    before = datetime.now(UTC)
    async for _ in collector.scan():
        pass
    after = datetime.now(UTC)

    start = int(captured["start_time"])
    lower = int((before - timedelta(days=30, seconds=5)).timestamp())
    upper = int((after - timedelta(days=30)).timestamp()) + 5
    assert lower <= start <= upper


# ---------------------------------------------------------------------------
# Auth / errors
# ---------------------------------------------------------------------------


async def test_401_raises_collector_error_and_does_not_leak_key() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "no"}})

    collector = OpenAIAPICollector(api_key=SECRET_KEY, transport=_transport_from(handler))

    with pytest.raises(CollectorError) as excinfo:
        await _collect(collector)

    assert "authentication failed" in str(excinfo.value)
    assert SECRET_KEY not in str(excinfo.value)
    assert SECRET_KEY not in repr(excinfo.value)


async def test_403_also_raises_authentication_failed() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "wrong key type"}})

    collector = OpenAIAPICollector(api_key=SECRET_KEY, transport=_transport_from(handler))
    with pytest.raises(CollectorError) as excinfo:
        await _collect(collector)

    assert "authentication failed" in str(excinfo.value)


async def test_key_never_appears_in_collector_repr() -> None:
    collector = OpenAIAPICollector(api_key=SECRET_KEY)
    assert SECRET_KEY not in repr(collector)
    assert SECRET_KEY not in str(collector)


async def test_missing_credential_raises_configuration_error(
    fake_keyring: dict[tuple[str, str], str],
) -> None:
    assert fake_keyring == {}
    collector = OpenAIAPICollector()
    with pytest.raises(CollectorError) as excinfo:
        await _collect(collector)
    msg = str(excinfo.value)
    assert "not configured" in msg
    assert "tokie init" in msg


# ---------------------------------------------------------------------------
# Retry / transport errors
# ---------------------------------------------------------------------------


async def test_429_then_success_is_retried() -> None:
    body = {
        "object": "page",
        "data": [_bucket(1_713_571_200, [_result()])],
        "has_more": False,
        "next_page": None,
    }
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json=body)

    collector = OpenAIAPICollector(api_key=SECRET_KEY, transport=_transport_from(handler))
    events = await _collect(collector)

    assert calls["n"] == 2
    assert len(events) == 1


async def test_persistent_429_raises_collector_error_without_key() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    collector = OpenAIAPICollector(api_key=SECRET_KEY, transport=_transport_from(handler))
    with pytest.raises(CollectorError) as excinfo:
        await _collect(collector)

    assert "unavailable" in str(excinfo.value)
    assert SECRET_KEY not in str(excinfo.value)


async def test_persistent_500_raises_collector_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    collector = OpenAIAPICollector(api_key=SECRET_KEY, transport=_transport_from(handler))
    with pytest.raises(CollectorError) as excinfo:
        await _collect(collector)

    assert "unavailable" in str(excinfo.value)


async def test_timeout_surfaces_as_collector_error_without_key() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    collector = OpenAIAPICollector(api_key=SECRET_KEY, transport=_transport_from(handler))
    with pytest.raises(CollectorError) as excinfo:
        await _collect(collector)

    msg = str(excinfo.value)
    assert "unavailable" in msg
    assert "timeout" in msg
    assert SECRET_KEY not in msg


# ---------------------------------------------------------------------------
# detect() / health() / hash stability
# ---------------------------------------------------------------------------


async def test_detect_returns_true_when_keyring_has_key(
    fake_keyring: dict[tuple[str, str], str],
) -> None:
    assert OpenAIAPICollector.detect() is False
    fake_keyring[("tokie-openai", "admin_api_key")] = SECRET_KEY
    assert OpenAIAPICollector.detect() is True


async def test_health_reports_configured_when_override_present() -> None:
    collector = OpenAIAPICollector(api_key=SECRET_KEY)
    h = collector.health()
    assert h.detected is True
    assert h.ok is True
    assert SECRET_KEY not in h.message


async def test_health_reports_missing_when_no_credential(
    fake_keyring: dict[tuple[str, str], str],
) -> None:
    assert fake_keyring == {}
    collector = OpenAIAPICollector()
    h = collector.health()
    assert h.detected is False
    assert h.ok is False
    assert "not configured" in h.message


async def test_raw_hash_stable_across_identical_scans() -> None:
    body = {
        "object": "page",
        "data": [
            _bucket(
                1_713_571_200,
                [_result(input_tokens=1000, output_tokens=500, input_cached_tokens=200)],
            )
        ],
        "has_more": False,
        "next_page": None,
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    c1 = OpenAIAPICollector(api_key=SECRET_KEY, transport=_transport_from(handler))
    c2 = OpenAIAPICollector(api_key=SECRET_KEY, transport=_transport_from(handler))
    ev1 = await _collect(c1)
    ev2 = await _collect(c2)

    assert len(ev1) == 1
    assert len(ev2) == 1
    assert ev1[0].raw_hash == ev2[0].raw_hash


async def test_unknown_model_falls_back_to_placeholder() -> None:
    start = 1_713_571_200
    body = {
        "object": "page",
        "data": [
            _bucket(
                start,
                [
                    {
                        "object": "organization.usage.completions.result",
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "input_cached_tokens": 0,
                        "project_id": None,
                        "model": None,
                    }
                ],
            )
        ],
        "has_more": False,
        "next_page": None,
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    collector = OpenAIAPICollector(api_key=SECRET_KEY, transport=_transport_from(handler))
    events = await _collect(collector)

    assert len(events) == 1
    assert events[0].model == "unknown"
    assert events[0].source == f"openai_api:1h:{start}:unknown"


async def test_invalid_json_body_raises_collector_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json at all")

    collector = OpenAIAPICollector(api_key=SECRET_KEY, transport=_transport_from(handler))
    with pytest.raises(CollectorError) as excinfo:
        await _collect(collector)

    assert "invalid json" in str(excinfo.value)


async def test_non_retryable_4xx_surfaces_status_without_key() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(418, content=json.dumps({"error": {"message": "teapot"}}).encode())

    collector = OpenAIAPICollector(api_key=SECRET_KEY, transport=_transport_from(handler))
    with pytest.raises(CollectorError) as excinfo:
        await _collect(collector)

    msg = str(excinfo.value)
    assert "418" in msg
    assert SECRET_KEY not in msg
