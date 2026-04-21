"""Collector discovery registry.

Tokie ships with a handful of built-in collectors. Third parties add their
own by publishing a package that registers an entry point in the
``tokie.collectors`` group:

.. code-block:: toml

    # In your connector package's pyproject.toml
    [project.entry-points."tokie.collectors"]
    my_vendor = "my_tokie_connector:MyCollector"

The registry merges built-ins with discovered entry points. Built-ins
always win on name collision — this protects users from a rogue package
shadowing ``claude_code`` — but collisions emit a visible warning so the
third-party maintainer knows to pick a different name.

Design
------
- **Pure discovery, lazy instantiation.** The registry returns classes,
  not instances. Construction is the CLI's responsibility because some
  collectors (e.g. ``openai-compat``) require per-call configuration.
- **Fail loud, not silent.** A broken entry point raises
  :class:`CollectorRegistrationError` with context about which package
  tried to register what — we never swallow import errors and pretend
  the collector simply isn't there.
- **Snapshot, not singleton.** :func:`load_registry` returns a fresh
  ``dict`` every call so tests can monkeypatch ``importlib.metadata``
  between cases without hitting a cached view.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from importlib.metadata import EntryPoint, entry_points
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tokie_cli.collectors.base import Collector

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "tokie.collectors"


class CollectorRegistrationError(RuntimeError):
    """A ``tokie.collectors`` entry point failed to load or validate.

    The message always names the offending distribution + entry point so
    operators can open an issue against the right package.
    """


def _builtin_collectors() -> dict[str, type[Collector]]:
    """Return the built-in collector name -> class map.

    Imported lazily so the registry module is importable even if one of
    the built-in collectors drags in an optional dependency that happens
    to be missing in a minimal install.
    """

    from tokie_cli.collectors.api_anthropic import (
        AnthropicAPICollector,
    )
    from tokie_cli.collectors.api_gemini import (
        GeminiAPICollector,
    )
    from tokie_cli.collectors.api_openai import (
        OpenAIAPICollector,
    )
    from tokie_cli.collectors.api_openai_compatible import (
        OpenAICompatibleCollector,
    )
    from tokie_cli.collectors.claude_code import (
        ClaudeCodeCollector,
    )
    from tokie_cli.collectors.codex import (
        CodexCollector,
    )
    from tokie_cli.collectors.copilot_cli import (
        CopilotCLICollector,
    )
    from tokie_cli.collectors.cursor_ide import (
        CursorIDECollector,
    )
    from tokie_cli.collectors.manual import (
        ManualCollector,
    )
    from tokie_cli.collectors.perplexity_api import (
        PerplexityAPICollector,
    )

    return {
        ClaudeCodeCollector.name: ClaudeCodeCollector,
        CodexCollector.name: CodexCollector,
        AnthropicAPICollector.name: AnthropicAPICollector,
        OpenAIAPICollector.name: OpenAIAPICollector,
        GeminiAPICollector.name: GeminiAPICollector,
        OpenAICompatibleCollector.name: OpenAICompatibleCollector,
        CopilotCLICollector.name: CopilotCLICollector,
        PerplexityAPICollector.name: PerplexityAPICollector,
        CursorIDECollector.name: CursorIDECollector,
        ManualCollector.name: ManualCollector,
    }


def _iter_entry_points() -> Iterable[EntryPoint]:
    """Return every entry point registered under :data:`ENTRY_POINT_GROUP`.

    Wrapped so tests can monkeypatch a deterministic list without having
    to build real ``EntryPoint`` objects from distribution metadata.
    """

    return entry_points(group=ENTRY_POINT_GROUP)


def _load_entry_point(ep: EntryPoint) -> type[Collector]:
    """Import the object at ``ep`` and validate the :class:`Collector` contract.

    Validation is intentionally cheap: we only check for the class-level
    ``name`` attribute and that the loaded object is a type. A fully
    rigorous structural check belongs in the future
    ``pytest-tokie-connector`` plugin.
    """

    from tokie_cli.collectors.base import Collector

    try:
        obj = ep.load()
    except Exception as exc:  # pragma: no cover - defensive
        raise CollectorRegistrationError(
            f"failed to import {ENTRY_POINT_GROUP} entry point "
            f"{ep.name!r} ({ep.value!r}): {exc}"
        ) from exc

    if not isinstance(obj, type) or not issubclass(obj, Collector):
        raise CollectorRegistrationError(
            f"{ENTRY_POINT_GROUP} entry point {ep.name!r} "
            f"({ep.value!r}) did not resolve to a Collector subclass"
        )

    if not getattr(obj, "name", None):
        raise CollectorRegistrationError(
            f"{ENTRY_POINT_GROUP} entry point {ep.name!r} "
            f"({ep.value!r}) is missing a class-level `name` attribute"
        )

    return obj


def discover_third_party() -> dict[str, type[Collector]]:
    """Return only the collectors registered via entry points.

    Useful for ``tokie doctor`` + tests that want to distinguish
    "ships with Tokie" from "brought in by a plugin".
    """

    out: dict[str, type[Collector]] = {}
    for ep in _iter_entry_points():
        try:
            cls = _load_entry_point(ep)
        except CollectorRegistrationError as exc:
            logger.warning("ignoring broken tokie.collectors entry point: %s", exc)
            continue
        existing = out.get(cls.name)
        if existing is not None and existing is not cls:
            logger.warning(
                "duplicate tokie.collectors registration for %r (second was %s); "
                "keeping the first",
                cls.name,
                ep.value,
            )
            continue
        out[cls.name] = cls
    return out


def load_registry(
    *,
    extras: Mapping[str, type[Collector]] | None = None,
) -> dict[str, type[Collector]]:
    """Return the merged collector registry.

    Precedence (highest wins):
    1. Built-in collectors shipped with Tokie.
    2. ``extras`` passed in explicitly (used by tests).
    3. Third-party entry points under ``tokie.collectors``.

    Conflicts with built-ins are logged at ``WARNING`` and the built-in
    wins. This is deliberate: a plugin masquerading as ``claude_code``
    would be both surprising and a security issue.
    """

    merged: dict[str, type[Collector]] = {}
    for name, cls in discover_third_party().items():
        merged[name] = cls
    if extras:
        for name, cls in extras.items():
            merged[name] = cls
    for name, cls in _builtin_collectors().items():
        existing = merged.get(name)
        if existing is not None and existing is not cls:
            logger.warning(
                "third-party collector %r shadowed by built-in; keeping the built-in",
                name,
            )
        merged[name] = cls
    return merged


def get_collector(
    name: str,
    *,
    registry: Mapping[str, type[Collector]] | None = None,
) -> type[Collector]:
    """Look up a collector class by name.

    Raises :class:`KeyError` — callers decide whether to translate that
    into ``typer.BadParameter`` or a JSON error.
    """

    reg = registry if registry is not None else load_registry()
    try:
        return reg[name]
    except KeyError as exc:
        valid = ", ".join(sorted(reg))
        raise KeyError(f"unknown collector {name!r}. Valid: {valid}.") from exc


__all__ = [
    "ENTRY_POINT_GROUP",
    "CollectorRegistrationError",
    "discover_third_party",
    "get_collector",
    "load_registry",
]
