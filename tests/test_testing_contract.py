"""Tests for :mod:`tokie_cli.testing.contract`.

Covers both directions of every contract check: a known-good collector
passes silently, and each specific violation raises
:class:`ContractViolation` with a human-readable message.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from tokie_cli.collectors.base import Collector, CollectorHealth
from tokie_cli.schema import Confidence, UsageEvent, compute_raw_hash
from tokie_cli.testing import (
    ContractViolation,
    assert_collector_contract,
    assert_event_is_valid,
    assert_idempotent_rescan,
    assert_scan_yields_valid_events,
)
from tokie_cli.testing.contract import assert_health_contract

NOW = datetime(2026, 4, 20, 12, tzinfo=UTC)


def _valid_event(*, raw: str = "abc", tokens: int = 10) -> UsageEvent:
    raw_hash = compute_raw_hash(raw)
    return UsageEvent(
        id=raw_hash[:16],
        collected_at=NOW,
        occurred_at=NOW,
        provider="acme",
        product="acme-cli",
        account_id="default",
        model="gpt-whatever",
        input_tokens=tokens,
        output_tokens=tokens,
        confidence=Confidence.EXACT,
        source="test",
        raw_hash=raw_hash,
    )


class _GoodCollector(Collector):
    name = "acme"
    default_confidence = Confidence.EXACT

    _SENTINEL: object = object()

    def __init__(self, *, events: list[UsageEvent] | None = None) -> None:
        if events is None:
            events = [_valid_event(raw="a"), _valid_event(raw="b")]
        self._events = events

    @classmethod
    def detect(cls) -> bool:
        return True

    def scan(self, since: datetime | None = None) -> AsyncIterator[UsageEvent]:
        events = self._events

        async def _gen() -> AsyncIterator[UsageEvent]:
            for e in events:
                yield e

        return _gen()

    def health(self) -> CollectorHealth:
        return CollectorHealth(
            name=self.name,
            detected=True,
            ok=True,
            last_scan_at=NOW,
            last_scan_events=len(self._events),
            message="ok",
        )


# ---------------------------------------------------------------------------
# assert_collector_contract — structural checks
# ---------------------------------------------------------------------------


def test_good_collector_passes_structural_contract() -> None:
    assert_collector_contract(_GoodCollector)


def test_non_class_is_rejected() -> None:
    with pytest.raises(ContractViolation, match="expected a class"):
        assert_collector_contract("not a class")  # type: ignore[arg-type]


def test_non_subclass_is_rejected() -> None:
    class _NotACollector:
        name = "x"

    with pytest.raises(ContractViolation, match="must subclass"):
        assert_collector_contract(_NotACollector)  # type: ignore[arg-type]


def test_empty_name_is_rejected() -> None:
    class _Bad(_GoodCollector):
        name = ""

    with pytest.raises(ContractViolation, match="non-empty string"):
        assert_collector_contract(_Bad)


def test_name_with_spaces_is_rejected() -> None:
    class _Bad(_GoodCollector):
        name = "has space"

    with pytest.raises(ContractViolation, match="must not contain spaces"):
        assert_collector_contract(_Bad)


def test_bad_default_confidence_is_rejected() -> None:
    class _Bad(_GoodCollector):
        default_confidence = "high"  # type: ignore[assignment]

    with pytest.raises(ContractViolation, match="default_confidence"):
        assert_collector_contract(_Bad)


def test_non_classmethod_detect_is_rejected() -> None:
    class _Bad(_GoodCollector):
        def detect(self) -> bool:  # type: ignore[override]
            return True

    with pytest.raises(ContractViolation, match="@classmethod"):
        assert_collector_contract(_Bad)


def test_non_bool_detect_return_is_rejected() -> None:
    class _Bad(_GoodCollector):
        @classmethod
        def detect(cls) -> bool:
            return "yes"  # type: ignore[return-value]

    with pytest.raises(ContractViolation, match="must return bool"):
        assert_collector_contract(_Bad)


# ---------------------------------------------------------------------------
# assert_event_is_valid
# ---------------------------------------------------------------------------


def test_valid_event_passes() -> None:
    assert_event_is_valid(_valid_event())


def test_non_event_is_rejected() -> None:
    with pytest.raises(ContractViolation, match="expected UsageEvent"):
        assert_event_is_valid("nope")  # type: ignore[arg-type]


def test_naive_datetime_is_rejected() -> None:
    good = _valid_event()
    naive = good.model_copy(
        update={"occurred_at": datetime(2026, 4, 20, 12)}
    )
    with pytest.raises(ContractViolation, match="timezone-aware"):
        assert_event_is_valid(naive)


# ---------------------------------------------------------------------------
# assert_scan_yields_valid_events + idempotent rescan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_yields_valid_events() -> None:
    events = await assert_scan_yields_valid_events(_GoodCollector(), min_events=2)
    assert len(events) == 2


@pytest.mark.asyncio
async def test_scan_min_events_enforced() -> None:
    class _Empty(_GoodCollector):
        def __init__(self) -> None:
            super().__init__(events=[])

    with pytest.raises(ContractViolation, match="expected at least 1"):
        await assert_scan_yields_valid_events(_Empty())


@pytest.mark.asyncio
async def test_scan_must_return_async_iterator() -> None:
    class _Broken(_GoodCollector):
        def scan(  # type: ignore[override]
            self, since: datetime | None = None
        ) -> list[UsageEvent]:
            return [_valid_event()]

    with pytest.raises(ContractViolation, match="AsyncIterator"):
        await assert_scan_yields_valid_events(_Broken())


@pytest.mark.asyncio
async def test_idempotent_rescan_passes_for_stable_collector() -> None:
    await assert_idempotent_rescan(lambda: _GoodCollector())


@pytest.mark.asyncio
async def test_idempotent_rescan_fails_when_hashes_diverge() -> None:
    calls = {"n": 0}

    def factory() -> Collector:
        calls["n"] += 1
        raw = "a" if calls["n"] == 1 else "b"
        return _GoodCollector(events=[_valid_event(raw=raw)])

    with pytest.raises(ContractViolation, match="not idempotent"):
        await assert_idempotent_rescan(factory)


# ---------------------------------------------------------------------------
# assert_health_contract
# ---------------------------------------------------------------------------


def test_health_contract_passes_for_good_collector() -> None:
    health = assert_health_contract(_GoodCollector())
    assert health.ok


def test_health_contract_rejects_wrong_return_type() -> None:
    class _Bad(_GoodCollector):
        def health(self) -> CollectorHealth:
            return "ok"  # type: ignore[return-value]

    with pytest.raises(ContractViolation, match="expected CollectorHealth"):
        assert_health_contract(_Bad())
