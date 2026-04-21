"""Tests for :func:`tokie_cli.plans.load_plans_metadata`.

Full ``plans.yaml`` validation already has coverage in ``test_plans.py``.
This file pins down the freshness-signal contract: what counts as
"stale", how the date field is coerced across the three shapes PyYAML
can emit, and how malformed headers surface as ``PlansFileError``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tokie_cli.plans import (
    PLANS_FRESHNESS_WARN_DAYS,
    PlansFileError,
    load_plans_metadata,
)


def _write_plans(
    tmp_path: Path,
    *,
    version: object = 1,
    updated: object = "2026-04-01",
    plans: object | None = None,
) -> Path:
    lines = [f"version: {version!r}", f"updated: {updated!r}"]
    if plans is None:
        lines.append("plans: []")
    else:
        lines.append(f"plans: {plans!r}")
    path = tmp_path / "plans.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_metadata_reports_age_for_string_date(tmp_path: Path) -> None:
    path = _write_plans(tmp_path, updated="2026-03-01")
    now = datetime(2026, 4, 21, tzinfo=UTC)

    meta = load_plans_metadata(path, now=now)

    assert meta.version == 1
    assert meta.updated == date(2026, 3, 1)
    assert meta.age_days == 51
    assert meta.path == path
    assert meta.plan_count == 0
    assert meta.is_stale is False


def test_metadata_is_stale_when_age_exceeds_threshold(tmp_path: Path) -> None:
    path = _write_plans(tmp_path, updated="2025-12-01")
    now = datetime(2026, 4, 21, tzinfo=UTC)

    meta = load_plans_metadata(path, now=now, warn_days=30)

    assert meta.age_days > 30
    assert meta.is_stale is True


def test_metadata_default_warn_days_matches_exported_constant(
    tmp_path: Path,
) -> None:
    # We pass ``warn_days`` explicitly elsewhere; this guards the
    # exported default so the CLI banner and ``load_plans_metadata``
    # can never silently disagree.
    path = _write_plans(tmp_path, updated="2026-04-01")
    now = datetime(2026, 4, 21, tzinfo=UTC)

    default_meta = load_plans_metadata(path, now=now)
    explicit_meta = load_plans_metadata(
        path, now=now, warn_days=PLANS_FRESHNESS_WARN_DAYS
    )

    assert default_meta.is_stale == explicit_meta.is_stale


def test_metadata_accepts_unquoted_date(tmp_path: Path) -> None:
    # PyYAML parses ``2026-03-01`` (unquoted) as ``datetime.date``; make
    # sure we handle that branch without forcing contributors to quote
    # every date in plans.yaml.
    path = tmp_path / "plans.yaml"
    path.write_text(
        "version: 1\nupdated: 2026-03-01\nplans: []\n", encoding="utf-8"
    )
    now = datetime(2026, 4, 21, tzinfo=UTC)

    meta = load_plans_metadata(path, now=now)

    assert meta.updated == date(2026, 3, 1)
    assert meta.age_days == 51


def test_metadata_rejects_missing_updated(tmp_path: Path) -> None:
    path = tmp_path / "plans.yaml"
    path.write_text("version: 1\nplans: []\n", encoding="utf-8")

    with pytest.raises(PlansFileError, match="'updated'"):
        load_plans_metadata(path)


def test_metadata_rejects_unparseable_updated(tmp_path: Path) -> None:
    path = _write_plans(tmp_path, updated="not-a-date")

    with pytest.raises(PlansFileError, match="unparseable"):
        load_plans_metadata(path)


def test_metadata_plan_count_reflects_list_length(tmp_path: Path) -> None:
    path = tmp_path / "plans.yaml"
    path.write_text(
        "version: 1\nupdated: 2026-04-01\nplans:\n  - {}\n  - {}\n  - {}\n",
        encoding="utf-8",
    )

    meta = load_plans_metadata(path, now=datetime(2026, 4, 21, tzinfo=UTC))

    assert meta.plan_count == 3


def test_metadata_clamps_future_updated_to_zero_age(tmp_path: Path) -> None:
    # A clock-skew-only repro: system clock before the plans' update
    # date. We don't care about the exact number, only that we don't
    # flip to a negative age or accidentally flag the file as stale.
    path = _write_plans(tmp_path, updated="2030-01-01")
    now = datetime(2026, 4, 21, tzinfo=UTC)

    meta = load_plans_metadata(path, now=now)

    assert meta.age_days == 0
    assert meta.is_stale is False
