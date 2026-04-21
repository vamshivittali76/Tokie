"""Tests for :mod:`tokie_cli.collectors.perplexity_api`."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch

from tokie_cli.collectors.perplexity_api import PerplexityAPICollector
from tokie_cli.schema import UsageEvent


async def _collect(it: AsyncIterator[UsageEvent]) -> list[UsageEvent]:
    out: list[UsageEvent] = []
    async for evt in it:
        out.append(evt)
    return out


def _line(
    *,
    ts: str = "2026-04-20T10:00:00Z",
    model: str = "sonar-large-online",
    prompt: int = 120,
    completion: int = 60,
) -> str:
    return json.dumps(
        {
            "timestamp": ts,
            "model": model,
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
        }
    )


@pytest.mark.asyncio
async def test_scan_parses_usage(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    history = tmp_path / "history.jsonl"
    history.write_text(_line() + "\n" + _line(model="sonar-small", prompt=10, completion=5))
    monkeypatch.setenv("TOKIE_PERPLEXITY_LOG", str(history))

    events = await _collect(PerplexityAPICollector().scan())
    assert len(events) == 2
    assert events[0].provider == "perplexity"
    assert events[0].model == "sonar-large-online"
    assert events[1].input_tokens == 10


def test_health_mentions_vendor_gap_when_key_stored_but_no_logs(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.delenv("TOKIE_PERPLEXITY_LOG", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        "tokie_cli.collectors.perplexity_api._keyring_has_key", lambda: True
    )
    health = PerplexityAPICollector().health()
    assert health.detected is True
    assert health.ok is False
    assert any("vendor" in w for w in health.warnings)


def test_detect_false_without_logs_or_key(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("TOKIE_PERPLEXITY_LOG", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        "tokie_cli.collectors.perplexity_api._keyring_has_key", lambda: False
    )
    assert PerplexityAPICollector.detect() is False
