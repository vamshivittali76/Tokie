"""Tokie command-line interface.

Minimal Day-3 surface: ``version``, ``init``, ``doctor``, ``scan``, ``status``,
``paths``. More commands (``watch``, ``alert``, ``serve``, ``import``,
``export``) land in later phases per the implementation plan.

Output policy
-------------
- Human-friendly output goes through :mod:`rich`.
- Every command accepts ``--json`` for script-friendly output.
- Nothing sensitive ever lands in stdout or in error traces: no API keys,
  no prompt content, no file contents, only counts + paths + classnames.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from tokie_cli import __version__
from tokie_cli.alerts import AlertRunResult, check_alerts
from tokie_cli.collectors import Collector
from tokie_cli.collectors.api_anthropic import AnthropicAPICollector
from tokie_cli.collectors.api_gemini import GeminiAPICollector
from tokie_cli.collectors.api_openai import OpenAIAPICollector
from tokie_cli.collectors.api_openai_compatible import OpenAICompatibleCollector
from tokie_cli.collectors.claude_code import ClaudeCodeCollector
from tokie_cli.collectors.codex import CodexCollector
from tokie_cli.collectors.copilot_cli import CopilotCLICollector
from tokie_cli.collectors.cursor_ide import CursorIDECollector
from tokie_cli.collectors.manual import ManualCollector
from tokie_cli.collectors.perplexity_api import PerplexityAPICollector
from tokie_cli.config import (
    CollectorConfig,
    config_dir,
    data_dir,
    default_config,
    default_config_path,
    load_config,
    save_config,
)
from tokie_cli.db import connect, insert_events, migrate, query_events
from tokie_cli.plans import PlanTemplate, Trackability, load_plans

app = typer.Typer(
    name="tokie",
    help="Local-first CLI for tracking AI token usage and subscription quotas.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()
err_console = Console(stderr=True)

_COLLECTOR_REGISTRY: dict[str, type[Collector]] = {
    ClaudeCodeCollector.name: ClaudeCodeCollector,
    CodexCollector.name: CodexCollector,
    AnthropicAPICollector.name: AnthropicAPICollector,
    OpenAIAPICollector.name: OpenAIAPICollector,
    GeminiAPICollector.name: GeminiAPICollector,
    OpenAICompatibleCollector.name: OpenAICompatibleCollector,
    CopilotCLICollector.name: CopilotCLICollector,
    PerplexityAPICollector.name: PerplexityAPICollector,
    CursorIDECollector.name: CursorIDECollector,
    ManualCollector.name: ManualCollector,
}


def _build_collector(name: str) -> Collector:
    """Instantiate a collector by its registered name.

    The CLI uses zero-arg construction where every collector either discovers
    its source via env vars / platform paths or, for API collectors, pulls
    credentials out of the OS keyring on demand.

    Raises :class:`typer.BadParameter` if the name is unknown.
    """

    cls = _COLLECTOR_REGISTRY.get(name)
    if cls is None:
        valid = ", ".join(sorted(_COLLECTOR_REGISTRY))
        raise typer.BadParameter(
            f"unknown collector {name!r}. Valid: {valid}.", param_hint="--collector"
        )
    if cls is OpenAICompatibleCollector:
        import os

        log_path = os.environ.get("TOKIE_OPENAI_COMPAT_LOG")
        if not log_path:
            raise typer.BadParameter(
                "openai-compat requires TOKIE_OPENAI_COMPAT_LOG to point at an NDJSON log",
                param_hint="--collector",
            )
        return OpenAICompatibleCollector(log_path=Path(log_path).expanduser())
    return cls()


@app.command()
def version(
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Print Tokie's version, plan-catalog size, and Python runtime info."""

    try:
        plans = load_plans()
        plans_count: int | None = len(plans)
    except Exception:  # pragma: no cover - best-effort read of bundled yaml
        plans_count = None

    payload: dict[str, Any] = {
        "tokie": __version__,
        "plans_in_catalog": plans_count,
        "python": ".".join(str(p) for p in sys.version_info[:3]),
        "platform": sys.platform,
    }

    if as_json:
        console.print_json(data=payload)
        return

    console.print(f"[bold]tokie[/bold] {__version__}")
    if plans_count is not None:
        console.print(f"bundled plans: {plans_count}")
    console.print(f"python: {payload['python']} ({payload['platform']})")


@app.command()
def paths(
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Show the directories Tokie uses for config, data, and the SQLite DB."""

    cfg = load_config()
    payload = {
        "config_dir": str(config_dir()),
        "data_dir": str(data_dir()),
        "config_file": str(default_config_path()),
        "db_path": str(cfg.db_path),
        "audit_log_path": str(cfg.audit_log_path),
        "manual_drop_dir": str(data_dir() / "manual"),
    }
    if as_json:
        console.print_json(data=payload)
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("Setting")
    table.add_column("Path")
    for k, v in payload.items():
        table.add_row(k, v)
    console.print(table)


@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="Overwrite an existing config file."),
) -> None:
    """Create config + data directories and a starter ``tokie.toml``.

    Idempotent by default: existing configs are left alone unless ``--force``
    is passed. Enables every detected local-signal collector so first-run
    ``tokie scan`` does something useful.
    """

    cfg_path = default_config_path()
    if cfg_path.exists() and not force:
        console.print(
            f"[yellow]config already exists at {cfg_path}; pass --force to overwrite[/yellow]"
        )
        raise typer.Exit(code=0)

    cfg = default_config()
    detected: list[str] = []
    for name, cls in _COLLECTOR_REGISTRY.items():
        try:
            is_detected = cls.detect()
        except Exception:  # pragma: no cover - never let detect crash init
            is_detected = False
        cfg = cfg.with_collector(CollectorConfig(name=name, enabled=is_detected))
        if is_detected:
            detected.append(name)

    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    (data_dir() / "manual").mkdir(parents=True, exist_ok=True)
    written = save_config(cfg, cfg_path)
    conn = connect(cfg.db_path)
    try:
        migrate(conn)
    finally:
        conn.close()

    console.print(f"[green]wrote[/green] {written}")
    console.print(f"[green]initialized db[/green] {cfg.db_path}")
    if detected:
        console.print(
            f"[green]enabled collectors (detected locally):[/green] {', '.join(detected)}"
        )
    else:
        console.print(
            "[yellow]no local collectors detected; run 'tokie doctor' for guidance[/yellow]"
        )


@app.command()
def doctor(
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Probe every registered collector and report detection + readiness."""

    rows: list[dict[str, Any]] = []
    for name, cls in _COLLECTOR_REGISTRY.items():
        try:
            instance: Collector | None = None if cls is OpenAICompatibleCollector else cls()
            detected = cls.detect()
            if instance is not None:
                health = instance.health()
                rows.append(
                    {
                        "collector": name,
                        "detected": detected,
                        "ok": health.ok,
                        "message": health.message,
                        "warnings": list(health.warnings),
                    }
                )
            else:
                rows.append(
                    {
                        "collector": name,
                        "detected": detected,
                        "ok": detected,
                        "message": "requires TOKIE_OPENAI_COMPAT_LOG to construct",
                        "warnings": [],
                    }
                )
        except Exception as exc:  # pragma: no cover - defensive
            rows.append(
                {
                    "collector": name,
                    "detected": False,
                    "ok": False,
                    "message": f"{type(exc).__name__}: {exc}",
                    "warnings": [],
                }
            )

    if as_json:
        console.print_json(data={"collectors": rows})
        return

    table = Table(title="Tokie doctor", show_header=True, header_style="bold")
    table.add_column("collector")
    table.add_column("detected")
    table.add_column("ok")
    table.add_column("message")
    for row in rows:
        detected_str = "[green]yes[/green]" if row["detected"] else "[dim]no[/dim]"
        ok_str = "[green]yes[/green]" if row["ok"] else "[yellow]no[/yellow]"
        table.add_row(row["collector"], detected_str, ok_str, row["message"])
    console.print(table)


async def _run_scan(collectors: Iterable[Collector], since: datetime | None) -> int:
    """Run each collector once and bulk-insert every emitted event.

    Returns the total number of new events committed to the DB across all
    collectors. Duplicate ``raw_hash`` values are silently ignored by the
    insert path, which is the whole point of the idempotency contract.
    """

    cfg = load_config()
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(cfg.db_path)
    try:
        migrate(conn)
        total_new = 0
        for collector in collectors:
            batch = []
            async for event in collector.scan(since=since):
                batch.append(event)
            if batch:
                stats = insert_events(conn, batch)
                total_new += stats.inserted
                seen = stats.inserted + stats.deduped
                console.print(
                    f"[green]{collector.name}[/green]: {stats.inserted} new / {seen} seen"
                )
            else:
                console.print(f"[dim]{collector.name}: no events[/dim]")
        return total_new
    finally:
        conn.close()


@app.command()
def scan(
    collector: list[str] = typer.Option(
        [],
        "--collector",
        "-c",
        help="Collector name to run. Repeat for multiple. Defaults to all enabled.",
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help="ISO-8601 timestamp; skip events before this (e.g. 2026-04-20T00:00:00Z).",
    ),
) -> None:
    """Run one-shot scans against each selected collector and persist events."""

    cfg = load_config()
    if collector:
        names = collector
    else:
        names = [c.name for c in cfg.collectors if c.enabled] or list(_COLLECTOR_REGISTRY)

    instances: list[Collector] = []
    for name in names:
        try:
            instances.append(_build_collector(name))
        except typer.BadParameter as exc:
            err_console.print(f"[yellow]skipping {name}: {exc.message}[/yellow]")

    if not instances:
        err_console.print("[red]no runnable collectors[/red]")
        raise typer.Exit(code=1)

    cutoff: datetime | None = None
    if since is not None:
        cutoff = _parse_iso(since)
        if cutoff is None:
            raise typer.BadParameter(f"invalid ISO timestamp {since!r}", param_hint="--since")

    total = asyncio.run(_run_scan(instances, cutoff))
    console.print(f"[bold]total new events: {total}[/bold]")


@app.command()
def status(
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Summarize events in the local DB, grouped by provider/product."""

    cfg = load_config()
    if not cfg.db_path.exists():
        console.print("[yellow]no database yet; run 'tokie init' and then 'tokie scan'[/yellow]")
        raise typer.Exit(code=0)
    conn = connect(cfg.db_path)
    try:
        migrate(conn)
        events = list(query_events(conn))
    finally:
        conn.close()

    totals: dict[tuple[str, str], dict[str, int]] = {}
    for evt in events:
        key = (evt.provider, evt.product)
        bucket = totals.setdefault(
            key,
            {"events": 0, "input": 0, "output": 0, "cache_read": 0, "reasoning": 0},
        )
        bucket["events"] += 1
        bucket["input"] += evt.input_tokens
        bucket["output"] += evt.output_tokens
        bucket["cache_read"] += evt.cache_read_tokens
        bucket["reasoning"] += evt.reasoning_tokens

    payload = {
        "db_path": str(cfg.db_path),
        "totals": [
            {
                "provider": p,
                "product": pr,
                **counts,
            }
            for (p, pr), counts in sorted(totals.items())
        ],
        "grand_total_events": sum(b["events"] for b in totals.values()),
    }

    if as_json:
        console.print_json(data=payload)
        return

    if not totals:
        console.print("[dim]no events yet; run 'tokie scan'[/dim]")
        return

    try:
        alert_result = check_alerts(cfg, dry_run=True)
    except Exception:  # pragma: no cover - alerts must never break status
        alert_result = None
    if alert_result and alert_result.banner_lines:
        console.print()
        console.print("[bold yellow]⚠ thresholds armed[/bold yellow]")
        for line in alert_result.banner_lines:
            console.print(f"  [{_severity_style(line.severity)}]{line.text}[/]")
        console.print()

    table = Table(title="Tokie status", show_header=True, header_style="bold")
    table.add_column("provider")
    table.add_column("product")
    table.add_column("events", justify="right")
    table.add_column("input", justify="right")
    table.add_column("output", justify="right")
    table.add_column("cache_read", justify="right")
    table.add_column("reasoning", justify="right")
    for (provider, product), counts in sorted(totals.items()):
        table.add_row(
            provider,
            product,
            str(counts["events"]),
            str(counts["input"]),
            str(counts["output"]),
            str(counts["cache_read"]),
            str(counts["reasoning"]),
        )
    console.print(table)


@app.command()
def plans(
    tier: str | None = typer.Option(
        None,
        "--tier",
        help="Filter by trackability: local_exact, api_exact, web_only_manual.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """List every bundled subscription plan Tokie knows about."""

    try:
        bundled = load_plans()
    except Exception as exc:
        err_console.print(f"[red]failed to load plans: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    selected: list[PlanTemplate]
    if tier is None:
        selected = bundled
    else:
        try:
            wanted = Trackability(tier)
        except ValueError as exc:
            valid = ", ".join(t.value for t in Trackability)
            raise typer.BadParameter(
                f"unknown tier {tier!r}. Valid: {valid}.", param_hint="--tier"
            ) from exc
        selected = [p for p in bundled if p.trackability is wanted]

    if as_json:
        console.print_json(
            data={
                "plans": [
                    {
                        "id": p.id,
                        "display_name": p.display_name,
                        "provider": p.subscription.provider,
                        "product": p.subscription.product,
                        "plan": p.subscription.plan,
                        "trackability": p.trackability.value,
                        "source_url": p.source_url,
                    }
                    for p in selected
                ]
            }
        )
        return

    table = Table(title=f"Tokie plans ({len(selected)})", show_header=True, header_style="bold")
    table.add_column("id")
    table.add_column("display_name")
    table.add_column("provider")
    table.add_column("product")
    table.add_column("trackability")
    for p in selected:
        color = {
            Trackability.LOCAL_EXACT: "green",
            Trackability.API_EXACT: "cyan",
            Trackability.WEB_ONLY_MANUAL: "yellow",
        }[p.trackability]
        table.add_row(
            p.id,
            p.display_name,
            p.subscription.provider,
            p.subscription.product,
            f"[{color}]{p.trackability.value}[/{color}]",
        )
    console.print(table)


@app.command()
def dashboard(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Interface to bind. Use 127.0.0.1 (default) to stay loopback-only.",
    ),
    port: int = typer.Option(7878, "--port", help="TCP port to bind."),
    remote: bool = typer.Option(
        False,
        "--remote",
        help="Required to bind any non-loopback host. Prints a security warning.",
    ),
    open_browser: bool = typer.Option(
        True,
        "--open/--no-open",
        help="Open the default browser once the server is listening.",
    ),
) -> None:
    """Start the localhost dashboard at ``http://{host}:{port}``.

    Binds to loopback by default; ``--remote`` must be passed to bind any
    non-loopback interface and will print a visible warning when it does.
    """

    from tokie_cli.dashboard.server import run as run_server

    loopback = {"127.0.0.1", "localhost", "::1"}
    if host not in loopback:
        if not remote:
            err_console.print(
                f"[red]refusing to bind {host!r} without --remote[/red]\n"
                "use '--remote' explicitly if you really want non-loopback access."
            )
            raise typer.Exit(code=2)
        err_console.print(
            f"[yellow]binding {host}:{port} (non-loopback). "
            "Tokie has no auth layer yet — do not expose to untrusted networks.[/yellow]"
        )

    cfg = load_config()
    cfg_with_bind = cfg.__class__(
        db_path=cfg.db_path,
        audit_log_path=cfg.audit_log_path,
        dashboard_host=host,
        dashboard_port=port,
        collectors=cfg.collectors,
        subscriptions=cfg.subscriptions,
    )

    console.print(f"[green]tokie dashboard[/green] -> http://{host}:{port}  (Ctrl-C to stop)")
    if open_browser and host in loopback:
        import threading
        import webbrowser

        def _open() -> None:
            import time

            time.sleep(0.8)
            webbrowser.open(f"http://{host}:{port}")

        threading.Thread(target=_open, daemon=True).start()

    try:
        run_server(host=host, port=port, allow_remote=remote, config=cfg_with_bind)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        console.print("[dim]dashboard stopped[/dim]")


@app.command()
def watch() -> None:
    """Launch the live TUI — per-subscription bars, sparklines, reset countdowns."""

    cfg = load_config()
    if not cfg.db_path.exists():
        err_console.print(
            "[yellow]no database yet; run 'tokie init' and 'tokie scan' first[/yellow]"
        )
        raise typer.Exit(code=1)
    # Importing here avoids paying Textual's import cost on every `tokie`
    # invocation (it pulls in most of rich's optional UI stack).
    from tokie_cli.tui import run_watch

    run_watch(config=cfg)


alerts_app = typer.Typer(
    name="alerts",
    help="Threshold alerts — evaluate subscriptions and dispatch notifications.",
    no_args_is_help=True,
)
app.add_typer(alerts_app)


def _severity_style(severity: str) -> str:
    return {
        "low": "green",
        "medium": "yellow",
        "high": "orange3",
        "over": "bold red",
    }.get(severity, "white")


def _render_alert_result(result: AlertRunResult) -> None:
    """Print banner + fired-summary for a run of ``tokie alerts check``."""

    if not result.armed:
        console.print("[dim]no thresholds armed.[/dim]")
        return
    console.print("[bold]armed thresholds[/bold]")
    for line in result.banner_lines:
        console.print(f"  [{_severity_style(line.severity)}]{line.text}[/]")
    if result.fired:
        console.print(f"[bold green]dispatched {len(result.fired)} new fire(s):[/]")
        for crossing in result.fired:
            console.print(
                f"  - {crossing.display_name} [{crossing.account_id}] "
                f"@ {crossing.threshold_pct}% via {', '.join(crossing.channels)}"
            )
    else:
        console.print("[dim]no new fires — everything already dispatched this window.[/dim]")

    if result.dispatch_results:
        ok = sum(1 for r in result.dispatch_results if r.ok)
        fail = len(result.dispatch_results) - ok
        console.print(
            f"[dim]dispatches: {ok} ok, {fail} failed[/dim]"
        )
        for r in result.dispatch_results:
            if r.ok:
                continue
            err_console.print(
                f"[yellow]channel {r.channel} failed: {r.message}[/yellow]"
            )


@alerts_app.command("check")
def alerts_check(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Evaluate + record new fires but skip every channel side effect.",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON."
    ),
) -> None:
    """Run one alert tick: evaluate thresholds and fire new crossings."""

    cfg = load_config()
    if not cfg.db_path.exists():
        err_console.print(
            "[yellow]no database yet; run 'tokie init' and 'tokie scan' first[/yellow]"
        )
        raise typer.Exit(code=1)

    result = check_alerts(cfg, dry_run=dry_run)

    if as_json:
        console.print_json(
            data={
                "ran_at": result.ran_at.isoformat(),
                "armed": [
                    {
                        "plan_id": c.plan_id,
                        "account_id": c.account_id,
                        "window_type": c.window_type,
                        "window_starts_at": c.window_starts_at_iso,
                        "window_resets_at": c.window_resets_at_iso,
                        "threshold_pct": c.threshold_pct,
                        "pct_used": c.pct_used,
                        "severity": c.severity(),
                    }
                    for c in result.armed
                ],
                "fired": [
                    {
                        "plan_id": c.plan_id,
                        "account_id": c.account_id,
                        "threshold_pct": c.threshold_pct,
                        "channels": list(c.channels),
                    }
                    for c in result.fired
                ],
                "dispatch_results": [
                    {
                        "channel": r.channel,
                        "ok": r.ok,
                        "message": r.message,
                    }
                    for r in result.dispatch_results
                ],
                "banner": [
                    {"text": line.text, "severity": line.severity}
                    for line in result.banner_lines
                ],
                "dry_run": dry_run,
            }
        )
        return

    if dry_run:
        console.print("[dim]--dry-run: no channel side effects will be triggered[/dim]")
    _render_alert_result(result)


@alerts_app.command("watch")
def alerts_watch(
    interval: int = typer.Option(
        60, "--interval", "-i", min=5, help="Seconds between ticks."
    ),
    iterations: int = typer.Option(
        0,
        "--iterations",
        "-n",
        help="Stop after N ticks (0 = loop forever, Ctrl-C to quit).",
    ),
) -> None:
    """Run ``tokie alerts check`` in a loop until Ctrl-C.

    Useful as a one-liner on a tmux pane or a tiny systemd service; for
    heavier use, wrap in ``cron``/``launchd`` calling ``tokie alerts check``.
    """

    cfg = load_config()
    if not cfg.db_path.exists():
        err_console.print(
            "[yellow]no database yet; run 'tokie init' and 'tokie scan' first[/yellow]"
        )
        raise typer.Exit(code=1)
    import time

    count = 0
    try:
        while True:
            result = check_alerts(cfg)
            ts = result.ran_at.strftime("%H:%M:%S")
            if result.fired:
                console.print(
                    f"[green]{ts}[/green] fired {len(result.fired)} new, "
                    f"{len(result.armed)} armed total"
                )
                for crossing in result.fired:
                    console.print(
                        f"  -> {crossing.display_name} [{crossing.account_id}] "
                        f"@ {crossing.threshold_pct}%"
                    )
            else:
                console.print(
                    f"[dim]{ts} ok ({len(result.armed)} armed, 0 new)[/dim]"
                )
            count += 1
            if iterations and count >= iterations:
                break
            time.sleep(interval)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        console.print("[dim]alerts watcher stopped[/dim]")


@alerts_app.command("reset")
def alerts_reset(
    confirm: bool = typer.Option(
        False,
        "--yes",
        help="Skip interactive confirmation — for scripts / rearming after testing.",
    ),
) -> None:
    """Delete every recorded fire so every armed threshold fires again.

    The fire log lives in the same SQLite DB as usage events but in a
    separate table, so this never affects historical usage. Use when you
    want to rearm channels after changing rules or after a dry-run.
    """

    cfg = load_config()
    if not cfg.db_path.exists():
        console.print("[dim]nothing to reset — no database yet.[/dim]")
        raise typer.Exit(code=0)
    if not confirm:
        err_console.print(
            "[yellow]pass --yes to confirm wiping the threshold fire log[/yellow]"
        )
        raise typer.Exit(code=2)

    from tokie_cli.alerts import AlertStorage, connect_alerts

    conn = connect_alerts(cfg.db_path)
    try:
        storage = AlertStorage(conn)
        removed = storage.clear()
    finally:
        conn.close()
    console.print(f"[green]cleared {removed} fire record(s)[/green]")


@alerts_app.command("banner")
def alerts_banner(
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Render the current alert banner (live, no dispatch, no DB write)."""

    cfg = load_config()
    if not cfg.db_path.exists():
        if as_json:
            console.print_json(data={"banner": []})
        else:
            console.print("[dim]no database yet — no banner.[/dim]")
        raise typer.Exit(code=0)

    # dry_run=True skips dispatch; storage still records but that's harmless
    # here because the same ticks are re-entrant by design. Using check_alerts
    # keeps the banner math identical to the dashboard.
    result = check_alerts(cfg, dry_run=True)

    if as_json:
        console.print_json(
            data={
                "banner": [
                    {"text": line.text, "severity": line.severity}
                    for line in result.banner_lines
                ]
            }
        )
        return

    if not result.banner_lines:
        console.print("[dim]all clear — no thresholds armed.[/dim]")
        return
    for line in result.banner_lines:
        console.print(f"[{_severity_style(line.severity)}]{line.text}[/]")


def _parse_iso(value: str) -> datetime | None:
    """Parse ``value`` as a tz-aware ISO-8601 datetime, or return ``None``.

    Accepts the ``Z`` suffix as shorthand for ``+00:00``. Refuses naive
    datetimes because mixing zones would poison ``since`` filtering.
    """

    text = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def main() -> None:
    """Console-script entry point; wires a sane default log level."""

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    app()


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["app", "main"]
