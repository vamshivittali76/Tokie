"""Tests for :mod:`tokie_cli.collectors.claude_code`.

Every test uses an explicit ``session_root=tmp_path`` so we never read the
real ``~/.claude`` directory. Async tests are decorated explicitly for
clarity even though ``pytest-asyncio`` runs in auto mode.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tokie_cli.collectors.claude_code import ClaudeCodeCollector
from tokie_cli.schema import Confidence, UsageEvent


def _assistant_line(
    *,
    ts: str = "2026-04-20T12:00:00.000Z",
    session_id: str = "session-abc",
    cwd: str = "/home/user/projects/tokie",
    model: str = "claude-3-5-sonnet-20241022",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_creation: int = 7,
    cache_read: int = 11,
    **extra: Any,
) -> str:
    payload: dict[str, Any] = {
        "type": "assistant",
        "timestamp": ts,
        "sessionId": session_id,
        "cwd": cwd,
        "message": {
            "id": "msg_1",
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
            },
        },
    }
    payload.update(extra)
    return json.dumps(payload)


def _user_line() -> str:
    return json.dumps(
        {
            "type": "user",
            "timestamp": "2026-04-20T11:59:00.000Z",
            "sessionId": "session-abc",
            "message": {"role": "user", "content": "SECRET PROMPT DO NOT LOG"},
        }
    )


def _write_session(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _collect(
    collector: ClaudeCodeCollector, since: datetime | None = None
) -> list[UsageEvent]:
    events: list[UsageEvent] = []
    iterator: AsyncIterator[UsageEvent] = collector.scan(since=since)
    async for evt in iterator:
        events.append(evt)
    return events


@pytest.mark.asyncio
async def test_scan_happy_path_across_multiple_files(tmp_path: Path) -> None:
    _write_session(
        tmp_path / "proj-a" / "s1.jsonl",
        [
            _assistant_line(ts="2026-04-20T10:00:00Z", session_id="s1"),
            _user_line(),
            _assistant_line(ts="2026-04-20T10:05:00Z", session_id="s1"),
        ],
    )
    _write_session(
        tmp_path / "proj-b" / "s2.jsonl",
        [_assistant_line(ts="2026-04-20T11:00:00Z", session_id="s2")],
    )

    events = await _collect(ClaudeCodeCollector(session_root=tmp_path))

    assert len(events) == 3
    assert {e.session_id for e in events} == {"s1", "s2"}
    assert all(e.provider == "anthropic" for e in events)
    assert all(e.product == "claude-code" for e in events)
    assert all(e.account_id == "default" for e in events)
    assert all(e.confidence is Confidence.EXACT for e in events)
    assert all(e.occurred_at.tzinfo is not None for e in events)
    assert all(e.cost_usd is None for e in events)


@pytest.mark.asyncio
async def test_scan_skips_malformed_json_and_missing_usage(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write_session(
        tmp_path / "s.jsonl",
        [
            "not json at all",
            _user_line(),
            json.dumps({"type": "system", "timestamp": "2026-04-20T10:00:00Z"}),
            _assistant_line(ts="2026-04-20T10:00:00Z"),
            "",
        ],
    )

    with caplog.at_level("WARNING", logger="tokie_cli.collectors.claude_code"):
        events = await _collect(ClaudeCodeCollector(session_root=tmp_path))

    assert len(events) == 1
    # Malformed line was logged but SECRET PROMPT was NOT.
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "malformed json" in joined
    assert "SECRET PROMPT" not in joined


@pytest.mark.asyncio
async def test_scan_since_filter_drops_older_events(tmp_path: Path) -> None:
    _write_session(
        tmp_path / "s.jsonl",
        [
            _assistant_line(ts="2026-04-19T23:59:00Z", session_id="old"),
            _assistant_line(ts="2026-04-20T00:00:00Z", session_id="boundary"),
            _assistant_line(ts="2026-04-20T12:00:00Z", session_id="new"),
        ],
    )

    since = datetime(2026, 4, 20, 0, 0, tzinfo=UTC)
    events = await _collect(ClaudeCodeCollector(session_root=tmp_path), since=since)

    session_ids = [e.session_id for e in events]
    assert "old" not in session_ids
    assert set(session_ids) == {"boundary", "new"}


def test_detect_false_when_default_root_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOKIE_CLAUDE_SESSION_ROOT", str(tmp_path / "does-not-exist"))
    assert ClaudeCodeCollector.detect() is False


def test_detect_true_when_env_override_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "claude-projects"
    root.mkdir()
    monkeypatch.setenv("TOKIE_CLAUDE_SESSION_ROOT", str(root))
    assert ClaudeCodeCollector.detect() is True


def test_env_var_override_drives_default_session_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "custom-root"
    root.mkdir()
    monkeypatch.setenv("TOKIE_CLAUDE_SESSION_ROOT", str(root))
    collector = ClaudeCodeCollector()
    assert collector.session_root == root


def test_health_reports_counts_and_latest_mtime(tmp_path: Path) -> None:
    _write_session(tmp_path / "a.jsonl", [_assistant_line()])
    _write_session(tmp_path / "nested" / "b.jsonl", [_assistant_line()])

    health = ClaudeCodeCollector(session_root=tmp_path).health()

    assert health.detected is True
    assert health.ok is True
    assert health.last_scan_events == 0
    assert health.last_scan_at is not None
    assert health.last_scan_at.tzinfo is not None
    assert "2 jsonl file(s)" in health.message


def test_health_detects_missing_root(tmp_path: Path) -> None:
    missing = tmp_path / "not-there"
    health = ClaudeCodeCollector(session_root=missing).health()
    assert health.detected is False
    assert health.ok is False
    assert health.last_scan_at is None


@pytest.mark.asyncio
async def test_idempotent_raw_hashes_across_runs(tmp_path: Path) -> None:
    _write_session(
        tmp_path / "s.jsonl",
        [
            _assistant_line(ts="2026-04-20T10:00:00Z"),
            _assistant_line(ts="2026-04-20T11:00:00Z"),
        ],
    )
    collector = ClaudeCodeCollector(session_root=tmp_path)

    first = await _collect(collector)
    second = await _collect(collector)

    assert [e.raw_hash for e in first] == [e.raw_hash for e in second]
    assert len({e.raw_hash for e in first}) == 2  # lines differ -> hashes differ


@pytest.mark.asyncio
async def test_cache_token_fields_flow_through(tmp_path: Path) -> None:
    _write_session(
        tmp_path / "s.jsonl",
        [
            _assistant_line(
                input_tokens=200,
                output_tokens=80,
                cache_creation=13,
                cache_read=27,
            )
        ],
    )
    events = await _collect(ClaudeCodeCollector(session_root=tmp_path))

    assert len(events) == 1
    evt = events[0]
    assert evt.input_tokens == 200
    assert evt.output_tokens == 80
    assert evt.cache_write_tokens == 13
    assert evt.cache_read_tokens == 27
    assert evt.reasoning_tokens == 0


@pytest.mark.asyncio
async def test_source_uses_native_path_separator(tmp_path: Path) -> None:
    _write_session(tmp_path / "proj-a" / "nested" / "s.jsonl", [_assistant_line()])
    events = await _collect(ClaudeCodeCollector(session_root=tmp_path))

    assert len(events) == 1
    src = events[0].source
    assert src.startswith("claude_code:")
    assert src.endswith(":1")
    # Native separator present — on Windows this is ``\``, on POSIX it is ``/``.
    expected_fragment = f"proj-a{os.sep}nested{os.sep}s.jsonl"
    assert expected_fragment in src


@pytest.mark.asyncio
async def test_missing_session_id_falls_back_to_filename(tmp_path: Path) -> None:
    line = json.dumps(
        {
            "type": "assistant",
            "timestamp": "2026-04-20T10:00:00Z",
            "cwd": "/tmp/x",
            "message": {
                "model": "claude-3-5-sonnet-20241022",
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        }
    )
    _write_session(tmp_path / "deadbeef.jsonl", [line])

    events = await _collect(ClaudeCodeCollector(session_root=tmp_path))
    assert len(events) == 1
    assert events[0].session_id == "deadbeef"
    assert events[0].project == "x"


@pytest.mark.asyncio
async def test_scan_returns_nothing_when_root_missing(tmp_path: Path) -> None:
    collector = ClaudeCodeCollector(session_root=tmp_path / "missing")
    events = await _collect(collector)
    assert events == []
