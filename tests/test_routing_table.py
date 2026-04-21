"""Unit tests for the task-routing table loader."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tokie_cli.routing.table import (
    RoutingTableError,
    bundled_routing_path,
    load_routing_table,
)


def _write(path: Path, data: str) -> Path:
    path.write_text(data, encoding="utf-8")
    return path


def test_bundled_file_loads_without_errors() -> None:
    table = load_routing_table()
    assert table.version == 1
    assert table.updated
    assert table.tool_ids
    assert table.task_ids
    assert "code_generation" in table.task_ids


def test_bundled_file_tasks_reference_known_tools_only() -> None:
    table = load_routing_table()
    tool_ids = set(table.tool_ids)
    for task in table.tasks:
        for entry in task.preferred:
            assert entry.tool_id in tool_ids


def test_bundled_path_is_a_file() -> None:
    assert bundled_routing_path().is_file()


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(RoutingTableError):
        load_routing_table(tmp_path / "does-not-exist.yaml")


def test_non_mapping_top_level_raises(tmp_path: Path) -> None:
    p = _write(tmp_path / "t.yaml", "- a\n- b\n")
    with pytest.raises(RoutingTableError):
        load_routing_table(p)


def test_missing_version_raises(tmp_path: Path) -> None:
    p = _write(tmp_path / "t.yaml", "tools: []\ntask_types: {}\n")
    with pytest.raises(RoutingTableError):
        load_routing_table(p)


def test_unknown_tool_in_preferred_raises(tmp_path: Path) -> None:
    yaml_content = """
version: 1
tools:
  - id: claude-code
    display_name: Claude Code
    products: [claude-code]
task_types:
  code_generation:
    description: test
    preferred:
      - tool: not-a-tool
        tier: 1
        rationale: bad ref
"""
    p = _write(tmp_path / "t.yaml", yaml_content.strip())
    with pytest.raises(RoutingTableError):
        load_routing_table(p)


def test_duplicate_tool_id_raises(tmp_path: Path) -> None:
    yaml_content = """
version: 1
tools:
  - id: claude-code
    display_name: Claude Code
    products: [claude-code]
  - id: claude-code
    display_name: Dup
    products: [claude-code]
task_types:
  code_generation:
    description: test
    preferred:
      - tool: claude-code
        tier: 1
        rationale: ok
"""
    p = _write(tmp_path / "t.yaml", yaml_content.strip())
    with pytest.raises(RoutingTableError):
        load_routing_table(p)


def test_empty_preferred_raises(tmp_path: Path) -> None:
    yaml_content = """
version: 1
tools:
  - id: claude-code
    display_name: Claude Code
    products: [claude-code]
task_types:
  code_generation:
    description: test
    preferred: []
"""
    p = _write(tmp_path / "t.yaml", yaml_content.strip())
    with pytest.raises(RoutingTableError):
        load_routing_table(p)


def test_tier_must_be_positive(tmp_path: Path) -> None:
    yaml_content = """
version: 1
tools:
  - id: claude-code
    display_name: Claude Code
    products: [claude-code]
task_types:
  code_generation:
    description: test
    preferred:
      - tool: claude-code
        tier: 0
        rationale: bad tier
"""
    p = _write(tmp_path / "t.yaml", yaml_content.strip())
    with pytest.raises(RoutingTableError):
        load_routing_table(p)


def test_lookup_helpers(tmp_path: Path) -> None:
    yaml_content = """
version: 1
tools:
  - id: claude-code
    display_name: Claude Code
    products: [claude-code]
task_types:
  code_generation:
    description: test
    preferred:
      - tool: claude-code
        tier: 1
        rationale: best
"""
    p = _write(tmp_path / "t.yaml", yaml_content.strip())
    table = load_routing_table(p)
    assert table.tool("claude-code").display_name == "Claude Code"
    assert table.task("code_generation").preferred[0].tier == 1
    with pytest.raises(KeyError):
        table.tool("nope")
    with pytest.raises(KeyError):
        table.task("nope")


def test_yaml_parse_error_raises(tmp_path: Path) -> None:
    p = _write(tmp_path / "t.yaml", ":\n  invalid: [unterminated")
    with pytest.raises(RoutingTableError):
        load_routing_table(p)


def test_rationale_defaults_to_empty_string(tmp_path: Path) -> None:
    yaml_content = """
version: 1
tools:
  - id: claude-code
    display_name: Claude Code
    products: [claude-code]
task_types:
  quick:
    description: ""
    preferred:
      - tool: claude-code
        tier: 2
"""
    p = _write(tmp_path / "t.yaml", yaml_content.strip())
    table = load_routing_table(p)
    entry: Any = table.task("quick").preferred[0]
    assert entry.rationale == ""
    assert entry.tier == 2
