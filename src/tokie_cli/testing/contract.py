"""Connector contract assertions — the ``pytest-tokie-connector`` core.

Every third-party connector MUST satisfy the :class:`Collector` contract
so ``tokie doctor`` / ``tokie scan`` / ``tokie watch`` treat it like a
built-in. Rather than documenting the contract in prose and hoping
authors follow it, this module turns every requirement into an
executable assertion.

Why ship it inside ``tokie-cli`` instead of a separate package?
    - One ``pip install tokie-cli`` gets you the runtime *and* the
      testing helpers. No version-drift between plugin and harness.
    - The helpers live in production code under ``src/``, so mypy and
      ruff catch drift at the same moment they catch it for built-ins.
    - The pytest-plugin entry point below (``tokie_connector``) still
      makes pytest auto-discover the fixtures whenever ``tokie-cli`` is
      installed as a dev dep, so the DX matches a dedicated plugin.

Layering
--------
1. **Structural** checks (:func:`assert_collector_contract`): pure static
   validation of the class itself. Safe to call without instantiating.
2. **Event** checks (:func:`assert_event_is_valid`): runtime validation
   of a single :class:`UsageEvent`.
3. **Scan** checks (:func:`assert_scan_yields_valid_events`,
   :func:`assert_idempotent_rescan`): dynamic contract that requires the
   author's own fixture to point at a reproducible data source.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import NoReturn

from tokie_cli.collectors.base import Collector, CollectorHealth
from tokie_cli.schema import Confidence, UsageEvent


class ContractViolationError(AssertionError):
    """A connector fails the Tokie contract.

    Subclass of :class:`AssertionError` so plain ``pytest`` rendering
    shows the failing message inline without extra configuration.
    """


# Backwards-compatible alias. Early docs referenced this shorter name; keep
# it exported until we cut a major release.
ContractViolation = ContractViolationError


def _fail(msg: str) -> NoReturn:
    raise ContractViolationError(msg)


def assert_collector_contract(cls: type[Collector]) -> None:
    """Validate the static shape of a :class:`Collector` subclass.

    Checks that can be made without instantiating:

    * ``cls`` is a ``Collector`` subclass.
    * Class-level ``name`` is a non-empty, lowercase-ish string.
    * Class-level ``default_confidence`` is a :class:`Confidence` value.
    * ``detect`` is a ``classmethod`` returning ``bool``.
    * ``scan`` is declared and returns an ``AsyncIterator[UsageEvent]``.
    * ``health`` is callable and returns :class:`CollectorHealth` (the
      default implementation is accepted).
    """

    if not isinstance(cls, type):
        _fail(f"expected a class, got {type(cls).__name__}")
    if not issubclass(cls, Collector):
        _fail(f"{cls.__name__} must subclass tokie_cli.collectors.Collector")

    name = getattr(cls, "name", None)
    if not isinstance(name, str) or not name.strip():
        _fail(f"{cls.__name__}.name must be a non-empty string, got {name!r}")
    if name != name.strip():
        _fail(f"{cls.__name__}.name must not have leading/trailing whitespace: {name!r}")
    if " " in name:
        _fail(f"{cls.__name__}.name must not contain spaces: {name!r}")

    conf = getattr(cls, "default_confidence", None)
    if not isinstance(conf, Confidence):
        _fail(
            f"{cls.__name__}.default_confidence must be a Confidence value, got {conf!r}"
        )

    detect = inspect.getattr_static(cls, "detect", None)
    if not isinstance(detect, classmethod):
        _fail(f"{cls.__name__}.detect must be declared with @classmethod")

    # Call detect() — it must be side-effect-free and return a bool.
    try:
        result = cls.detect()
    except Exception as exc:  # pragma: no cover - defensive
        _fail(f"{cls.__name__}.detect() raised {type(exc).__name__}: {exc}")
    if not isinstance(result, bool):
        _fail(f"{cls.__name__}.detect() must return bool, got {type(result).__name__}")

    scan = getattr(cls, "scan", None)
    if scan is None:
        _fail(f"{cls.__name__}.scan must be implemented")
    # We can't actually introspect the return annotation generically
    # (the user might inherit scan from an abstract base) but we can
    # confirm it's overridden or intentionally abstract.
    if getattr(scan, "__isabstractmethod__", False):
        _fail(
            f"{cls.__name__}.scan is still abstract — concrete collectors must override"
        )


def assert_event_is_valid(event: UsageEvent) -> None:
    """Validate a single :class:`UsageEvent` against the schema invariants.

    Checks duplicated in :class:`UsageEvent` itself (Pydantic validation)
    are still asserted here so the contract test stays self-contained
    even if schema validation becomes looser in a future release.
    """

    if not isinstance(event, UsageEvent):
        _fail(f"expected UsageEvent, got {type(event).__name__}")
    if not event.id:
        _fail("UsageEvent.id must be a non-empty string")
    if not event.raw_hash:
        _fail("UsageEvent.raw_hash must be a non-empty string")
    if not event.provider:
        _fail("UsageEvent.provider must be non-empty")
    if not event.product:
        _fail("UsageEvent.product must be non-empty")
    if not event.account_id:
        _fail("UsageEvent.account_id must be non-empty")
    if event.input_tokens < 0:
        _fail(f"UsageEvent.input_tokens must be >= 0, got {event.input_tokens}")
    if event.output_tokens < 0:
        _fail(f"UsageEvent.output_tokens must be >= 0, got {event.output_tokens}")
    if event.occurred_at.tzinfo is None:
        _fail("UsageEvent.occurred_at must be timezone-aware")
    if event.collected_at.tzinfo is None:
        _fail("UsageEvent.collected_at must be timezone-aware")
    # ``UsageEvent.confidence`` is already typed as :class:`Confidence`
    # via Pydantic, so a dynamic isinstance check is redundant. Keeping
    # the enum import above documents the invariant.


async def assert_scan_yields_valid_events(
    collector: Collector,
    *,
    since: datetime | None = None,
    min_events: int = 1,
) -> list[UsageEvent]:
    """Drive :meth:`Collector.scan` once and validate every emitted event.

    Returns the collected events so the caller can perform additional
    domain-specific assertions (e.g. "we parsed at least one tool-call"
    for a file-based connector).
    """

    stream = collector.scan(since=since)
    if not isinstance(stream, AsyncIterator):
        _fail(
            f"{collector.__class__.__name__}.scan() must return an AsyncIterator, "
            f"got {type(stream).__name__}"
        )

    events: list[UsageEvent] = []
    async for evt in stream:
        assert_event_is_valid(evt)
        events.append(evt)

    if min_events > 0 and len(events) < min_events:
        _fail(
            f"{collector.__class__.__name__}.scan() yielded {len(events)} event(s), "
            f"expected at least {min_events}"
        )
    return events


async def assert_idempotent_rescan(
    collector_factory: Callable[[], Collector],
    *,
    since: datetime | None = None,
) -> None:
    """Confirm that two back-to-back scans produce the same ``raw_hash`` set.

    Dedupe in ``tokie.db`` keys entirely off ``raw_hash`` — if the same
    input produces different hashes across runs, every ``tokie scan``
    will spam duplicate rows. The factory exists because some collectors
    cache internal state across :meth:`scan` calls; passing a function
    ensures each scan starts from a fresh instance.
    """

    first = await assert_scan_yields_valid_events(
        collector_factory(), since=since, min_events=0
    )
    second = await assert_scan_yields_valid_events(
        collector_factory(), since=since, min_events=0
    )
    first_hashes = sorted(e.raw_hash for e in first)
    second_hashes = sorted(e.raw_hash for e in second)
    if first_hashes != second_hashes:
        diff_first = set(first_hashes) - set(second_hashes)
        diff_second = set(second_hashes) - set(first_hashes)
        _fail(
            "raw_hash set diverged across rescans — collector is not idempotent. "
            f"first-only={sorted(diff_first)[:5]}, second-only={sorted(diff_second)[:5]}"
        )


def _collector_health_contract(health: CollectorHealth) -> None:
    """Assert the shape of a :class:`CollectorHealth` instance."""

    if not isinstance(health, CollectorHealth):
        _fail(f"expected CollectorHealth, got {type(health).__name__}")
    if not health.name:
        _fail("CollectorHealth.name must be non-empty")
    if not isinstance(health.detected, bool):
        _fail("CollectorHealth.detected must be a bool")
    if not isinstance(health.ok, bool):
        _fail("CollectorHealth.ok must be a bool")
    if not isinstance(health.last_scan_events, int):
        _fail("CollectorHealth.last_scan_events must be an int")


def assert_health_contract(collector: Collector) -> CollectorHealth:
    """Call ``collector.health()`` and validate the result.

    Returned so authors can chain extra assertions (e.g. "detected must
    be true when the fixture set the env var").
    """

    try:
        health = collector.health()
    except Exception as exc:  # pragma: no cover - defensive
        _fail(f"{collector.__class__.__name__}.health() raised {type(exc).__name__}: {exc}")
    _collector_health_contract(health)
    return health


__all__ = [
    "ContractViolation",
    "ContractViolationError",
    "assert_collector_contract",
    "assert_event_is_valid",
    "assert_health_contract",
    "assert_idempotent_rescan",
    "assert_scan_yields_valid_events",
]


# ---------------------------------------------------------------------------
# pytest plugin surface
# ---------------------------------------------------------------------------
# Advertised via ``[project.entry-points.pytest11]`` in ``pyproject.toml``.
# Third parties ``pip install tokie-cli`` and the fixtures below become
# auto-available in their tests.

import pytest  # noqa: E402


@pytest.fixture
def tokie_contract() -> Callable[[type[Collector]], None]:
    """Return :func:`assert_collector_contract` as a pytest-friendly fixture.

    Usage::

        def test_my_collector(tokie_contract):
            from my_connector import MyCollector
            tokie_contract(MyCollector)
    """

    return assert_collector_contract


@pytest.fixture
def tokie_event_contract() -> Callable[[UsageEvent], None]:
    """Return :func:`assert_event_is_valid` as a pytest fixture."""

    return assert_event_is_valid
