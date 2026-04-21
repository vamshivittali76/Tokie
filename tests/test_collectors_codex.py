"""Tests for :class:`tokie_cli.collectors.codex.CodexCollector`.

Every test writes synthetic JSONL rollouts into a ``tmp_path`` so we never
touch the real ``~/.codex`` directory. The shape of the fixture lines is
copied from the two Codex formats documented in the collector module.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tokie_cli.collectors.codex import CodexCollector
from tokie_cli.schema import Confidence, UsageEvent


def _shape_a(
    *,
    ts: str = "2026-04-20T12:00:00Z",
    session_id: str = "sess-a",
    model: str = "gpt-5-codex",
    input_tokens: int = 123,
    output_tokens: int = 456,
    cached_input_tokens: int = 0,
    reasoning_tokens: int = 100,
) -> dict[str, object]:
    return {
        "timestamp": ts,
        "type": "response_complete",
        "session_id": session_id,
        "model": model,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_input_tokens": cached_input_tokens,
            "reasoning_tokens": reasoning_tokens,
        },
    }


def _shape_b(
    *,
    ts: str = "2026-04-20T12:05:00Z",
    model: str = "gpt-5-codex",
    prompt_tokens: int = 222,
    completion_tokens: int = 333,
    cached_tokens: int = 7,
) -> dict[str, object]:
    return {
        "timestamp": ts,
        "role": "assistant",
        "model": model,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "cached_tokens": cached_tokens,
        },
    }


def _write_rollout(
    root: Path,
    *,
    date: tuple[str, str, str] = ("2026", "04", "20"),
    name: str = "rollout-abc.jsonl",
    lines: list[dict[str, object] | str],
) -> Path:
    y, m, d = date
    folder = root / y / m / d
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    with path.open("w", encoding="utf-8") as fp:
        for line in lines:
            if isinstance(line, str):
                fp.write(line)
            else:
                fp.write(json.dumps(line))
            fp.write("\n")
    return path


async def _collect(it: AsyncIterator[UsageEvent]) -> list[UsageEvent]:
    out: list[UsageEvent] = []
    async for evt in it:
        out.append(evt)
    return out


async def test_scan_shape_a_happy_path(tmp_path: Path) -> None:
    _write_rollout(tmp_path, lines=[_shape_a()])
    collector = CodexCollector(session_root=tmp_path)

    events = await _collect(collector.scan())

    assert len(events) == 1
    evt = events[0]
    assert evt.provider == "openai"
    assert evt.product == "codex"
    assert evt.account_id == "default"
    assert evt.model == "gpt-5-codex"
    assert evt.input_tokens == 123
    assert evt.output_tokens == 456
    assert evt.reasoning_tokens == 100
    assert evt.cache_read_tokens == 0
    assert evt.confidence is Confidence.EXACT
    assert evt.occurred_at == datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    assert evt.session_id == "sess-a"
    assert evt.project is None
    assert evt.source.startswith("codex:2026/04/20/rollout-abc.jsonl:")


async def test_scan_shape_b_happy_path(tmp_path: Path) -> None:
    _write_rollout(tmp_path, lines=[_shape_b()])
    collector = CodexCollector(session_root=tmp_path)

    events = await _collect(collector.scan())

    assert len(events) == 1
    evt = events[0]
    assert evt.input_tokens == 222
    assert evt.output_tokens == 333
    assert evt.cache_read_tokens == 7
    assert evt.reasoning_tokens == 0
    assert evt.session_id == "rollout-abc"


async def test_scan_mixed_shapes_and_skips_non_usage_lines(tmp_path: Path) -> None:
    lines: list[dict[str, object] | str] = [
        {"timestamp": "2026-04-20T11:00:00Z", "role": "user", "content": "hi"},
        _shape_a(ts="2026-04-20T11:01:00Z", session_id="s-mix"),
        {"type": "tool_call", "name": "shell"},
        _shape_b(ts="2026-04-20T11:02:00Z"),
    ]
    _write_rollout(tmp_path, lines=lines)

    events = await _collect(CodexCollector(session_root=tmp_path).scan())

    assert len(events) == 2
    shapes = {(evt.input_tokens, evt.output_tokens) for evt in events}
    assert shapes == {(123, 456), (222, 333)}


async def test_scan_skips_malformed_json_between_valid_lines(tmp_path: Path) -> None:
    lines: list[dict[str, object] | str] = [
        _shape_a(ts="2026-04-20T10:00:00Z"),
        "{not valid json",
        _shape_b(ts="2026-04-20T10:05:00Z"),
    ]
    _write_rollout(tmp_path, lines=lines)

    events = await _collect(CodexCollector(session_root=tmp_path).scan())

    assert len(events) == 2


async def test_since_filter_drops_earlier_events(tmp_path: Path) -> None:
    _write_rollout(
        tmp_path,
        lines=[
            _shape_a(ts="2026-04-20T08:00:00Z"),
            _shape_a(ts="2026-04-20T09:30:00Z", session_id="later"),
        ],
    )
    collector = CodexCollector(session_root=tmp_path)

    cutoff = datetime(2026, 4, 20, 9, 0, tzinfo=UTC)
    events = await _collect(collector.scan(since=cutoff))

    assert len(events) == 1
    assert events[0].session_id == "later"


def test_detect_true_when_default_root_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOKIE_CODEX_SESSION_ROOT", str(tmp_path))
    assert CodexCollector.detect() is True


def test_detect_false_when_default_root_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("TOKIE_CODEX_SESSION_ROOT", str(missing))
    assert CodexCollector.detect() is False


def test_health_reports_ok_for_readable_directory(tmp_path: Path) -> None:
    _write_rollout(tmp_path, lines=[_shape_a()])
    health = CodexCollector(session_root=tmp_path).health()

    assert health.name == "codex"
    assert health.detected is True
    assert health.ok is True
    assert health.warnings == ()
    assert "1 session file(s)" in health.message


def test_health_warns_on_unreadable_file(tmp_path: Path) -> None:
    path = _write_rollout(tmp_path, lines=[_shape_a()])

    # Best-effort: if chmod can't make the file unreadable (common on Windows),
    # skip rather than lie about what the collector does.
    try:
        os.chmod(path, 0o000)
        if os.access(path, os.R_OK):
            pytest.skip("filesystem ignores permission bits")
    except OSError:
        pytest.skip("chmod not supported here")

    try:
        health = CodexCollector(session_root=tmp_path).health()
        assert health.detected is True
        assert health.ok is False
        assert any("unreadable" in w for w in health.warnings)
    finally:
        os.chmod(path, 0o600)


def test_health_reports_missing_root(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    health = CodexCollector(session_root=missing).health()
    assert health.detected is False
    assert health.ok is False
    assert "not found" in health.message


async def test_scan_is_idempotent_across_runs(tmp_path: Path) -> None:
    _write_rollout(
        tmp_path,
        lines=[
            _shape_a(ts="2026-04-20T07:00:00Z"),
            _shape_b(ts="2026-04-20T07:05:00Z"),
        ],
    )
    collector = CodexCollector(session_root=tmp_path)

    first = await _collect(collector.scan())
    second = await _collect(collector.scan())

    assert {e.raw_hash for e in first} == {e.raw_hash for e in second}
    assert len(first) == len(second) == 2


async def test_env_var_overrides_session_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_rollout(tmp_path, lines=[_shape_a(session_id="env-sess")])
    monkeypatch.setenv("TOKIE_CODEX_SESSION_ROOT", str(tmp_path))

    collector = CodexCollector()
    assert collector.session_root == tmp_path

    events = await _collect(collector.scan())
    assert len(events) == 1
    assert events[0].session_id == "env-sess"


async def test_reasoning_tokens_populate_only_under_shape_a(tmp_path: Path) -> None:
    _write_rollout(
        tmp_path,
        name="rollout-a.jsonl",
        lines=[_shape_a(reasoning_tokens=555)],
    )
    _write_rollout(
        tmp_path,
        date=("2026", "04", "21"),
        name="rollout-b.jsonl",
        lines=[_shape_b()],
    )

    events = await _collect(CodexCollector(session_root=tmp_path).scan())
    by_input = {e.input_tokens: e for e in events}

    assert by_input[123].reasoning_tokens == 555
    assert by_input[222].reasoning_tokens == 0


async def test_nested_date_directories_are_all_scanned(tmp_path: Path) -> None:
    _write_rollout(
        tmp_path,
        date=("2026", "04", "18"),
        name="rollout-1.jsonl",
        lines=[_shape_a(ts="2026-04-18T09:00:00Z", session_id="day-18")],
    )
    _write_rollout(
        tmp_path,
        date=("2026", "04", "19"),
        name="rollout-2.jsonl",
        lines=[_shape_a(ts="2026-04-19T09:00:00Z", session_id="day-19")],
    )
    _write_rollout(
        tmp_path,
        date=("2026", "04", "20"),
        name="rollout-3.jsonl",
        lines=[_shape_a(ts="2026-04-20T09:00:00Z", session_id="day-20")],
    )

    events = await _collect(CodexCollector(session_root=tmp_path).scan())
    assert {e.session_id for e in events} == {"day-18", "day-19", "day-20"}
    for evt in events:
        assert "/" in evt.source.split(":", 1)[1]


async def test_line_missing_timestamp_is_skipped(tmp_path: Path) -> None:
    payload = _shape_a()
    del payload["timestamp"]
    _write_rollout(tmp_path, lines=[payload, _shape_a(ts="2026-04-20T13:00:00Z")])

    events = await _collect(CodexCollector(session_root=tmp_path).scan())
    assert len(events) == 1
    assert events[0].occurred_at == datetime(2026, 4, 20, 13, 0, tzinfo=UTC)


async def test_raw_hash_differs_between_lines(tmp_path: Path) -> None:
    _write_rollout(
        tmp_path,
        lines=[
            _shape_a(ts="2026-04-20T12:00:00Z", session_id="one"),
            _shape_a(
                ts=(datetime(2026, 4, 20, 12, 0, tzinfo=UTC) + timedelta(minutes=1))
                .isoformat()
                .replace("+00:00", "Z"),
                session_id="two",
            ),
        ],
    )

    events = await _collect(CodexCollector(session_root=tmp_path).scan())
    assert len({e.raw_hash for e in events}) == 2
