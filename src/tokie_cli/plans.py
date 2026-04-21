"""Bundled subscription plan templates.

This module loads the curated ``plans.yaml`` shipped inside the wheel and
exposes it as validated :class:`PlanTemplate` records. Each template wraps a
:class:`~tokie_cli.schema.Subscription` so downstream code can trust the
frozen schema contract.

Source: section 11.1 of TOKIE_DEVELOPMENT_PLAN_FINAL.md — "plans.yaml update
strategy": community PRs keep the file fresh, every entry carries a
``source_url`` citation, and Tokie reads the bundled copy at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from tokie_cli.schema import Subscription

__all__ = [
    "DEFAULT_PLANS_FILENAME",
    "PlanTemplate",
    "PlansFileError",
    "Trackability",
    "bundled_plans_path",
    "get_plan",
    "load_plans",
]


class Trackability(StrEnum):
    """How Tokie can observe usage for this plan.

    - LOCAL_EXACT: data comes from the vendor's own local logs (e.g. Claude Code JSONL).
    - API_EXACT: data comes from a vendor admin-usage endpoint (e.g. Anthropic usage report).
    - WEB_ONLY_MANUAL: no local signal exists; user enters usage via the manual collector.
    """

    LOCAL_EXACT = "local_exact"
    API_EXACT = "api_exact"
    WEB_ONLY_MANUAL = "web_only_manual"


DEFAULT_PLANS_FILENAME: str = "plans.yaml"


class PlansFileError(Exception):
    """Raised when ``plans.yaml`` is missing, malformed, or schema-invalid."""


@dataclass(frozen=True)
class PlanTemplate:
    """A curated subscription template that users can adopt by id.

    ``subscription`` is already validated against
    :class:`tokie_cli.schema.Subscription`, so consumers can treat it as a
    trusted source of truth without re-validating.
    """

    id: str
    display_name: str
    source_url: str
    notes: str | None
    subscription: Subscription
    trackability: Trackability = Trackability.LOCAL_EXACT


def bundled_plans_path() -> Path:
    """Return the filesystem path to the bundled ``plans.yaml``.

    Resolves through :mod:`importlib.resources` so the file is found whether
    Tokie is installed as a wheel or used via an editable install during
    development.
    """

    resource = files("tokie_cli").joinpath(DEFAULT_PLANS_FILENAME)
    return Path(str(resource))


def load_plans(path: Path | str | None = None) -> list[PlanTemplate]:
    """Load and validate plan templates from a YAML file.

    When ``path`` is ``None`` the bundled ``plans.yaml`` is used. The file is
    parsed with :func:`yaml.safe_load` — never ``yaml.load`` — and every entry
    is validated against the frozen :class:`Subscription` schema.

    Raises:
        PlansFileError: If the file cannot be read, YAML cannot be parsed,
            the top-level shape is wrong, or any individual plan fails
            schema validation. The offending plan id is included in the
            message when relevant.
    """

    resolved = Path(path) if path is not None else bundled_plans_path()

    try:
        raw_text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlansFileError(f"Could not read plans file at {resolved}: {exc}") from exc

    try:
        parsed: Any = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise PlansFileError(f"Invalid YAML in plans file {resolved}: {exc}") from exc

    if not isinstance(parsed, dict):
        raise PlansFileError(f"Plans file {resolved} must contain a YAML mapping at the top level.")

    version = parsed.get("version")
    if not isinstance(version, int):
        raise PlansFileError(f"Plans file {resolved} is missing an integer 'version' field.")

    updated = parsed.get("updated")
    if not isinstance(updated, str):
        # PyYAML parses ``YYYY-MM-DD`` as ``datetime.date``; coerce to str so
        # the shape is predictable regardless of quoting style in the file.
        if updated is None:
            raise PlansFileError(f"Plans file {resolved} is missing an 'updated' field.")
        updated = str(updated)

    plans_raw = parsed.get("plans")
    if not isinstance(plans_raw, list):
        raise PlansFileError(f"Plans file {resolved} must contain a 'plans' list.")

    templates: list[PlanTemplate] = []
    for index, entry in enumerate(plans_raw):
        if not isinstance(entry, dict):
            raise PlansFileError(f"Plan entry at index {index} in {resolved} is not a mapping.")

        plan_id_obj = entry.get("id")
        plan_id = plan_id_obj if isinstance(plan_id_obj, str) else f"<index {index}>"

        required = ("id", "display_name", "source_url", "subscription")
        missing = [key for key in required if key not in entry]
        if missing:
            raise PlansFileError(
                f"Plan '{plan_id}' is missing required field(s): {', '.join(missing)}."
            )

        display_name = entry["display_name"]
        source_url = entry["source_url"]
        if (
            not isinstance(plan_id_obj, str)
            or not isinstance(display_name, str)
            or not isinstance(source_url, str)
        ):
            raise PlansFileError(f"Plan '{plan_id}' has non-string id/display_name/source_url.")

        notes_value = entry.get("notes")
        if notes_value is not None and not isinstance(notes_value, str):
            raise PlansFileError(f"Plan '{plan_id}' has a non-string 'notes' field.")

        trackability_raw = entry.get("trackability")
        if trackability_raw is None:
            trackability = Trackability.LOCAL_EXACT
        else:
            if not isinstance(trackability_raw, str):
                raise PlansFileError(f"Plan '{plan_id}' has a non-string 'trackability' field.")
            try:
                trackability = Trackability(trackability_raw)
            except ValueError as exc:
                valid = ", ".join(t.value for t in Trackability)
                raise PlansFileError(
                    f"Plan '{plan_id}' has an unknown trackability "
                    f"{trackability_raw!r}. Valid values: {valid}."
                ) from exc

        try:
            subscription = Subscription.model_validate(entry["subscription"])
        except ValidationError as exc:
            raise PlansFileError(f"Plan '{plan_id}' has an invalid subscription: {exc}") from exc

        templates.append(
            PlanTemplate(
                id=plan_id_obj,
                display_name=display_name,
                source_url=source_url,
                notes=notes_value,
                subscription=subscription,
                trackability=trackability,
            )
        )

    return templates


def get_plan(plans: list[PlanTemplate], plan_id: str) -> PlanTemplate:
    """Return the template with the given ``plan_id``.

    Raises:
        KeyError: If no plan matches. The error message lists every available
            id so the caller (or user) can spot typos quickly.
    """

    for template in plans:
        if template.id == plan_id:
            return template

    available = ", ".join(sorted(t.id for t in plans)) or "<none>"
    raise KeyError(f"No plan with id {plan_id!r}. Available plans: {available}.")
