"""Tests for the bundled plans loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from tokie_cli.plans import (
    PlansFileError,
    PlanTemplate,
    bundled_plans_path,
    get_plan,
    load_plans,
)
from tokie_cli.schema import Subscription, WindowType

MINIMAL_VALID_YAML = """
version: 1
updated: "2026-04-20"
plans:
  - id: test_plan
    display_name: Test Plan
    source_url: https://example.com/pricing
    notes: A tiny fixture.
    subscription:
      id: test_plan
      provider: example
      product: example-api
      plan: free
      account_id: default
      windows:
        - window_type: monthly
          limit_usd: 10.0
"""


INVALID_SUBSCRIPTION_YAML = """
version: 1
updated: "2026-04-20"
plans:
  - id: broken_plan
    display_name: Broken Plan
    source_url: https://example.com
    subscription:
      id: broken_plan
      provider: example
      product: example-api
      plan: free
      account_id: default
      windows:
        - window_type: fortnightly
"""


def test_bundled_plans_path_exists() -> None:
    path = bundled_plans_path()
    assert path.exists(), f"bundled plans.yaml missing at {path}"
    assert path.name == "plans.yaml"


def test_load_bundled_plans_succeeds() -> None:
    plans = load_plans()
    assert isinstance(plans, list)
    assert len(plans) > 0
    assert all(isinstance(p, PlanTemplate) for p in plans)


def test_every_plan_has_source_url() -> None:
    plans = load_plans()
    for template in plans:
        assert template.source_url, f"{template.id} missing source_url"
        assert template.source_url.startswith("http"), (
            f"{template.id} has non-URL source_url: {template.source_url!r}"
        )


def test_every_plan_id_is_unique() -> None:
    plans = load_plans()
    ids = [p.id for p in plans]
    assert len(ids) == len(set(ids)), f"duplicate plan ids: {ids}"


def test_every_plan_validates_as_subscription() -> None:
    plans = load_plans()
    for template in plans:
        assert isinstance(template.subscription, Subscription)
        roundtrip = Subscription.model_validate(template.subscription.model_dump())
        assert roundtrip == template.subscription


def test_claude_pro_has_two_shared_windows() -> None:
    plans = load_plans()
    claude_pro = get_plan(plans, "claude_pro_personal")
    window_types = {w.window_type for w in claude_pro.subscription.windows}
    assert window_types == {WindowType.ROLLING_5H, WindowType.WEEKLY}
    for window in claude_pro.subscription.windows:
        assert "claude-web" in window.shared_with
        assert "claude-code" in window.shared_with


def test_anthropic_api_direct_has_none_window() -> None:
    plans = load_plans()
    api_direct = get_plan(plans, "anthropic_api_direct")
    assert len(api_direct.subscription.windows) == 1
    assert api_direct.subscription.windows[0].window_type is WindowType.NONE


def test_openai_tier1_has_monthly_usd_limit() -> None:
    plans = load_plans()
    tier1 = get_plan(plans, "openai_tier1")
    assert len(tier1.subscription.windows) == 1
    window = tier1.subscription.windows[0]
    assert window.window_type is WindowType.MONTHLY
    assert window.limit_usd is not None
    assert window.limit_usd > 0


def test_load_plans_from_explicit_path(tmp_path: Path) -> None:
    plans_file = tmp_path / "custom_plans.yaml"
    plans_file.write_text(MINIMAL_VALID_YAML, encoding="utf-8")

    plans = load_plans(plans_file)
    assert len(plans) == 1
    assert plans[0].id == "test_plan"
    assert plans[0].subscription.provider == "example"


def test_load_plans_raises_on_bad_yaml(tmp_path: Path) -> None:
    plans_file = tmp_path / "bad.yaml"
    plans_file.write_text("version: 1\nplans: [unterminated", encoding="utf-8")

    with pytest.raises(PlansFileError):
        load_plans(plans_file)


def test_load_plans_raises_on_invalid_subscription(tmp_path: Path) -> None:
    plans_file = tmp_path / "bad_sub.yaml"
    plans_file.write_text(INVALID_SUBSCRIPTION_YAML, encoding="utf-8")

    with pytest.raises(PlansFileError) as excinfo:
        load_plans(plans_file)

    assert "broken_plan" in str(excinfo.value)


def test_get_plan_returns_matching_template() -> None:
    plans = load_plans()
    template = get_plan(plans, "cursor_pro_personal")
    assert template.id == "cursor_pro_personal"
    assert template.subscription.provider == "cursor"


def test_get_plan_raises_on_unknown_id_with_available_ids() -> None:
    plans = load_plans()
    with pytest.raises(KeyError) as excinfo:
        get_plan(plans, "does_not_exist")

    message = str(excinfo.value)
    assert "does_not_exist" in message
    assert "claude_pro_personal" in message
