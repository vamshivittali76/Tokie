"""Collector contract and shared primitives.

Every connector — built-in and third-party — implements :class:`Collector`.
Third-party packages register under the ``tokie.collectors`` entry point so
``tokie doctor`` can discover them automatically.

See section 8 of ``TOKIE_DEVELOPMENT_PLAN_FINAL.md`` for the design motivation.
"""

from __future__ import annotations

import asyncio
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from tokie_cli.schema import Confidence, UsageEvent


@dataclass(frozen=True)
class CollectorHealth:
    """Result of a readiness probe surfaced by ``tokie doctor``."""

    name: str
    detected: bool
    ok: bool
    last_scan_at: datetime | None
    last_scan_events: int
    message: str
    warnings: tuple[str, ...] = ()


class CollectorError(Exception):
    """A collector failed in a way the user should see.

    Collectors MUST NOT include secrets or raw prompt content in the message.
    ``tokie doctor`` and the alert pipeline both render this string directly.
    """


class Collector(ABC):
    """Contract every concrete collector implements.

    Subclasses MUST set :attr:`name` and :attr:`default_confidence` as class
    attributes. They MAY override :meth:`watch` and :meth:`health` but get a
    reasonable default for both out of the box.
    """

    name: str
    default_confidence: Confidence

    @classmethod
    @abstractmethod
    def detect(cls) -> bool:
        """Return True if this collector's data source exists on this machine.

        Implementations MUST be fast and side-effect-free. No network requests,
        no authentication probes, no file writes. Detection is called often
        (once per ``tokie doctor`` run and once per ``tokie watch`` restart).
        """

    @abstractmethod
    def scan(self, since: datetime | None = None) -> AsyncIterator[UsageEvent]:
        """Yield every :class:`UsageEvent` produced since ``since``.

        Must be idempotent across runs. Re-scanning the same source with the
        same ``since`` value MUST yield events with the same ``raw_hash``
        values so the database can dedupe them.

        ``since`` is tz-aware. Passing ``None`` means "from the beginning of
        the retained history on the source".
        """

    async def watch(self, *, poll_interval_sec: float = 5.0) -> AsyncIterator[UsageEvent]:
        """Yield new :class:`UsageEvent`s as they appear.

        Default implementation polls :meth:`scan` on a monotonic cursor with a
        configurable interval. File-backed collectors can override this with
        a watchdog-style watcher; API collectors typically stick with polling
        because vendors don't expose push notifications for usage data.
        """

        cursor: datetime | None = None
        while True:
            latest = cursor
            async for evt in self.scan(since=cursor):
                yield evt
                if latest is None or evt.occurred_at > latest:
                    latest = evt.occurred_at
            cursor = latest
            await asyncio.sleep(poll_interval_sec)

    def health(self) -> CollectorHealth:
        """Fast readiness probe used by ``tokie doctor``.

        Default implementation reports detection state only. Subclasses
        override to report credential presence, last scan success, and any
        source-specific warnings the user should know about.
        """

        detected = self.detect()
        return CollectorHealth(
            name=self.name,
            detected=detected,
            ok=detected,
            last_scan_at=None,
            last_scan_events=0,
            message="source detected" if detected else "source not detected",
        )

    def make_event(
        self,
        *,
        occurred_at: datetime,
        provider: str,
        product: str,
        account_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        raw_hash: str,
        source: str,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        cost_usd: float | None = None,
        session_id: str | None = None,
        project: str | None = None,
        confidence: Confidence | None = None,
    ) -> UsageEvent:
        """Helper that fills ``id`` and ``collected_at`` with sensible defaults.

        Every collector uses this rather than constructing :class:`UsageEvent`
        by hand so provenance fields stay consistent across the codebase.
        """

        return UsageEvent(
            id=str(uuid.uuid4()),
            collected_at=datetime.now(UTC),
            occurred_at=occurred_at,
            provider=provider,
            product=product,
            account_id=account_id,
            session_id=session_id,
            project=project,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            reasoning_tokens=reasoning_tokens,
            cost_usd=cost_usd,
            confidence=confidence or self.default_confidence,
            source=source,
            raw_hash=raw_hash,
        )


def aiterate(items: Any) -> AsyncIterator[UsageEvent]:
    """Wrap a sync iterable of events as an ``AsyncIterator``.

    Handy for file-based collectors that do synchronous I/O but need to
    satisfy the async ``scan()`` return type.
    """

    async def _gen() -> AsyncIterator[UsageEvent]:
        for item in items:
            yield item

    return _gen()


__all__ = [
    "Collector",
    "CollectorError",
    "CollectorHealth",
    "aiterate",
]
