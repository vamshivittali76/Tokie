"""FastAPI application for the Tokie localhost dashboard.

Binds ``127.0.0.1:7878`` by default. JSON API endpoints live under ``/api``;
the single HTMX-rendered index page lives at ``/``. Static assets are
served from ``static/`` and templates from ``templates/``; both are
resolved through :mod:`importlib.resources` so the server works the same
whether Tokie is run from a wheel install or an editable checkout.

Design decisions
----------------
- **Never rebind off loopback implicitly.** The :func:`run` helper refuses
  ``host != "127.0.0.1"`` unless ``allow_remote=True``; the CLI surfaces
  this behind an explicit ``--remote`` flag with a visible warning.
- **No request logging of event bodies.** Only status codes and durations.
  Prompt content never leaves the DB layer and the dashboard does not re-
  emit it.
- **Dependency injection for DB + plans + now().** Tests override via
  :meth:`FastAPI.dependency_overrides`; production wires them to real
  state at app-construction time.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from tokie_cli import __version__
from tokie_cli.config import (
    ThresholdRuleConfig,
    TokieConfig,
    default_config_path,
    load_config,
    save_config,
)
from tokie_cli.dashboard.aggregator import DashboardPayload, build_payload
from tokie_cli.db import connect, migrate, query_events
from tokie_cli.plans import PlanTemplate, load_plans
from tokie_cli.schema import UsageEvent

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7878


@dataclass(frozen=True)
class AppState:
    """Injectable state the routes consume via FastAPI dependencies.

    Kept intentionally small: a config object, a plans-loader callable, an
    event-loader callable, and a now()-provider. Tests swap any of these;
    production wires them to :mod:`tokie_cli.db` and :mod:`tokie_cli.plans`.
    """

    config: TokieConfig
    plans_loader: Callable[[], Sequence[PlanTemplate]]
    events_loader: Callable[[TokieConfig], Sequence[UsageEvent]]
    now: Callable[[], datetime]


def default_events_loader(config: TokieConfig) -> list[UsageEvent]:
    """Read the entire event table from disk.

    Dashboard traffic is single-user and low-QPS, so reading the whole
    table for every request is the simplest correct option. If this ever
    becomes a bottleneck the right fix is a windowed query plus
    streaming, not an in-process cache.
    """

    if not config.db_path.exists():
        return []
    conn = connect(config.db_path)
    try:
        migrate(conn)
        return list(query_events(conn))
    finally:
        conn.close()


def default_now() -> datetime:
    return datetime.now(tz=UTC)


def _templates_dir() -> Path:
    return Path(str(files("tokie_cli.dashboard").joinpath("templates")))


def _static_dir() -> Path:
    return Path(str(files("tokie_cli.dashboard").joinpath("static")))


def create_app(
    state: AppState | None = None,
    *,
    config: TokieConfig | None = None,
) -> FastAPI:
    """Construct the FastAPI app, wiring routes and dependency-injection hooks.

    ``state`` is the preferred override path for tests. ``config`` is a
    shortcut for "use real loaders, just swap the config object".
    """

    if state is None:
        resolved_config = config if config is not None else load_config()
        state = AppState(
            config=resolved_config,
            plans_loader=lambda: load_plans(),
            events_loader=default_events_loader,
            now=default_now,
        )

    app = FastAPI(
        title="Tokie",
        version=__version__,
        description="Local-first AI usage & quota dashboard.",
    )
    app.state.tokie = state

    templates_dir = _templates_dir()
    static_dir = _static_dir()
    templates = Jinja2Templates(directory=str(templates_dir))
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    def get_state(request: Request) -> AppState:
        resolved: AppState = request.app.state.tokie
        return resolved

    @app.get("/api/health", response_class=JSONResponse)
    def health(state: AppState = Depends(get_state)) -> dict[str, Any]:
        """Cheap liveness + db-presence probe."""

        db_exists = state.config.db_path.exists()
        return {
            "ok": True,
            "version": __version__,
            "db_present": db_exists,
            "db_path": str(state.config.db_path),
        }

    @app.get("/api/status", response_class=JSONResponse)
    def status(state: AppState = Depends(get_state)) -> Any:
        """Top-level payload consumed by the HTMX index page."""

        payload = _build(state)
        return _to_jsonable(payload)

    @app.get("/api/subscriptions", response_class=JSONResponse)
    def subscriptions(state: AppState = Depends(get_state)) -> dict[str, Any]:
        payload = _build(state)
        return {"subscriptions": _to_jsonable(payload.subscriptions)}

    @app.get("/api/events", response_class=JSONResponse)
    def recent_events(state: AppState = Depends(get_state)) -> dict[str, Any]:
        payload = _build(state)
        return {"events": _to_jsonable(payload.recent_events)}

    @app.get("/api/daily", response_class=JSONResponse)
    def daily(state: AppState = Depends(get_state)) -> dict[str, Any]:
        payload = _build(state)
        return {"bars": _to_jsonable(payload.daily_bars)}

    @app.get("/api/timeline", response_class=JSONResponse)
    def timeline(state: AppState = Depends(get_state)) -> dict[str, Any]:
        payload = _build(state)
        return {"timeline": _to_jsonable(payload.hourly_timeline)}

    @app.get("/api/burn-rate", response_class=JSONResponse)
    def burn_rate(state: AppState = Depends(get_state)) -> dict[str, Any]:
        payload = _build(state)
        return {"burn_rate": _to_jsonable(payload.burn_rate)}

    @app.get("/api/accounts", response_class=JSONResponse)
    def accounts(state: AppState = Depends(get_state)) -> dict[str, Any]:
        payload = _build(state)
        return {"accounts": list(payload.accounts)}

    @app.get("/api/routing", response_class=JSONResponse)
    def routing_catalog(state: AppState = Depends(get_state)) -> dict[str, Any]:
        """Return the full task catalog from ``task_routing.yaml``.

        Imported lazily so any YAML error surfaces as a 500 with a
        readable message instead of blocking dashboard startup.
        """

        from tokie_cli.routing import (
            load_routing_table,
        )

        try:
            table = load_routing_table()
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"failed to load routing table: {exc}"
            ) from exc
        _ = state  # state is unused today but keeps the dependency hook warm
        return {
            "version": table.version,
            "updated": table.updated,
            "tools": [
                {
                    "id": t.id,
                    "display_name": t.display_name,
                    "products": list(t.products),
                    "notes": t.notes,
                }
                for t in table.tools
            ],
            "tasks": [
                {
                    "id": t.id,
                    "description": t.description,
                    "preferred": [
                        {
                            "tool": p.tool_id,
                            "tier": p.tier,
                            "rationale": p.rationale,
                        }
                        for p in t.preferred
                    ],
                }
                for t in table.tasks
            ],
        }

    @app.get("/api/recommend", response_class=JSONResponse)
    def recommend_endpoint(
        task: str, state: AppState = Depends(get_state)
    ) -> dict[str, Any]:
        """Return a ranked :func:`recommend` result for ``task``.

        Example: ``GET /api/recommend?task=code_generation``.
        """

        from tokie_cli.routing import (
            load_routing_table,
            recommend,
            suggest_alternatives,
        )

        try:
            table = load_routing_table()
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"failed to load routing table: {exc}"
            ) from exc
        try:
            table.task(task)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        payload = _build(state)
        result = recommend(
            task_id=task, table=table, subscriptions=payload.subscriptions
        )

        # Also surface auto-handoff style suggestions when any subscription
        # is currently over its limit — useful in the UI to show "try X".
        from tokie_cli.alerts.thresholds import (
            ThresholdRule,
            evaluate_thresholds,
        )

        rules = [
            ThresholdRule(
                plan_id=r.plan_id,
                account_id=r.account_id,
                levels=tuple(r.levels),
                channels=tuple(r.channels),
            )
            for r in state.config.thresholds
        ]
        armed = evaluate_thresholds(payload.subscriptions, rules)
        suggestions = suggest_alternatives(
            crossings=armed,
            subscriptions=payload.subscriptions,
            table=table,
            fallback_task=task,
        )

        return {
            "task_id": result.task_id,
            "description": result.task_description,
            "recommendations": [
                {
                    "tool_id": r.tool_id,
                    "tool_display_name": r.tool_display_name,
                    "plan_id": r.plan_id,
                    "plan_display_name": r.plan_display_name,
                    "account_id": r.account_id,
                    "product": r.product,
                    "tier": r.tier,
                    "rationale": r.rationale,
                    "saturation": r.saturation,
                    "remaining_fraction": r.remaining_fraction,
                    "worst_window_type": r.worst_window_type,
                    "is_over": r.is_over,
                }
                for r in result.recommendations
            ],
            "missing_tools": list(result.missing_tools),
            "handoff_suggestions": [
                {
                    "saturated_plan_id": s.saturated_plan_id,
                    "saturated_display_name": s.saturated_display_name,
                    "threshold_pct": s.threshold_pct,
                    "alternative": (
                        None
                        if s.alternative is None
                        else {
                            "tool_id": s.alternative.tool_id,
                            "tool_display_name": s.alternative.tool_display_name,
                            "plan_id": s.alternative.plan_id,
                            "account_id": s.alternative.account_id,
                            "tier": s.alternative.tier,
                            "rationale": s.alternative.rationale,
                        }
                    ),
                    "reason": s.reason,
                }
                for s in suggestions
            ],
        }

    @app.get("/api/alerts", response_class=JSONResponse)
    def alerts_status(state: AppState = Depends(get_state)) -> dict[str, Any]:
        """Live evaluation of threshold rules (no dispatch, no DB write).

        Uses the same ``events_loader`` injection path as :func:`_build`, so
        the dashboard's test harness can drive alerts without touching a real
        SQLite file.
        """

        from tokie_cli.alerts.channels import render_banner
        from tokie_cli.alerts.thresholds import (
            ThresholdRule,
            evaluate_thresholds,
        )

        payload = _build(state)
        rules = [
            ThresholdRule(
                plan_id=r.plan_id,
                account_id=r.account_id,
                levels=tuple(r.levels),
                channels=tuple(r.channels),
            )
            for r in state.config.thresholds
        ]
        armed = evaluate_thresholds(payload.subscriptions, rules)
        banner = render_banner(armed)
        return {
            "ran_at": state.now().isoformat(),
            "armed": [
                {
                    "plan_id": c.plan_id,
                    "account_id": c.account_id,
                    "display_name": c.display_name,
                    "window_type": c.window_type,
                    "window_starts_at": c.window_starts_at_iso,
                    "window_resets_at": c.window_resets_at_iso,
                    "threshold_pct": c.threshold_pct,
                    "pct_used": c.pct_used,
                    "used": c.used,
                    "limit": c.limit,
                    "remaining": c.remaining,
                    "severity": c.severity(),
                    "channels": list(c.channels),
                }
                for c in armed
            ],
            "banner": [
                {"text": line.text, "severity": line.severity} for line in banner
            ],
        }

    @app.get("/api/thresholds", response_class=JSONResponse)
    def get_thresholds(state: AppState = Depends(get_state)) -> dict[str, Any]:
        """Return the currently-configured threshold rules."""

        return {
            "thresholds": [
                {
                    "plan_id": rule.plan_id,
                    "account_id": rule.account_id,
                    "levels": list(rule.levels),
                    "channels": list(rule.channels),
                }
                for rule in state.config.thresholds
            ],
            "available_channels": _enumerate_channels(state.config),
        }

    @app.post("/api/thresholds", response_class=JSONResponse)
    def post_thresholds(
        payload: dict[str, Any] = Body(...),
        state: AppState = Depends(get_state),
    ) -> dict[str, Any]:
        """Replace the threshold rule list and persist to ``tokie.toml``.

        Refused when the dashboard is bound non-loopback; editing config over
        the network is a foot-gun the CLI's ``--remote`` flag doesn't cover.
        """

        host = state.config.dashboard_host
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise HTTPException(
                status_code=403,
                detail="threshold edits are loopback-only",
            )

        rules_raw = payload.get("thresholds")
        if not isinstance(rules_raw, list):
            raise HTTPException(status_code=400, detail="'thresholds' must be a list")

        parsed: list[ThresholdRuleConfig] = []
        for i, entry in enumerate(rules_raw):
            if not isinstance(entry, dict):
                raise HTTPException(
                    status_code=400, detail=f"thresholds[{i}] must be an object"
                )
            plan_id = entry.get("plan_id") or None
            account_id = entry.get("account_id") or None
            levels_raw = entry.get("levels", [75, 95, 100])
            channels_raw = entry.get("channels", ["banner"])
            if not isinstance(levels_raw, list) or not all(
                isinstance(x, int) and not isinstance(x, bool) for x in levels_raw
            ):
                raise HTTPException(
                    status_code=400,
                    detail=f"thresholds[{i}].levels must be a list of integers",
                )
            if not isinstance(channels_raw, list) or not all(
                isinstance(x, str) for x in channels_raw
            ):
                raise HTTPException(
                    status_code=400,
                    detail=f"thresholds[{i}].channels must be a list of strings",
                )
            parsed.append(
                ThresholdRuleConfig(
                    plan_id=plan_id if plan_id else None,
                    account_id=account_id if account_id else None,
                    levels=tuple(int(v) for v in levels_raw),
                    channels=tuple(str(v) for v in channels_raw),
                )
            )

        # The dashboard is a single-process app; we mutate the live state's
        # config in-place (frozen dataclass → rebuild via constructor) so the
        # very next request sees the new rules without needing a reload.
        new_config = TokieConfig(
            db_path=state.config.db_path,
            audit_log_path=state.config.audit_log_path,
            dashboard_host=state.config.dashboard_host,
            dashboard_port=state.config.dashboard_port,
            collectors=state.config.collectors,
            subscriptions=state.config.subscriptions,
            thresholds=tuple(parsed),
            webhooks=state.config.webhooks,
            alerts_desktop_enabled=state.config.alerts_desktop_enabled,
        )
        save_config(new_config, default_config_path())
        new_state = AppState(
            config=new_config,
            plans_loader=state.plans_loader,
            events_loader=state.events_loader,
            now=state.now,
        )
        request_app: FastAPI = app
        request_app.state.tokie = new_state
        return {
            "thresholds": [
                {
                    "plan_id": r.plan_id,
                    "account_id": r.account_id,
                    "levels": list(r.levels),
                    "channels": list(r.channels),
                }
                for r in new_config.thresholds
            ]
        }

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, state: AppState = Depends(get_state)) -> HTMLResponse:
        """Single-page dashboard. Client JS fetches /api/status on mount."""

        payload = _build(state)
        jsonable = _to_jsonable(payload)
        context: dict[str, Any] = {
            "request": request,
            "version": __version__,
            "bind": f"{state.config.dashboard_host}:{state.config.dashboard_port}",
            "payload": payload,
            "payload_json": json.dumps(jsonable),
            "has_events": payload.event_count > 0,
            "has_subscriptions": payload.subscription_count > 0,
        }
        return templates.TemplateResponse(request, "index.html", context)

    return app


def _build(state: AppState) -> DashboardPayload:
    events = list(state.events_loader(state.config))
    try:
        plans = list(state.plans_loader())
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("plans failed to load: %s", type(exc).__name__)
        plans = []
    return build_payload(
        state.config.subscriptions,
        plans,
        events,
        now=state.now(),
    )


def _enumerate_channels(config: TokieConfig) -> list[str]:
    """Return a UI-friendly list of channel names the operator can opt-in to."""

    out: list[str] = ["banner"]
    if config.alerts_desktop_enabled:
        out.append("desktop")
    for webhook in config.webhooks:
        out.append(f"webhook:{webhook.name}")
    return out


def _to_jsonable(value: Any) -> Any:
    """Recursively convert dataclasses / datetimes / tuples into JSON-ready data."""

    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _to_jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, tuple | list):
        return [_to_jsonable(v) for v in value]
    return value


def run(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    allow_remote: bool = False,
    config: TokieConfig | None = None,
) -> None:
    """Start ``uvicorn`` synchronously until the user stops it.

    ``allow_remote`` must be explicitly set for non-loopback binds. This is
    the programmatic counterpart of the CLI's ``--remote`` flag.
    """

    import uvicorn

    if host not in {"127.0.0.1", "localhost", "::1"} and not allow_remote:
        raise RuntimeError(
            f"refusing to bind {host!r} without allow_remote=True; "
            f"pass tokie dashboard --remote to confirm non-loopback bind."
        )

    app = create_app(config=config)
    uvicorn.run(app, host=host, port=port, log_level="warning")


def iter_sqlite_events(db_path: Path) -> Iterator[UsageEvent]:  # pragma: no cover
    """Streaming alternative to :func:`default_events_loader` for future use.

    Kept as a thin wrapper so a future paginated dashboard can swap in a
    streaming loader without changing the ``AppState`` contract.
    """

    conn: sqlite3.Connection = connect(db_path)
    try:
        migrate(conn)
        yield from query_events(conn)
    finally:
        conn.close()


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "AppState",
    "create_app",
    "default_events_loader",
    "default_now",
    "run",
]
