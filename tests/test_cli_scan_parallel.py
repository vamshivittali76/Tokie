"""Regression tests for the parallel scan path in ``tokie_cli.cli``.

``_run_scan`` was rewritten to drain every collector's ``scan()`` in
parallel via ``asyncio.gather``. The upsides (lower wall-clock time,
per-collector duration reporting) only matter if two properties hold:

1. Collectors running concurrently are actually overlapped on the
   event loop, not awaited one after the other.
2. A failure in one collector does not crash the run or prevent
   others from committing their events.

Both are exercised here with a pair of fake collectors whose ``scan``
methods sleep deterministically.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tokie_cli.cli import _run_scan
from tokie_cli.collectors.base import Collector
from tokie_cli.config import TokieConfig, save_config
from tokie_cli.schema import Confidence, UsageEvent


class _FakeCollector(Collector):
    """Minimal collector that yields a fixed event after a delay."""

    default_confidence = Confidence.EXACT

    def __init__(
        self,
        name: str,
        *,
        delay: float = 0.0,
        raise_exc: Exception | None = None,
    ) -> None:
        self.name = name
        self._delay = delay
        self._raise = raise_exc

    @classmethod
    def detect(cls) -> bool:  # pragma: no cover - never called in tests
        return True

    async def scan(
        self, since: datetime | None = None
    ) -> AsyncIterator[UsageEvent]:
        await asyncio.sleep(self._delay)
        if self._raise is not None:
            raise self._raise
        yield self.make_event(
            occurred_at=datetime.now(UTC),
            provider=self.name,
            product=self.name,
            account_id="default",
            model="fake",
            input_tokens=1,
            output_tokens=1,
            raw_hash=f"{self.name}-{time.monotonic_ns()}",
            source=self.name,
        )


@pytest.fixture
def tokie_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg_dir = tmp_path / "tokie"
    cfg_dir.mkdir()
    monkeypatch.setenv("TOKIE_HOME", str(cfg_dir))
    config = TokieConfig(
        db_path=cfg_dir / "tokie.db",
        audit_log_path=cfg_dir / "audit.log",
    )
    save_config(config, cfg_dir / "tokie.toml")
    return cfg_dir


def test_run_scan_executes_collectors_concurrently(tokie_env: Path) -> None:
    # Two collectors each sleep 150ms. If the runner serialises them,
    # total wall time >= 300ms. With gather, it should be ~150ms.
    delay = 0.15
    collectors: list[Collector] = [
        _FakeCollector("alpha", delay=delay),
        _FakeCollector("bravo", delay=delay),
    ]

    start = time.monotonic()
    total_new = asyncio.run(_run_scan(collectors, since=None))
    elapsed = time.monotonic() - start

    assert total_new == 2
    # Generous headroom for CI jitter. The serial path would be >=
    # 2*delay (300ms); anything below 1.6x a single delay proves
    # overlap is happening.
    assert elapsed < delay * 1.6, (
        f"collectors did not run concurrently (elapsed={elapsed:.3f}s)"
    )


def test_run_scan_survives_broken_collector(tokie_env: Path) -> None:
    collectors: list[Collector] = [
        _FakeCollector("good", delay=0.01),
        _FakeCollector(
            "broken", delay=0.01, raise_exc=RuntimeError("kaboom")
        ),
    ]

    total_new = asyncio.run(_run_scan(collectors, since=None))

    # Broken collector yielded nothing; good one committed one event.
    assert total_new == 1
