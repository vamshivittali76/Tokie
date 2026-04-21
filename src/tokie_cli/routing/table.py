"""Loader + validator for ``task_routing.yaml``.

The file ships inside the wheel at ``tokie_cli/task_routing.yaml`` and
uses the same hand-tuned shape as ``plans.yaml``: a top-level ``version``,
a ``tools`` list and a ``task_types`` map of task -> ranked tool
recommendations.

This module deliberately knows nothing about the recommender or the
user's config — it returns a frozen :class:`RoutingTable` that other
layers can consume. Any YAML shape error raises
:class:`RoutingTableError` with the offending id in the message so the
error is usable from the CLI.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "DEFAULT_ROUTING_FILENAME",
    "RoutingTable",
    "RoutingTableError",
    "TaskEntry",
    "TaskRecommendationEntry",
    "ToolEntry",
    "bundled_routing_path",
    "load_routing_table",
]

DEFAULT_ROUTING_FILENAME: str = "task_routing.yaml"


class RoutingTableError(Exception):
    """Raised when ``task_routing.yaml`` is missing or malformed."""


@dataclass(frozen=True)
class ToolEntry:
    """One row from the top-level ``tools`` list.

    ``products`` is the set of :class:`UsageEvent.product` strings that
    satisfy this tool. A user "has" ``claude-code`` if any of their
    subscriptions declares a window shared with ``claude-code`` *or*
    whose own :attr:`Subscription.product` is ``claude-code``.
    """

    id: str
    display_name: str
    products: tuple[str, ...]
    notes: str | None


@dataclass(frozen=True)
class TaskRecommendationEntry:
    """One ranked entry inside a task type's ``preferred`` list."""

    tool_id: str
    tier: int
    rationale: str


@dataclass(frozen=True)
class TaskEntry:
    """One entry in the top-level ``task_types`` map."""

    id: str
    description: str
    preferred: tuple[TaskRecommendationEntry, ...]


@dataclass(frozen=True)
class RoutingTable:
    """Parsed ``task_routing.yaml``. Immutable by design.

    Access tools by id via :meth:`tool` and task entries via
    :meth:`task`. Both methods raise :class:`KeyError` on miss — the
    caller is expected to pre-validate via :attr:`task_ids` /
    :attr:`tool_ids`.
    """

    version: int
    updated: str
    tools: tuple[ToolEntry, ...]
    tasks: tuple[TaskEntry, ...]

    @property
    def task_ids(self) -> tuple[str, ...]:
        return tuple(t.id for t in self.tasks)

    @property
    def tool_ids(self) -> tuple[str, ...]:
        return tuple(t.id for t in self.tools)

    def tool(self, tool_id: str) -> ToolEntry:
        for entry in self.tools:
            if entry.id == tool_id:
                return entry
        raise KeyError(
            f"No tool with id {tool_id!r}. Known tools: {', '.join(self.tool_ids)}"
        )

    def task(self, task_id: str) -> TaskEntry:
        for entry in self.tasks:
            if entry.id == task_id:
                return entry
        raise KeyError(
            f"No task with id {task_id!r}. Known tasks: {', '.join(self.task_ids)}"
        )


def bundled_routing_path() -> Path:
    """Return the filesystem path to the bundled ``task_routing.yaml``."""

    return Path(str(files("tokie_cli").joinpath(DEFAULT_ROUTING_FILENAME)))


def load_routing_table(path: Path | str | None = None) -> RoutingTable:
    """Load and validate the routing table.

    When ``path`` is ``None`` the bundled copy is used. YAML is parsed
    with :func:`yaml.safe_load`. Any shape error raises
    :class:`RoutingTableError`.
    """

    resolved = Path(path) if path is not None else bundled_routing_path()

    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise RoutingTableError(
            f"Could not read routing file at {resolved}: {exc}"
        ) from exc

    try:
        parsed: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RoutingTableError(
            f"Invalid YAML in routing file {resolved}: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise RoutingTableError(
            f"Routing file {resolved} must contain a YAML mapping at the top level."
        )

    version = parsed.get("version")
    if not isinstance(version, int):
        raise RoutingTableError(
            f"Routing file {resolved} is missing an integer 'version' field."
        )

    updated_raw = parsed.get("updated")
    updated = str(updated_raw) if updated_raw is not None else ""

    tools = _parse_tools(parsed.get("tools"), resolved)
    tools_index = {t.id for t in tools}
    tasks = _parse_tasks(parsed.get("task_types"), resolved, tools_index)

    return RoutingTable(version=version, updated=updated, tools=tools, tasks=tasks)


def _parse_tools(raw: Any, source: Path) -> tuple[ToolEntry, ...]:
    if not isinstance(raw, list):
        raise RoutingTableError(
            f"Routing file {source} must contain a 'tools' list."
        )

    out: list[ToolEntry] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise RoutingTableError(
                f"tools[{index}] in {source} must be a mapping, got {type(entry).__name__}."
            )
        tool_id = entry.get("id")
        display_name = entry.get("display_name")
        products = entry.get("products")
        notes = entry.get("notes")

        if not isinstance(tool_id, str) or not tool_id:
            raise RoutingTableError(
                f"tools[{index}].id in {source} must be a non-empty string."
            )
        if tool_id in seen:
            raise RoutingTableError(f"Duplicate tool id {tool_id!r} in {source}.")
        seen.add(tool_id)
        if not isinstance(display_name, str) or not display_name:
            raise RoutingTableError(
                f"tools[{tool_id}].display_name in {source} must be a non-empty string."
            )
        if not isinstance(products, list) or not all(
            isinstance(p, str) and p for p in products
        ):
            raise RoutingTableError(
                f"tools[{tool_id}].products in {source} must be a list of non-empty strings."
            )
        if notes is not None and not isinstance(notes, str):
            raise RoutingTableError(
                f"tools[{tool_id}].notes in {source} must be a string or omitted."
            )

        out.append(
            ToolEntry(
                id=tool_id,
                display_name=display_name,
                products=tuple(products),
                notes=notes,
            )
        )
    return tuple(out)


def _parse_tasks(
    raw: Any, source: Path, known_tool_ids: set[str]
) -> tuple[TaskEntry, ...]:
    if not isinstance(raw, dict):
        raise RoutingTableError(
            f"Routing file {source} must contain a 'task_types' mapping."
        )

    out: list[TaskEntry] = []
    for task_id, body in raw.items():
        if not isinstance(task_id, str) or not task_id:
            raise RoutingTableError(
                f"task_types keys in {source} must be non-empty strings."
            )
        if not isinstance(body, dict):
            raise RoutingTableError(
                f"task_types[{task_id}] in {source} must be a mapping."
            )
        description = body.get("description", "")
        if not isinstance(description, str):
            raise RoutingTableError(
                f"task_types[{task_id}].description in {source} must be a string."
            )
        preferred_raw = body.get("preferred")
        if not isinstance(preferred_raw, list) or not preferred_raw:
            raise RoutingTableError(
                f"task_types[{task_id}].preferred in {source} must be a non-empty list."
            )
        preferred: list[TaskRecommendationEntry] = []
        for p_index, entry in enumerate(preferred_raw):
            if not isinstance(entry, dict):
                raise RoutingTableError(
                    f"task_types[{task_id}].preferred[{p_index}] must be a mapping."
                )
            tool = entry.get("tool")
            tier = entry.get("tier", 2)
            rationale = entry.get("rationale", "")
            if not isinstance(tool, str) or tool not in known_tool_ids:
                raise RoutingTableError(
                    f"task_types[{task_id}].preferred[{p_index}].tool {tool!r} "
                    f"is not a known tool id."
                )
            if not isinstance(tier, int) or tier < 1 or isinstance(tier, bool):
                raise RoutingTableError(
                    f"task_types[{task_id}].preferred[{p_index}].tier "
                    f"must be a positive integer."
                )
            if not isinstance(rationale, str):
                raise RoutingTableError(
                    f"task_types[{task_id}].preferred[{p_index}].rationale "
                    f"must be a string."
                )
            preferred.append(
                TaskRecommendationEntry(
                    tool_id=tool, tier=tier, rationale=rationale
                )
            )
        out.append(
            TaskEntry(
                id=task_id,
                description=description,
                preferred=tuple(preferred),
            )
        )
    return tuple(out)
