# Tokie Architecture

Tokie is a **local-first** control plane for tracking token usage and
subscription quotas across every AI tool a solo developer pays for. It
runs on your laptop, stores data in SQLite, and never phones home.

This document explains how the pieces fit together. For the user-facing
tour, read the [README](../README.md). For extension points (writing
connectors, embedding the MCP server), read
[docs/CONNECTORS.md](./CONNECTORS.md).

---

## High-level diagram

```
+------------------+  +------------------+  +-----------------+
|  Local log files |  |  Vendor admin    |  |  Manual CSV     |
|  (~/.claude, …)  |  |  APIs (keyring)  |  |  imports        |
+--------+---------+  +---------+--------+  +--------+--------+
         |                      |                    |
         v                      v                    v
 +---------------------------------------------------------+
 |            Collectors  (tokie_cli.collectors.*)         |
 |  Claude Code · Codex · Cursor · Copilot · Anthropic …   |
 |  Contract: detect() -> bool, scan(since) -> events      |
 +-----------------------------+---------------------------+
                               |
                               v
 +---------------------------------------------------------+
 |  SQLite event store (~/.local/share/tokie/tokie.db)     |
 |  usage_events (UNIQUE raw_hash)  · alert_fires · WAL    |
 +------+------------------+-----------------+-------------+
        |                  |                 |
        v                  v                 v
 +-------------+    +-------------+    +----------------+
 |  Aggregator |    |  Alert      |    |  Routing /     |
 |  builds     |    |  engine     |    |  recommender   |
 |  windows    |    |  (75/95/100)|    |  + handoff     |
 +------+------+    +------+------+    +-------+--------+
        |                  |                   |
        +----+-------------+---------+---------+
             |                       |
             v                       v
     +---------------+     +---------------+     +-----------+
     |  Dashboard    |     |  CLI + TUI    |     |  MCP tools|
     |  FastAPI +    |     |  Typer app,   |     |  (stdio)  |
     |  HTMX/Alpine  |     |  Textual      |     |  for LLM  |
     |  (loopback)   |     |  watch        |     |  agents   |
     +---------------+     +---------------+     +-----------+
```

Every arrow is one-way. The CLI is the only component that writes to
`tokie.toml`. The dashboard, TUI, and MCP server are all read-only
surfaces over the aggregator.

---

## The event model

Everything downstream is a view over `UsageEvent` rows. See
`src/tokie_cli/schema.py` for the Pydantic model and
`src/tokie_cli/db.py` for the SQLite schema.

Key invariants:

- **`raw_hash` is UNIQUE.** Collectors derive it from fields the source
  never rewrites (timestamp + message id + model). The insert path uses
  `INSERT OR IGNORE`, so re-scanning the same file twice is a no-op.
  This is why every collector can be safely re-run on startup.
- **`confidence` is explicit.** Every event carries `EXACT`, `ESTIMATED`,
  or `INFERRED`. The aggregator and dashboard never silently mix tiers:
  if a window contains any `INFERRED` events, the bar renders dashed.
- **`occurred_at` is authoritative for windowing.** Threshold and
  remaining-capacity calculations use the vendor-reported event time,
  not the time Tokie ingested the event. This keeps results stable
  across retroactive imports.

---

## Layers

### 1. Collectors (`tokie_cli.collectors.*`)

Plug-in data sources. Each implements the
`tokie_cli.collectors.base.Collector` contract: a cheap `detect()`
class method, an `async scan(since)` iterator, and an optional
`health()` probe.

The **registry** (`collectors/registry.py`) merges built-ins with any
third-party packages that register a `tokie.collectors` entry point.
Built-ins win on name collision, but the collision is logged so a
plugin maintainer knows to pick a different name. See
[CONNECTORS.md](./CONNECTORS.md) for the author workflow.

Scans run **in parallel** via `asyncio.gather` from
`cli._run_scan`. Most collectors are I/O-bound (file reads, HTTP),
so wall-clock scan time tracks the slowest collector, not the sum. A
failing collector is reported and skipped; it never aborts the run.

### 2. Storage (`tokie_cli.db`)

Single SQLite file (`tokie.db`), stdlib-only, WAL journaling on disk.
Two tables matter:

- `usage_events` — every event a collector ever produced, keyed by
  `raw_hash`.
- `alert_fires` — dedupe ledger for the alert engine
  (sub + window + threshold → single fire per reset cycle).

Schema versioning lives in the `schema_version` table. Migrations are
idempotent and commit on `migrate()`.

### 3. Plans & subscriptions

`plans.yaml` (shipped inside the wheel) is the curated catalog of
vendor plans — limits, windows, `shared_with` relationships, citations.
`plans.py` parses and validates it; `load_plans_metadata` exposes an
`updated` timestamp so `tokie doctor` can warn when the bundled file is
older than `PLANS_FRESHNESS_WARN_DAYS` (default 60).

Users bind `plan_id -> account_id` pairs in `tokie.toml`. A
`SubscriptionBinding` is what makes a plan template "active" for your
install.

### 4. Aggregator (`tokie_cli.dashboard.aggregator`)

Pure function from `(bindings, plans, events)` to a
`DashboardPayload` containing `SubscriptionView`s and `WindowView`s.
No I/O. This is the single source of truth for "how saturated is my
Claude Pro plan right now?" — the dashboard, the TUI, the alert
engine, and the MCP server all call into it.

Because it's pure, every surface gets the same answer without a
cache-invalidation dance.

### 5. Alerts (`tokie_cli.alerts`)

Threshold engine (`engine.py`) evaluates `SubscriptionView`s against
`ThresholdRule`s. Defaults are 75 / 95 / 100%; users override via
`tokie.toml`. The dedupe key is `(sub, window, threshold, reset_epoch)`
so one threshold fires at most once per reset cycle.

Channels (`desktop.py`, `webhook.py`) implement a common `Channel`
Protocol. Webhook secrets live in the OS keyring — never in
`tokie.toml`.

### 6. Routing & handoff (`tokie_cli.routing`)

`task_routing.yaml` is a hand-tuned matrix mapping task types
(`code_generation`, `research`, …) to ranked tools with tier 1/2/3
preferences.

`recommender.recommend(task_id, table, subs)` ranks active
subscriptions against that matrix, discounting anything near its
threshold. No LLM call, no randomness. `handoff.extract(events)`
builds a structured hand-off brief — last N events, active files,
open questions — for when a user must jump from one tool to another.

The dashboard recommender panel and `tokie suggest` share the same
entry points.

### 7. Surfaces

- **CLI (`cli.py`).** Typer app. Every interactive command is a thin
  orchestrator over the layers above.
- **TUI (`tui.py`).** Textual app (`tokie watch`) that subscribes to
  the aggregator and re-renders on change. Useful for terminal
  dashboards while you code.
- **Dashboard (`dashboard/`).** FastAPI + HTMX + Alpine + Tailwind +
  Chart.js. No build step. Loopback by default; `--remote` is an
  explicit opt-in and prints a warning because Tokie has no auth
  layer.
- **MCP server (`mcp_server/`).** Optional (`pip install
  'tokie-cli[mcp]'`). Exposes four read-only tools to LLM agents:
  `list_subscriptions`, `get_usage`, `get_remaining`, `suggest_tool`.
  Handlers (`handlers.py`) are pure; only `server.py` imports the
  `mcp` SDK.

---

## Data flow: a concrete example

User types `tokie scan` immediately after a Claude Code session:

1. **CLI** loads `TokieConfig`, queries the registry for enabled
   collectors, and builds instances.
2. `_run_scan` gathers every `collector.scan(since=last_cursor)`
   concurrently. Each collector re-reads its source file from disk.
3. Each `UsageEvent` is added to a per-collector list; the lists are
   bulk-inserted via `insert_events` (single transaction per
   collector). Existing `raw_hash` values are silently ignored.
4. Total new-event count + per-collector timing are printed.

Next time the user runs `tokie status` or opens the dashboard:

5. The aggregator reads every event from `usage_events`, groups by
   `SubscriptionBinding`, and builds `WindowView` objects for every
   active window (rolling-5h, weekly, daily, monthly).
6. The alert engine re-evaluates those views against configured
   thresholds. Any new crossings write a row to `alert_fires` and
   dispatch to each configured channel (desktop, webhook).
7. The dashboard or CLI renders the `DashboardPayload`.

An LLM agent can skip the dashboard entirely and ask Tokie directly via
MCP: `list_subscriptions()` returns the same `DashboardPayload`
serialised as JSON.

---

## Why local-first

- **Privacy.** Your prompt history and file paths never leave your
  machine. The default dashboard binds to `127.0.0.1`.
- **Reliability.** No service to monitor, no API rate limit between you
  and your own data.
- **Offline-ok.** Rolling 5h limits are computed from local events, so
  airplane-mode developers still get accurate saturation.
- **Cost.** Zero infrastructure. `uv tool install tokie-cli` gives you
  the full stack.

Everything in this architecture is designed so a user can `rm
~/.local/share/tokie/tokie.db` and rebuild their view by re-scanning
local sources.
