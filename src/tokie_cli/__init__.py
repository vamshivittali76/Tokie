"""Tokie — local-first AI usage control plane.

Distributed on PyPI as ``tokie-cli``; imported as ``tokie_cli``; the shipped
console command is still ``tokie``. See IMPLEMENTATION_PLAN.md for the roadmap.

The package version is resolved from installed package metadata so it
stays in lockstep with ``pyproject.toml`` without a second source of
truth. The fallback string is only hit when Tokie is imported from a
source tree that was never installed.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tokie-cli")
except PackageNotFoundError:  # pragma: no cover - only hit in un-installed source trees
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
