"""Collector namespace for Tokie."""

from __future__ import annotations

from tokie_cli.collectors.base import (
    Collector,
    CollectorError,
    CollectorHealth,
    aiterate,
)

__all__ = [
    "Collector",
    "CollectorError",
    "CollectorHealth",
    "aiterate",
]
