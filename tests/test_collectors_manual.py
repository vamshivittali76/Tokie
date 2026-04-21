"""Tests for :mod:`tokie_cli.collectors.manual`.

Covers CSV + YAML import, required-field enforcement, idempotency, env-var
discovery, and the ``messages`` → ``output_tokens`` shortcut used for chat
tools that count turns rather than tokens.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tokie_cli.collectors.manual import ManualCollector
from tokie_cli.schema import Confidence, UsageEvent


async def _collect(it: AsyncIterator[UsageEvent]) -> list[UsageEvent]:
    out: list[UsageEvent] = []
    async for evt in it:
        out.append(evt)
    return out


_HEADER = (
    "occurred_at,provider,product,account_id,model,"
    "input_tokens,output_tokens,cost_usd,notes,messages"
)


def _write_csv(path: Path, rows: list[str]) -> None:
    path.write_text(_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


async def test_csv_import_happy_path(tmp_path: Path) -> None:
    csv = tmp_path / "manual.csv"
    _write_csv(
        csv,
        [
            "2026-04-20T10:00:00Z,manus,manus-web,default,manus-v2,0,0,0.50,research,",
            "2026-04-20T11:00:00Z,wisperflow,wisperflow-web,default,whisper-dictation,0,0,,30min,",
        ],
    )
    collector = ManualCollector(log_paths=(csv,))
    events = await _collect(collector.scan())
    assert len(events) == 2
    assert events[0].provider == "manus"
    assert events[0].cost_usd == 0.50
    assert events[0].confidence is Confidence.INFERRED
    assert events[1].cost_usd is None


async def test_yaml_import_with_entries_list(tmp_path: Path) -> None:
    yml = tmp_path / "manual.yaml"
    yml.write_text(
        """
entries:
  - occurred_at: 2026-04-20T10:00:00Z
    provider: manus
    product: manus-web
    model: manus-v2
    cost_usd: 0.5
    notes: example
  - occurred_at: 2026-04-20T11:00:00Z
    provider: google
    product: gemini-web
    model: gemini-2.5-pro
    output_tokens: 5
""".strip(),
        encoding="utf-8",
    )
    collector = ManualCollector(log_paths=(yml,))
    events = await _collect(collector.scan())
    assert len(events) == 2
    assert events[1].output_tokens == 5


async def test_yaml_bare_list_top_level(tmp_path: Path) -> None:
    yml = tmp_path / "manual.yml"
    yml.write_text(
        """
- occurred_at: 2026-04-20T10:00:00Z
  provider: manus
  product: manus-web
  model: manus-v2
""".strip(),
        encoding="utf-8",
    )
    collector = ManualCollector(log_paths=(yml,))
    events = await _collect(collector.scan())
    assert len(events) == 1


async def test_mixed_csv_and_yaml_in_directory(tmp_path: Path) -> None:
    drop = tmp_path / "drop"
    drop.mkdir()
    _write_csv(
        drop / "a.csv",
        [
            "2026-04-20T10:00:00Z,manus,manus-web,default,manus-v2,0,0,0.5,,",
        ],
    )
    (drop / "b.yaml").write_text(
        """
entries:
  - occurred_at: 2026-04-20T11:00:00Z
    provider: google
    product: gemini-web
    model: gemini-2.5-pro
""".strip(),
        encoding="utf-8",
    )
    collector = ManualCollector(log_paths=(drop,))
    events = await _collect(collector.scan())
    assert {e.provider for e in events} == {"manus", "google"}


async def test_messages_column_maps_to_output_tokens(tmp_path: Path) -> None:
    csv = tmp_path / "manual.csv"
    _write_csv(
        csv,
        [
            "2026-04-20T10:00:00Z,manus,manus-web,default,manus-v2,0,,,,17",
        ],
    )
    collector = ManualCollector(log_paths=(csv,))
    events = await _collect(collector.scan())
    assert events[0].output_tokens == 17


async def test_row_missing_required_field_is_skipped(tmp_path: Path) -> None:
    csv = tmp_path / "manual.csv"
    _write_csv(
        csv,
        [
            "2026-04-20T10:00:00Z,,manus-web,default,manus-v2,0,0,,,",  # missing provider
            "2026-04-20T11:00:00Z,manus,manus-web,default,manus-v2,0,0,,,",  # valid
        ],
    )
    collector = ManualCollector(log_paths=(csv,))
    events = await _collect(collector.scan())
    assert len(events) == 1


async def test_naive_timestamp_is_skipped(tmp_path: Path) -> None:
    csv = tmp_path / "manual.csv"
    _write_csv(
        csv,
        [
            "2026-04-20T10:00:00,manus,manus-web,default,manus-v2,0,0,,,",  # no Z
            "2026-04-20T11:00:00Z,manus,manus-web,default,manus-v2,0,0,,,",
        ],
    )
    collector = ManualCollector(log_paths=(csv,))
    events = await _collect(collector.scan())
    assert len(events) == 1
    assert events[0].occurred_at == datetime(2026, 4, 20, 11, tzinfo=UTC)


async def test_since_filter(tmp_path: Path) -> None:
    csv = tmp_path / "manual.csv"
    _write_csv(
        csv,
        [
            "2026-04-20T09:00:00Z,manus,manus-web,default,manus-v2,0,0,,,",
            "2026-04-20T11:00:00Z,manus,manus-web,default,manus-v2,0,0,,,",
        ],
    )
    collector = ManualCollector(log_paths=(csv,))
    cutoff = datetime(2026, 4, 20, 10, tzinfo=UTC)
    events = await _collect(collector.scan(since=cutoff))
    assert [e.occurred_at.hour for e in events] == [11]


async def test_detect_with_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    csv = tmp_path / "manual.csv"
    _write_csv(csv, ["2026-04-20T10:00:00Z,m,m,default,m,0,0,,,"])
    monkeypatch.setenv("TOKIE_MANUAL_LOG", str(csv))
    monkeypatch.setenv("TOKIE_DATA_HOME", str(tmp_path / "empty"))
    assert ManualCollector.detect() is True


async def test_detect_false_when_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TOKIE_MANUAL_LOG", raising=False)
    monkeypatch.setenv("TOKIE_DATA_HOME", str(tmp_path / "empty"))
    assert ManualCollector.detect() is False


async def test_detect_true_with_file_in_default_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TOKIE_MANUAL_LOG", raising=False)
    monkeypatch.setenv("TOKIE_DATA_HOME", str(tmp_path))
    manual_dir = tmp_path / "manual"
    manual_dir.mkdir()
    _write_csv(manual_dir / "a.csv", ["2026-04-20T10:00:00Z,m,m,default,m,0,0,,,"])
    assert ManualCollector.detect() is True


async def test_raw_hash_stable_across_runs(tmp_path: Path) -> None:
    csv = tmp_path / "manual.csv"
    _write_csv(
        csv,
        ["2026-04-20T10:00:00Z,manus,manus-web,default,manus-v2,0,0,0.5,research,"],
    )
    collector = ManualCollector(log_paths=(csv,))
    first = [e.raw_hash for e in await _collect(collector.scan())]
    second = [e.raw_hash for e in await _collect(collector.scan())]
    assert first == second


async def test_notes_field_flows_into_source(tmp_path: Path) -> None:
    csv = tmp_path / "manual.csv"
    _write_csv(
        csv,
        ["2026-04-20T10:00:00Z,manus,manus-web,default,manus-v2,0,0,,important task,"],
    )
    collector = ManualCollector(log_paths=(csv,))
    events = await _collect(collector.scan())
    assert "important task" in events[0].source


async def test_confidence_is_always_inferred(tmp_path: Path) -> None:
    csv = tmp_path / "manual.csv"
    _write_csv(csv, ["2026-04-20T10:00:00Z,m,m,default,m,0,0,,,"])
    collector = ManualCollector(log_paths=(csv,))
    events = await _collect(collector.scan())
    assert events[0].confidence is Confidence.INFERRED
