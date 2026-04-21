"""Public testing utilities for third-party Tokie connectors.

Exposed via the ``tokie-cli`` package so connector authors can

.. code-block:: python

    from tokie_cli.testing import assert_collector_contract

    def test_my_collector_is_well_formed() -> None:
        from my_connector import MyCollector
        assert_collector_contract(MyCollector)

and ship a single one-liner contract test. The heavier dynamic checks
(``scan`` yielding events, idempotent rescans) live in async helpers the
author opts into with their own fixtures.

See ``docs/CONNECTORS.md`` for the full contract in prose.
"""

from __future__ import annotations

from tokie_cli.testing.contract import (
    ContractViolation,
    ContractViolationError,
    assert_collector_contract,
    assert_event_is_valid,
    assert_idempotent_rescan,
    assert_scan_yields_valid_events,
)

__all__ = [
    "ContractViolation",
    "ContractViolationError",
    "assert_collector_contract",
    "assert_event_is_valid",
    "assert_idempotent_rescan",
    "assert_scan_yields_valid_events",
]
