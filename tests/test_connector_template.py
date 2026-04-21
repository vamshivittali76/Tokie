"""Smoke test that the bundled connector template still passes the contract.

If this test fails, the template under ``templates/tokie-connector-example/``
has drifted away from the public :class:`Collector` API and every
third-party connector author who started from that template is now
looking at a broken example. Loud failure here is the whole point.

The template ships as plain source (not a package), so we load the
collector module by file path using :mod:`importlib.util` instead of
importing the package normally.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from tokie_cli.testing import (
    assert_collector_contract,
    assert_scan_yields_valid_events,
)

TEMPLATE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "templates"
    / "tokie-connector-example"
    / "src"
    / "acme_connector"
)


def _load_collector_class() -> type:
    """Import ``AcmeCollector`` directly from the template source tree.

    We bypass ``importlib.import_module`` because the template dir is
    not a package on ``sys.path``; loading the file explicitly keeps
    the test hermetic and immune to working-directory quirks.
    """

    spec = importlib.util.spec_from_file_location(
        "acme_connector_template.collector",
        TEMPLATE_ROOT / "collector.py",
    )
    assert spec is not None and spec.loader is not None, "template layout changed"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module.AcmeCollector  # type: ignore[no-any-return]


def test_template_collector_passes_structural_contract() -> None:
    cls = _load_collector_class()
    assert_collector_contract(cls)


@pytest.mark.asyncio
async def test_template_collector_emits_valid_event() -> None:
    cls = _load_collector_class()
    events = await assert_scan_yields_valid_events(cls(), min_events=1)
    assert events[0].provider == "acme"
