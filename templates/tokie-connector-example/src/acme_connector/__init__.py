"""Example third-party Tokie connector.

Rename this package (and everything in ``pyproject.toml``) before you
ship your real connector. See ``README.md`` at the template root for
the checklist.
"""

from __future__ import annotations

from acme_connector.collector import AcmeCollector

__all__ = ["AcmeCollector"]
