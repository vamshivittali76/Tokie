"""Canonical Tokie schema.

This module defines the *contract* that every collector, every storage row, and
every dashboard reader agrees on. Changes here are schema-breaking and require
a migration in ``tokie_cli.db`` plus regeneration of golden-file test fixtures.

Source: section 6 of TOKIE_DEVELOPMENT_PLAN_FINAL.md.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, NonNegativeFloat, NonNegativeInt


class Confidence(StrEnum):
    """How trustworthy a single usage number is.

    Drives dashboard rendering: exact -> solid bar, estimated -> striped,
    inferred -> dashed outline. Never silently averaged across tiers.
    """

    EXACT = "exact"
    ESTIMATED = "estimated"
    INFERRED = "inferred"


class WindowType(StrEnum):
    """Kinds of quota windows a subscription can expose."""

    ROLLING_5H = "rolling_5h"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    NONE = "none"


class UsageEvent(BaseModel):
    """A single LLM call, normalized.

    Every collector produces rows in this exact shape. ``raw_hash`` is the
    idempotency key: re-scanning the same source twice must never create
    duplicate rows.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    collected_at: datetime
    occurred_at: datetime

    provider: str
    product: str
    account_id: str
    session_id: str | None = None
    project: str | None = None

    model: str
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    cache_read_tokens: NonNegativeInt = 0
    cache_write_tokens: NonNegativeInt = 0
    reasoning_tokens: NonNegativeInt = 0
    cost_usd: NonNegativeFloat | None = None

    confidence: Confidence
    source: str
    raw_hash: str

    @property
    def total_tokens(self) -> int:
        """Total tokens across every sub-counter.

        Cache-read tokens are included because they still count against most
        vendor quotas. If a vendor treats them differently, the collector is
        responsible for zeroing the relevant field before emit.
        """

        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
            + self.reasoning_tokens
        )


class LimitWindow(BaseModel):
    """One quota window on a subscription.

    A subscription can declare many of these. Claude Pro declares two
    (rolling-5h and weekly) that share the same underlying bucket across
    ``claude-web`` and ``claude-code`` via ``shared_with``.
    """

    model_config = ConfigDict(extra="forbid")

    window_type: WindowType
    limit_tokens: NonNegativeInt | None = None
    limit_messages: NonNegativeInt | None = None
    limit_usd: NonNegativeFloat | None = None
    resets_at: datetime | None = None
    shared_with: list[str] = Field(default_factory=list)


class Subscription(BaseModel):
    """A user's paid relationship with one AI product.

    Identified by ``id`` (e.g. ``claude_pro_personal``). Multiple accounts
    against the same product get distinct ``account_id`` values.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    provider: str
    product: str
    plan: str
    account_id: str
    windows: list[LimitWindow] = Field(default_factory=list)


def compute_raw_hash(payload: str | bytes | dict[str, Any]) -> str:
    """Produce the dedup key for a raw source record.

    Accepts the original JSONL line, a bytes blob, or a dict. Dicts are
    serialized with sorted keys so two equivalent records always hash the same.
    """

    if isinstance(payload, dict):
        import json

        payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "Confidence",
    "LimitWindow",
    "Subscription",
    "UsageEvent",
    "WindowType",
    "compute_raw_hash",
]
