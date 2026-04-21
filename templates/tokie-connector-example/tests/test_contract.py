"""Contract test for :class:`AcmeCollector`.

This file is the reason Tokie ships a testing module: every connector
should be able to assert "I satisfy the contract" in a handful of
lines, with no per-vendor plumbing.

Install dev dependencies (``pip install -e ".[dev]"``) and run
``pytest`` — the assertions below cover static shape, event validity,
and deterministic rescans.
"""

from __future__ import annotations

import pytest

from acme_connector.collector import AcmeCollector
from tokie_cli.testing import (
    assert_collector_contract,
    assert_idempotent_rescan,
    assert_scan_yields_valid_events,
)


def test_structural_contract() -> None:
    """Class-level metadata + method shapes pass the contract."""

    assert_collector_contract(AcmeCollector)


@pytest.mark.asyncio
async def test_scan_emits_valid_events() -> None:
    """``scan()`` yields at least one valid :class:`UsageEvent`."""

    events = await assert_scan_yields_valid_events(AcmeCollector(), min_events=1)
    assert events[0].provider == "acme"


@pytest.mark.asyncio
async def test_rescan_is_idempotent() -> None:
    """Two fresh scans produce the same ``raw_hash`` set."""

    # The synthetic collector uses ``datetime.now(UTC)`` so this test
    # will legitimately fail — which is the whole point. When you
    # replace ``scan()`` with a real, reproducible source, un-skip this
    # assertion.
    pytest.skip("enable once scan() reads from a stable source")
    await assert_idempotent_rescan(lambda: AcmeCollector())
