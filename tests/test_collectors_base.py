"""Contract tests for the Collector ABC.

These validate the base-class invariants so every concrete collector inherits
the same behavior for ``watch`` polling, default ``health`` reporting, and
``make_event`` defaults.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from tokie_cli.collectors.base import (
    Collector,
    CollectorError,
    CollectorHealth,
    aiterate,
)
from tokie_cli.schema import Confidence, UsageEvent, compute_raw_hash


class _FakeCollector(Collector):
    name = "fake"
    default_confidence = Confidence.ESTIMATED

    def __init__(self, events: list[UsageEvent] | None = None, detected: bool = True) -> None:
        self._events = events or []
        self._detected = detected
        self.scan_calls: list[datetime | None] = []

    @classmethod
    def detect(cls) -> bool:
        return True

    def scan(self, since: datetime | None = None) -> AsyncIterator[UsageEvent]:
        self.scan_calls.append(since)
        chosen = (
            [e for e in self._events if since is None or e.occurred_at > since]
            if self._events
            else []
        )
        return aiterate(chosen)


def _event(occurred_at: datetime, *, hash_seed: str) -> UsageEvent:
    return UsageEvent(
        id=f"evt-{hash_seed}",
        collected_at=occurred_at,
        occurred_at=occurred_at,
        provider="fake",
        product="fake-product",
        account_id="acct",
        model="fake-model",
        input_tokens=10,
        output_tokens=5,
        confidence=Confidence.EXACT,
        source="test",
        raw_hash=compute_raw_hash(hash_seed),
    )


async def _collect(iterator: AsyncIterator[UsageEvent]) -> list[UsageEvent]:
    result: list[UsageEvent] = []
    async for item in iterator:
        result.append(item)
    return result


@pytest.mark.asyncio
async def test_make_event_fills_id_and_collected_at() -> None:
    collector = _FakeCollector()
    before = datetime.now(UTC)
    event = collector.make_event(
        occurred_at=datetime(2026, 4, 20, 12, tzinfo=UTC),
        provider="anthropic",
        product="claude-code",
        account_id="acct",
        model="claude-opus",
        input_tokens=100,
        output_tokens=50,
        raw_hash="a" * 64,
        source="jsonl:/tmp/fake",
    )
    assert event.id
    assert event.collected_at >= before
    assert event.confidence is Confidence.ESTIMATED


@pytest.mark.asyncio
async def test_make_event_respects_explicit_confidence() -> None:
    collector = _FakeCollector()
    event = collector.make_event(
        occurred_at=datetime(2026, 4, 20, tzinfo=UTC),
        provider="p",
        product="prod",
        account_id="a",
        model="m",
        input_tokens=1,
        output_tokens=1,
        raw_hash="b" * 64,
        source="manual",
        confidence=Confidence.INFERRED,
    )
    assert event.confidence is Confidence.INFERRED


@pytest.mark.asyncio
async def test_scan_is_idempotent_across_runs() -> None:
    e = _event(datetime(2026, 4, 20, 10, tzinfo=UTC), hash_seed="a")
    collector = _FakeCollector(events=[e])
    first = await _collect(collector.scan())
    second = await _collect(collector.scan())
    assert [x.raw_hash for x in first] == [x.raw_hash for x in second]


@pytest.mark.asyncio
async def test_default_watch_yields_initial_events_with_none_cursor() -> None:
    e1 = _event(datetime(2026, 4, 20, 10, tzinfo=UTC), hash_seed="a")
    e2 = _event(datetime(2026, 4, 20, 11, tzinfo=UTC), hash_seed="b")
    collector = _FakeCollector(events=[e1, e2])

    async def _drain() -> list[UsageEvent]:
        out: list[UsageEvent] = []
        async for evt in collector.watch(poll_interval_sec=0.001):
            out.append(evt)
            if len(out) == 2:
                break
        return out

    result = await asyncio.wait_for(_drain(), timeout=2.0)
    assert {e.raw_hash for e in result} == {e1.raw_hash, e2.raw_hash}
    assert collector.scan_calls[0] is None


@pytest.mark.asyncio
async def test_default_watch_advances_cursor_on_next_poll() -> None:
    e1 = _event(datetime(2026, 4, 20, 10, tzinfo=UTC), hash_seed="a")
    e2 = _event(datetime(2026, 4, 20, 11, tzinfo=UTC), hash_seed="b")
    collector = _FakeCollector(events=[e1])

    async def _drain() -> None:
        count = 0
        async for _ in collector.watch(poll_interval_sec=0.01):
            count += 1
            if count == 1:
                collector._events = [e1, e2]
            if count == 2:
                break

    await asyncio.wait_for(_drain(), timeout=2.0)
    assert collector.scan_calls[0] is None
    assert any(c == e1.occurred_at for c in collector.scan_calls[1:])


@pytest.mark.asyncio
async def test_default_health_reports_detected_source() -> None:
    collector = _FakeCollector()
    h = collector.health()
    assert isinstance(h, CollectorHealth)
    assert h.name == "fake"
    assert h.detected is True
    assert h.ok is True


def test_collector_error_is_exception() -> None:
    err = CollectorError("secrets redacted")
    assert str(err) == "secrets redacted"
