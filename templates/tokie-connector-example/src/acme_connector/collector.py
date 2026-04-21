"""Minimal but complete :class:`Collector` implementation.

What to change when forking this file:

1. ``name`` — the CLI slug. Use kebab-case so it matches the built-in
   collectors (``claude-code``, ``openai-api``, etc.).
2. ``default_confidence`` — pick based on your data source:
   * :attr:`Confidence.EXACT` for provider APIs with full metrics.
   * :attr:`Confidence.ESTIMATED` when you parse near-exact numbers
     (e.g. cost fields) that still need small unit conversions.
   * :attr:`Confidence.INFERRED` when you estimate from prompt/output
     text using a tokenizer, or from coarse signals like message
     counts.
3. ``detect()`` — must be fast and side-effect-free. Return ``True``
   when the source exists on this machine.
4. ``scan(since=...)`` — yield every :class:`UsageEvent` created since
   ``since``. MUST be idempotent: two calls with the same ``since``
   yield events with the same ``raw_hash`` values.

The example below emits a single synthetic event so ``tokie scan`` does
something visible before you wire up the real source.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from tokie_cli.collectors.base import Collector
from tokie_cli.schema import Confidence, UsageEvent, compute_raw_hash


class AcmeCollector(Collector):
    """Demo collector — replace with real ingestion logic."""

    name = "acme"
    default_confidence = Confidence.INFERRED

    @classmethod
    def detect(cls) -> bool:
        """Report whether the Acme data source is available locally.

        Keep this cheap: no network, no credential probes, no file
        writes. Stick to ``Path.exists()`` and environment variable
        checks.
        """

        # Replace with e.g. ``(Path.home() / ".acme" / "usage.log").exists()``.
        return True

    def scan(self, since: datetime | None = None) -> AsyncIterator[UsageEvent]:
        """Yield every event created after ``since``.

        Real implementations read a log file, call an API, or tail a
        session directory. Whatever you do, ``raw_hash`` MUST be a
        stable function of the source record so Tokie can dedupe
        across runs.
        """

        occurred_at = datetime.now(UTC)
        hash_input = f"acme|{occurred_at.isoformat()}"
        raw_hash = compute_raw_hash(hash_input)
        event = self.make_event(
            occurred_at=occurred_at,
            provider="acme",
            product="acme-cli",
            account_id="default",
            model="acme-v1",
            input_tokens=42,
            output_tokens=17,
            raw_hash=raw_hash,
            source="synthetic",
        )

        async def _stream() -> AsyncIterator[UsageEvent]:
            yield event

        return _stream()


def _stable_hash(*parts: str) -> str:
    """Convenience helper for building ``raw_hash`` from source tokens.

    Not used above (we call :func:`compute_raw_hash` directly) but
    exposed so your real ``scan()`` can produce a deterministic hash
    across platforms — the built-in helper already handles that, so
    prefer :func:`compute_raw_hash` unless you have a good reason.
    """

    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
