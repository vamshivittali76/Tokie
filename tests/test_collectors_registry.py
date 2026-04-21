"""Tests for :mod:`tokie_cli.collectors.registry`.

Focus:
- Built-in collectors are always visible, even with no entry points.
- Well-formed third-party entry points are discovered and merged.
- Broken / misconfigured entry points fail loud (non-Collector, missing
  name attribute) or are logged and skipped (import errors).
- Built-ins beat third-party on name collision, with a warning.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from datetime import datetime
from importlib.metadata import EntryPoint

import pytest

from tokie_cli.collectors import (
    Collector,
    CollectorRegistrationError,
    discover_third_party,
    get_collector,
    load_registry,
)
from tokie_cli.collectors import registry as registry_mod
from tokie_cli.collectors.api_openai_compatible import OpenAICompatibleCollector
from tokie_cli.collectors.claude_code import ClaudeCodeCollector
from tokie_cli.schema import Confidence, UsageEvent


class _GoodCollector(Collector):
    name = "fake_vendor"
    default_confidence = Confidence.INFERRED

    @classmethod
    def detect(cls) -> bool:
        return False

    def scan(self, since: datetime | None = None) -> AsyncIterator[UsageEvent]:
        async def _empty() -> AsyncIterator[UsageEvent]:
            if False:  # pragma: no cover - generator placeholder
                yield  # type: ignore[unreachable]

        return _empty()


class _NotACollector:
    """Deliberately NOT a Collector subclass."""

    name = "bogus"


class _NamelessCollector(Collector):
    """Missing the required class-level ``name``."""

    default_confidence = Confidence.INFERRED

    @classmethod
    def detect(cls) -> bool:
        return False

    def scan(self, since: datetime | None = None) -> AsyncIterator[UsageEvent]:
        async def _empty() -> AsyncIterator[UsageEvent]:
            if False:  # pragma: no cover
                yield  # type: ignore[unreachable]

        return _empty()


class _FakeEntryPoint:
    """Stand-in for :class:`importlib.metadata.EntryPoint`.

    Implements the narrow surface (``name``, ``value``, ``load``) that
    :func:`_load_entry_point` actually touches; keeps tests hermetic from
    real package metadata.
    """

    def __init__(self, name: str, value: str, target: object) -> None:
        self.name = name
        self.value = value
        self._target = target

    def load(self) -> object:
        if isinstance(self._target, Exception):
            raise self._target
        return self._target


def _patch_entry_points(
    monkeypatch: pytest.MonkeyPatch, eps: Iterable[_FakeEntryPoint]
) -> None:
    monkeypatch.setattr(registry_mod, "_iter_entry_points", lambda: tuple(eps))


def test_load_registry_has_builtins_with_no_plugins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_entry_points(monkeypatch, [])
    reg = load_registry()
    assert "claude-code" in reg
    assert "openai-compat" in reg
    assert reg["claude-code"] is ClaudeCodeCollector
    assert reg["openai-compat"] is OpenAICompatibleCollector


def test_discover_third_party_only_returns_plugins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_entry_points(
        monkeypatch,
        [_FakeEntryPoint("fake_vendor", "pkg:cls", _GoodCollector)],
    )
    third = discover_third_party()
    assert "fake_vendor" in third
    assert third["fake_vendor"] is _GoodCollector
    # Discovery MUST NOT return built-ins.
    assert "claude-code" not in third


def test_load_registry_merges_plugin_collectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_entry_points(
        monkeypatch,
        [_FakeEntryPoint("fake_vendor", "pkg:cls", _GoodCollector)],
    )
    reg = load_registry()
    assert reg["fake_vendor"] is _GoodCollector
    assert reg["claude-code"] is ClaudeCodeCollector


def test_builtin_shadows_third_party_with_same_name(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _EvilTwin(_GoodCollector):
        name = "claude-code"

    _patch_entry_points(
        monkeypatch,
        [_FakeEntryPoint("evil", "pkg:cls", _EvilTwin)],
    )
    caplog.set_level("WARNING", logger="tokie_cli.collectors.registry")
    reg = load_registry()
    assert reg["claude-code"] is ClaudeCodeCollector  # built-in wins
    assert any(
        "shadowed by built-in" in r.message for r in caplog.records
    ), caplog.records


def test_non_collector_entry_point_raises_registration_error() -> None:
    ep = _FakeEntryPoint("bad", "pkg:cls", _NotACollector)
    with pytest.raises(CollectorRegistrationError, match="did not resolve"):
        registry_mod._load_entry_point(ep)  # type: ignore[arg-type]


def test_collector_without_name_raises_registration_error() -> None:
    # `name` is missing at the class level -> attribute falls through to
    # :class:`Collector` where it's declared but not defined, so
    # ``getattr(cls, "name", None)`` returns ``None`` at validation time.
    ep = _FakeEntryPoint("nameless", "pkg:cls", _NamelessCollector)
    with pytest.raises(CollectorRegistrationError, match="missing a class-level"):
        registry_mod._load_entry_point(ep)  # type: ignore[arg-type]


def test_broken_entry_point_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ep = _FakeEntryPoint("broken", "pkg:cls", _NotACollector)
    _patch_entry_points(monkeypatch, [ep])
    caplog.set_level("WARNING", logger="tokie_cli.collectors.registry")
    # discover_third_party MUST keep going past a single broken plugin.
    assert discover_third_party() == {}
    assert any("ignoring broken" in r.message for r in caplog.records)


def test_get_collector_returns_the_class(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_entry_points(monkeypatch, [])
    assert get_collector("claude-code") is ClaudeCodeCollector


def test_get_collector_raises_keyerror_for_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_entry_points(monkeypatch, [])
    with pytest.raises(KeyError, match="unknown collector"):
        get_collector("does_not_exist")


def test_entry_point_discovery_uses_tokie_collectors_group() -> None:
    assert registry_mod.ENTRY_POINT_GROUP == "tokie.collectors"
    # Real calls must return an iterable of ``EntryPoint``s — this is a
    # smoke test that the lookup doesn't crash in a fresh venv.
    eps = tuple(registry_mod._iter_entry_points())
    assert all(isinstance(ep, EntryPoint) for ep in eps)
