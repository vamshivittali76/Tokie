"""Project-wide pytest fixtures.

Test isolation
--------------
Some dashboard endpoints call :func:`tokie_cli.config.save_config` with no
explicit path, which falls back to ``default_config_path()`` — i.e. the real
user's ``~/.config/tokie/tokie.toml`` (or ``%LOCALAPPDATA%\\tokie\\tokie.toml``
on Windows). Without a guard, a developer running ``pytest`` locally would see
their personal config silently overwritten.

This autouse fixture redirects ``TOKIE_CONFIG_HOME`` and ``TOKIE_DATA_HOME``
to a pytest-managed temporary directory for **every** test, so any accidental
write stays inside the ephemeral sandbox. Tests that need a specific isolated
dir can still override these env vars themselves; the autouse fixture only
supplies a safe default.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_tokie_home(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Point Tokie's config/data dirs at a per-test tmp directory.

    Returns the root of the isolated sandbox in case a test wants to assert
    on files written under it.
    """

    sandbox = tmp_path_factory.mktemp("tokie-home")
    monkeypatch.setenv("TOKIE_CONFIG_HOME", str(sandbox / "config"))
    monkeypatch.setenv("TOKIE_DATA_HOME", str(sandbox / "data"))
    return sandbox
