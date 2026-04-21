"""Collector namespace for Tokie."""

from __future__ import annotations

from tokie_cli.collectors.base import (
    Collector,
    CollectorError,
    CollectorHealth,
    aiterate,
)
from tokie_cli.collectors.registry import (
    ENTRY_POINT_GROUP,
    CollectorRegistrationError,
    discover_third_party,
    get_collector,
    load_registry,
)

__all__ = [
    "ENTRY_POINT_GROUP",
    "Collector",
    "CollectorError",
    "CollectorHealth",
    "CollectorRegistrationError",
    "aiterate",
    "discover_third_party",
    "get_collector",
    "load_registry",
]
