"""Tests for :mod:`tokie_cli.collectors.cursor_ide`."""

from __future__ import annotations

import csv
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from _pytest.monkeypatch import MonkeyPatch

from tokie_cli.collectors.cursor_ide import (
    _ESTIMATED_INPUT_TOKENS_PER_REQUEST,
    _ESTIMATED_OUTPUT_TOKENS_PER_REQUEST,
    CursorIDECollector,
)
from tokie_cli.schema import Confidence, UsageEvent


async def _collect(it: AsyncIterator[UsageEvent]) -> list[UsageEvent]:
    out: list[UsageEvent] = []
    async for evt in it:
        out.append(evt)
    return out


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


@pytest.mark.asyncio
async def test_csv_rows_emit_estimated_events(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    csv_path = tmp_path / "requests.csv"
    _write_csv(
        csv_path,
        [
            {
                "request_id": "req-1",
                "timestamp": "2026-04-20T10:00:00Z",
                "model": "gpt-4o",
            },
            {
                "request_id": "req-2",
                "timestamp": "2026-04-20T11:00:00Z",
                "model": "claude-sonnet-4.5",
            },
        ],
    )
    monkeypatch.setenv("TOKIE_CURSOR_LOG", str(csv_path))

    events = await _collect(CursorIDECollector().scan())
    assert len(events) == 2
    for evt in events:
        assert evt.provider == "cursor"
        assert evt.product == "cursor-ide"
        assert evt.confidence is Confidence.ESTIMATED
        assert evt.input_tokens == _ESTIMATED_INPUT_TOKENS_PER_REQUEST
        assert evt.output_tokens == _ESTIMATED_OUTPUT_TOKENS_PER_REQUEST


@pytest.mark.asyncio
async def test_jsonl_with_usage_block_is_exact(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    jsonl = tmp_path / "drop.jsonl"
    jsonl.write_text(
        json.dumps(
            {
                "timestamp": "2026-04-20T12:00:00Z",
                "model": "gpt-4o",
                "usage": {"prompt_tokens": 200, "completion_tokens": 80},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TOKIE_CURSOR_LOG", str(jsonl))

    events = await _collect(CursorIDECollector().scan())
    assert len(events) == 1
    assert events[0].confidence is Confidence.EXACT
    assert events[0].input_tokens == 200


def test_detect_false_without_drops(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("TOKIE_CURSOR_LOG", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert CursorIDECollector.detect() is False


def test_health_warns_about_estimated_confidence(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    csv_path = tmp_path / "requests.csv"
    csv_path.write_text("request_id,timestamp,model\nreq-1,2026-04-20T10:00:00Z,gpt-4o\n")
    monkeypatch.setenv("TOKIE_CURSOR_LOG", str(csv_path))
    health = CursorIDECollector().health()
    assert health.detected is True
    assert any("ESTIMATED" in w for w in health.warnings)
