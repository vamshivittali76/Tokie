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

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from tokie_cli import __version__
from tokie_cli.config import TokieConfig, load_config
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
