"""Tests for :mod:`tokie_cli.collectors.copilot_cli`."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch

from tokie_cli.collectors.copilot_cli import CopilotCLICollector
from tokie_cli.schema import Confidence, UsageEvent


async def _collect(it: AsyncIterator[UsageEvent]) -> list[UsageEvent]:
    out: list[UsageEvent] = []
    async for evt in it:
        out.append(evt)
    return out


def _line(
    *,
    ts: str = "2026-04-20T10:00:00Z",
    model: str = "gpt-4o-copilot",
    prompt: int = 100,
    completion: int = 50,
    session: str = "copilot-sess-1",
) -> str:
    return json.dumps(
        {
            "timestamp": ts,
            "model": model,
            "session_id": session,
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
        }
    )


@pytest.mark.asyncio
async def test_scan_parses_prompt_and_completion_shapes(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    history = tmp_path / "history.jsonl"
    history.write_text(
        "\n".join(
            [
                _line(ts="2026-04-20T10:00:00Z"),
                json.dumps(
                    {
                        "timestamp": "2026-04-20T10:05:00Z",
                        "model": "gpt-4o-copilot",
                        "usage": {"input_tokens": 80, "output_tokens": 40},
                    }
                ),
                "not-json",
                json.dumps({"timestamp": "2026-04-20T10:10:00Z", "model": "x"}),
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TOKIE_COPILOT_LOG", str(history))

    events = await _collect(CopilotCLICollector().scan())
    assert len(events) == 2
    assert events[0].input_tokens == 100
    assert events[0].output_tokens == 50
    assert events[1].input_tokens == 80
    assert events[0].provider == "github"
    assert events[0].product == "copilot-cli"
    assert events[0].confidence is Confidence.EXACT


@pytest.mark.asyncio
async def test_since_filters_old_events(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    history = tmp_path / "history.jsonl"
    history.write_text(
        _line(ts="2026-04-19T09:00:00Z") + "\n" + _line(ts="2026-04-20T12:00:00Z"),
        encoding="utf-8",
    )
    monkeypatch.setenv("TOKIE_COPILOT_LOG", str(history))

    cutoff = datetime(2026, 4, 20, tzinfo=UTC)
    events = await _collect(CopilotCLICollector().scan(since=cutoff))
    assert len(events) == 1
    assert events[0].occurred_at.date().day == 20


def test_detect_true_when_history_present(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    history = tmp_path / "history.jsonl"
    history.write_text(_line(), encoding="utf-8")
    monkeypatch.setenv("TOKIE_COPILOT_LOG", str(history))
    assert CopilotCLICollector.detect() is True


def test_detect_false_when_history_absent(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("TOKIE_COPILOT_LOG", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert CopilotCLICollector.detect() is False


def test_health_reports_missing_source(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("TOKIE_COPILOT_LOG", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    health = CopilotCLICollector().health()
    assert health.detected is False
    assert "no copilot history" in health.message
