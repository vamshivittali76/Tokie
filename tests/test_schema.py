"""Schema contract tests.

These are the first tests that run on CI. They lock the Pydantic contract
defined in ``src/tokie/schema.py``. If any of these change, the commit also
needs a migration in ``tokie.db`` and a golden-file refresh.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from tokie_cli.schema import (
    Confidence,
    LimitWindow,
    Subscription,
    UsageEvent,
    WindowType,
    compute_raw_hash,
)


def make_event(**overrides: object) -> UsageEvent:
    defaults: dict[str, object] = {
        "id": "evt-1",
        "collected_at": datetime(2026, 4, 20, tzinfo=UTC),
        "occurred_at": datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
        "provider": "anthropic",
        "product": "claude-code",
        "account_id": "hash-me",
        "model": "claude-opus-4-7",
        "input_tokens": 100,
        "output_tokens": 50,
        "confidence": Confidence.EXACT,
        "source": "jsonl:~/.claude/projects/foo.jsonl",
        "raw_hash": "a" * 64,
    }
    defaults.update(overrides)
    return UsageEvent(**defaults)  # type: ignore[arg-type]


def test_usage_event_total_tokens_sums_every_counter() -> None:
    evt = make_event(
        input_tokens=10,
        output_tokens=20,
        cache_read_tokens=5,
        cache_write_tokens=3,
        reasoning_tokens=7,
    )
    assert evt.total_tokens == 45


def test_usage_event_is_frozen() -> None:
    evt = make_event()
    with pytest.raises(ValidationError):
        evt.input_tokens = 999


def test_usage_event_rejects_negative_tokens() -> None:
    with pytest.raises(ValidationError):
        make_event(input_tokens=-1)


def test_usage_event_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        make_event(surprise_field="nope")


def test_confidence_roundtrips_through_json() -> None:
    evt = make_event(confidence=Confidence.INFERRED)
    dumped = evt.model_dump_json()
    assert '"inferred"' in dumped
    restored = UsageEvent.model_validate_json(dumped)
    assert restored.confidence is Confidence.INFERRED


def test_subscription_with_shared_window() -> None:
    sub = Subscription(
        id="claude_pro_personal",
        provider="anthropic",
        product="claude-pro",
        plan="pro",
        account_id="acct-1",
        windows=[
            LimitWindow(
                window_type=WindowType.ROLLING_5H,
                limit_messages=45,
                shared_with=["claude-web", "claude-code"],
            ),
            LimitWindow(
                window_type=WindowType.WEEKLY,
                limit_messages=900,
                shared_with=["claude-web", "claude-code"],
            ),
        ],
    )
    assert {w.window_type for w in sub.windows} == {
        WindowType.ROLLING_5H,
        WindowType.WEEKLY,
    }
    assert all("claude-web" in w.shared_with for w in sub.windows)


def test_compute_raw_hash_is_stable_and_order_independent() -> None:
    a = compute_raw_hash({"b": 2, "a": 1})
    b = compute_raw_hash({"a": 1, "b": 2})
    assert a == b
    assert len(a) == 64


def test_compute_raw_hash_accepts_bytes_and_str() -> None:
    assert compute_raw_hash("hello") == compute_raw_hash(b"hello")
