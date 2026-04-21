"""Tests for :mod:`tokie_cli.collectors.api_gemini`.

All tests use ``tmp_path`` so we never read from the user's real
``~/.gemini`` directory.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tokie_cli.collectors.api_gemini import GeminiAPICollector
from tokie_cli.schema import Confidence, UsageEvent


async def _collect(it: AsyncIterator[UsageEvent]) -> list[UsageEvent]:
    out: list[UsageEvent] = []
    async for evt in it:
        out.append(evt)
    return out


def _format_a_line(
    *,
    ts: str = "2026-04-20T10:00:00Z",
    model: str = "gemini-2.5-pro",
    session: str = "sess-a",
    prompt: int = 100,
    candidates: int = 50,
    cached: int = 0,
    thoughts: int = 0,
) -> str:
    return json.dumps(
        {
            "timestamp": ts,
            "model": model,
            "sessionId": session,
            "usageMetadata": {
                "promptTokenCount": prompt,
                "candidatesTokenCount": candidates,
                "totalTokenCount": prompt + candidates,
                "cachedContentTokenCount": cached,
                "thoughtsTokenCount": thoughts,
            },
        }
    )


def _format_b_line(
    *,
    ts: str = "2026-04-20T11:00:00Z",
    model: str = "gemini-2.5-flash",
    prompt: int = 7,
    candidates: int = 3,
) -> str:
    return json.dumps(
        {
            "timestamp": ts,
            "modelVersion": model,
            "usageMetadata": {
                "promptTokenCount": prompt,
                "candidatesTokenCount": candidates,
                "totalTokenCount": prompt + candidates,
            },
        }
    )


async def test_format_a_happy_path(tmp_path: Path) -> None:
    root = tmp_path / "history"
    root.mkdir()
    (root / "a.jsonl").write_text(
        _format_a_line(prompt=100, candidates=50, cached=20, thoughts=10) + "\n",
        encoding="utf-8",
    )
    collector = GeminiAPICollector(session_root=root)
    events = await _collect(collector.scan())
    assert len(events) == 1
    evt = events[0]
    assert evt.provider == "google"
    assert evt.product == "gemini-api"
    assert evt.model == "gemini-2.5-pro"
    assert evt.input_tokens == 100
    assert evt.output_tokens == 50
    assert evt.cache_read_tokens == 20
    assert evt.reasoning_tokens == 10
    assert evt.confidence is Confidence.EXACT


async def test_format_b_with_model_version(tmp_path: Path) -> None:
    root = tmp_path / "history"
    root.mkdir()
    (root / "b.ndjson").write_text(_format_b_line() + "\n", encoding="utf-8")
    collector = GeminiAPICollector(session_root=root)
    events = await _collect(collector.scan())
    assert len(events) == 1
    assert events[0].model == "gemini-2.5-flash"


async def test_mixed_extensions_and_formats(tmp_path: Path) -> None:
    root = tmp_path / "history"
    root.mkdir()
    (root / "a.jsonl").write_text(
        _format_a_line(ts="2026-04-20T10:00:00Z") + "\n", encoding="utf-8"
    )
    (root / "b.ndjson").write_text(
        _format_b_line(ts="2026-04-20T11:00:00Z") + "\n", encoding="utf-8"
    )
    collector = GeminiAPICollector(session_root=root)
    events = await _collect(collector.scan())
    assert {e.model for e in events} == {"gemini-2.5-pro", "gemini-2.5-flash"}


async def test_missing_usage_metadata_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "history"
    root.mkdir()
    bad = json.dumps({"timestamp": "2026-04-20T10:00:00Z", "model": "g"}) + "\n"
    (root / "a.jsonl").write_text(bad + _format_a_line() + "\n", encoding="utf-8")
    collector = GeminiAPICollector(session_root=root)
    events = await _collect(collector.scan())
    assert len(events) == 1


async def test_malformed_json_line_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "history"
    root.mkdir()
    (root / "a.jsonl").write_text("{not valid json\n" + _format_a_line() + "\n", encoding="utf-8")
    collector = GeminiAPICollector(session_root=root)
    events = await _collect(collector.scan())
    assert len(events) == 1


async def test_since_filter(tmp_path: Path) -> None:
    root = tmp_path / "history"
    root.mkdir()
    lines = [
        _format_a_line(ts="2026-04-20T09:00:00Z", session="s1"),
        _format_a_line(ts="2026-04-20T11:00:00Z", session="s2"),
    ]
    (root / "a.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    collector = GeminiAPICollector(session_root=root)
    cutoff = datetime(2026, 4, 20, 10, 0, tzinfo=UTC)
    events = await _collect(collector.scan(since=cutoff))
    assert [e.session_id for e in events] == ["s2"]


async def test_env_var_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "drop.ndjson"
    log.write_text(_format_a_line() + "\n", encoding="utf-8")
    monkeypatch.setenv("TOKIE_GEMINI_LOG", str(log))
    collector = GeminiAPICollector(session_root=None)
    events = await _collect(collector.scan())
    assert len(events) == 1


async def test_extra_paths_additional_directory(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    extra = tmp_path / "extra"
    primary.mkdir()
    extra.mkdir()
    (primary / "p.jsonl").write_text(_format_a_line(session="p1") + "\n", encoding="utf-8")
    (extra / "e.jsonl").write_text(_format_a_line(session="e1") + "\n", encoding="utf-8")
    collector = GeminiAPICollector(session_root=primary, extra_paths=(extra,))
    events = await _collect(collector.scan())
    assert {e.session_id for e in events} == {"p1", "e1"}


def test_detect_false_when_nothing_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TOKIE_GEMINI_LOG", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert GeminiAPICollector.detect() is False


def test_detect_true_with_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "log.ndjson"
    log.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("TOKIE_GEMINI_LOG", str(log))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert GeminiAPICollector.detect() is True


async def test_raw_hash_is_stable_across_runs(tmp_path: Path) -> None:
    root = tmp_path / "history"
    root.mkdir()
    (root / "a.jsonl").write_text(_format_a_line() + "\n", encoding="utf-8")
    collector = GeminiAPICollector(session_root=root)
    first = [e.raw_hash for e in await _collect(collector.scan())]
    second = [e.raw_hash for e in await _collect(collector.scan())]
    assert first == second


async def test_thoughts_token_count_populates_reasoning_tokens(tmp_path: Path) -> None:
    root = tmp_path / "history"
    root.mkdir()
    (root / "a.jsonl").write_text(_format_a_line(thoughts=777) + "\n", encoding="utf-8")
    collector = GeminiAPICollector(session_root=root)
    events = await _collect(collector.scan())
    assert events[0].reasoning_tokens == 777
