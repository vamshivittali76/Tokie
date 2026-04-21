"""Tests for the generic OpenAI-compatible NDJSON collector.

Every test writes its fixture under ``tmp_path`` — never touches a real log
— so these are safe to run in CI and on developer machines that happen to
have ``TOKIE_OPENAI_COMPAT_LOG`` pointed at something real.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tokie_cli.collectors.api_openai_compatible import (
    OpenAICompatibleCollector,
)
from tokie_cli.schema import Confidence, UsageEvent


def _write_lines(path: Path, lines: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        for obj in lines:
            fp.write(json.dumps(obj) + "\n")


async def _collect(it: AsyncIterator[UsageEvent]) -> list[UsageEvent]:
    return [evt async for evt in it]


def _groq_line(**overrides: object) -> dict[str, object]:
    line: dict[str, object] = {
        "timestamp": "2026-04-20T12:34:56Z",
        "provider": "groq",
        "model": "llama-3.1-70b-versatile",
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        },
    }
    line.update(overrides)
    return line


async def test_single_file_groq_line(tmp_path: Path) -> None:
    log = tmp_path / "groq.jsonl"
    _write_lines(log, [_groq_line(session_id="sess-1")])

    collector = OpenAICompatibleCollector(log_path=log)
    events = await _collect(collector.scan())

    assert len(events) == 1
    evt = events[0]
    assert evt.provider == "groq"
    assert evt.product == "groq-api"
    assert evt.model == "llama-3.1-70b-versatile"
    assert evt.input_tokens == 100
    assert evt.output_tokens == 50
    assert evt.cache_read_tokens == 0
    assert evt.reasoning_tokens == 0
    assert evt.session_id == "sess-1"
    assert evt.account_id == "default"
    assert evt.confidence is Confidence.EXACT
    assert evt.source.startswith("openai_compat:groq:")
    assert evt.source.endswith(":1")
    assert evt.occurred_at == datetime(2026, 4, 20, 12, 34, 56, tzinfo=UTC)


async def test_deepseek_line_with_nested_cached_tokens(tmp_path: Path) -> None:
    log = tmp_path / "deepseek.jsonl"
    line: dict[str, object] = {
        "timestamp": "2026-04-20T08:00:00Z",
        "provider": "deepseek",
        "model": "deepseek-chat",
        "usage": {
            "prompt_tokens": 200,
            "completion_tokens": 75,
            "total_tokens": 275,
            "prompt_tokens_details": {"cached_tokens": 128},
        },
    }
    _write_lines(log, [line])

    events = await _collect(OpenAICompatibleCollector(log_path=log).scan())

    assert len(events) == 1
    assert events[0].cache_read_tokens == 128
    assert events[0].provider == "deepseek"


async def test_openrouter_reasoning_via_completion_details(tmp_path: Path) -> None:
    log = tmp_path / "openrouter.ndjson"
    line: dict[str, object] = {
        "timestamp": "2026-04-20T09:00:00Z",
        "provider": "openrouter",
        "model": "deepseek/deepseek-r1",
        "usage": {
            "prompt_tokens": 50,
            "completion_tokens": 600,
            "completion_tokens_details": {"reasoning_tokens": 512},
        },
    }
    _write_lines(log, [line])

    events = await _collect(OpenAICompatibleCollector(log_path=log).scan())

    assert len(events) == 1
    assert events[0].reasoning_tokens == 512
    assert events[0].output_tokens == 600


async def test_directory_with_two_files(tmp_path: Path) -> None:
    _write_lines(tmp_path / "groq.jsonl", [_groq_line()])
    _write_lines(
        tmp_path / "nested" / "together.ndjson",
        [
            {
                "timestamp": "2026-04-20T10:00:00Z",
                "provider": "together",
                "model": "meta-llama/Llama-3-70b-chat-hf",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        ],
    )

    events = await _collect(OpenAICompatibleCollector(log_path=tmp_path).scan())

    providers = {e.provider for e in events}
    assert providers == {"groq", "together"}
    assert len(events) == 2


async def test_malformed_json_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    log = tmp_path / "mix.jsonl"
    with log.open("w", encoding="utf-8") as fp:
        fp.write("{not json at all\n")
        fp.write(json.dumps(_groq_line()) + "\n")

    with caplog.at_level("WARNING"):
        events = await _collect(OpenAICompatibleCollector(log_path=log).scan())

    assert len(events) == 1
    assert any("malformed json" in rec.message for rec in caplog.records)
    # Line content must never appear in logs.
    assert all("not json at all" not in rec.message for rec in caplog.records)


async def test_missing_usage_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    log = tmp_path / "noUsage.jsonl"
    _write_lines(
        log,
        [
            {
                "timestamp": "2026-04-20T12:00:00Z",
                "provider": "groq",
                "model": "llama-3.1-70b-versatile",
            },
            _groq_line(),
        ],
    )

    with caplog.at_level("WARNING"):
        events = await _collect(OpenAICompatibleCollector(log_path=log).scan())

    assert len(events) == 1
    assert any("missing usage" in rec.message for rec in caplog.records)


async def test_missing_model_rejected(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    log = tmp_path / "noModel.jsonl"
    _write_lines(
        log,
        [
            {
                "timestamp": "2026-04-20T12:00:00Z",
                "provider": "groq",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
            _groq_line(),
        ],
    )

    with caplog.at_level("WARNING"):
        events = await _collect(OpenAICompatibleCollector(log_path=log).scan())

    assert len(events) == 1
    assert any("missing model" in rec.message for rec in caplog.records)


async def test_provider_falls_back_to_default_provider(tmp_path: Path) -> None:
    log = tmp_path / "nolabel.jsonl"
    line = _groq_line()
    line.pop("provider")
    _write_lines(log, [line])

    collector = OpenAICompatibleCollector(log_path=log, default_provider="fireworks")
    events = await _collect(collector.scan())

    assert len(events) == 1
    assert events[0].provider == "fireworks"
    assert events[0].product == "fireworks-api"


async def test_provider_falls_back_to_openai_compat_sentinel(tmp_path: Path) -> None:
    log = tmp_path / "anon.jsonl"
    line = _groq_line()
    line.pop("provider")
    _write_lines(log, [line])

    events = await _collect(OpenAICompatibleCollector(log_path=log).scan())

    assert len(events) == 1
    assert events[0].provider == "openai-compat"
    assert events[0].product == "openai-compat-api"


async def test_per_line_account_and_product_override(tmp_path: Path) -> None:
    log = tmp_path / "override.jsonl"
    _write_lines(
        log,
        [
            _groq_line(account_id="work-sso", product="groq-batch"),
            _groq_line(),
        ],
    )

    collector = OpenAICompatibleCollector(log_path=log, default_account_id="personal")
    events = await _collect(collector.scan())

    assert events[0].account_id == "work-sso"
    assert events[0].product == "groq-batch"
    assert events[1].account_id == "personal"
    assert events[1].product == "groq-api"


async def test_since_filter_drops_older_events(tmp_path: Path) -> None:
    log = tmp_path / "since.jsonl"
    _write_lines(
        log,
        [
            _groq_line(timestamp="2026-04-19T00:00:00Z"),
            _groq_line(timestamp="2026-04-20T12:00:00Z"),
            _groq_line(timestamp="2026-04-21T00:00:00Z"),
        ],
    )

    cutoff = datetime(2026, 4, 20, tzinfo=UTC)
    events = await _collect(OpenAICompatibleCollector(log_path=log).scan(since=cutoff))

    assert [e.occurred_at for e in events] == [
        datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
        datetime(2026, 4, 21, 0, 0, tzinfo=UTC),
    ]


def test_detect_reflects_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TOKIE_OPENAI_COMPAT_LOG", raising=False)
    assert OpenAICompatibleCollector.detect() is False

    log = tmp_path / "present.jsonl"
    log.write_text("", encoding="utf-8")
    monkeypatch.setenv("TOKIE_OPENAI_COMPAT_LOG", str(log))
    assert OpenAICompatibleCollector.detect() is True

    monkeypatch.setenv("TOKIE_OPENAI_COMPAT_LOG", str(tmp_path / "does-not-exist.jsonl"))
    assert OpenAICompatibleCollector.detect() is False


async def test_raw_hash_stable_across_two_scans(tmp_path: Path) -> None:
    log = tmp_path / "stable.jsonl"
    _write_lines(log, [_groq_line(), _groq_line(timestamp="2026-04-20T13:00:00Z")])

    collector = OpenAICompatibleCollector(log_path=log)
    first = await _collect(collector.scan())
    second = await _collect(collector.scan())

    assert [e.raw_hash for e in first] == [e.raw_hash for e in second]
    assert [e.source for e in first] == [e.source for e in second]
    assert len({e.raw_hash for e in first}) == 2


async def test_large_file_streaming(tmp_path: Path) -> None:
    log = tmp_path / "big.jsonl"
    lines = [
        _groq_line(timestamp=f"2026-04-20T{hour:02d}:00:00Z", session_id=f"s-{hour}")
        for hour in range(24)
    ]
    lines.extend(
        _groq_line(
            timestamp=f"2026-04-19T{hour % 24:02d}:{(hour * 3) % 60:02d}:00Z",
            session_id=f"s2-{hour}",
        )
        for hour in range(100)
    )
    _write_lines(log, lines)

    events = await _collect(OpenAICompatibleCollector(log_path=log).scan())

    assert len(events) == 124
    assert all(e.provider == "groq" for e in events)
    assert len({e.source for e in events}) == 124


def test_health_reports_missing_path(tmp_path: Path) -> None:
    collector = OpenAICompatibleCollector(log_path=tmp_path / "nope.jsonl")
    health = collector.health()
    assert health.detected is False
    assert health.ok is False


def test_health_reports_present_file(tmp_path: Path) -> None:
    log = tmp_path / "present.jsonl"
    _write_lines(log, [_groq_line()])
    collector = OpenAICompatibleCollector(log_path=log)
    health = collector.health()
    assert health.detected is True
    assert health.ok is True
