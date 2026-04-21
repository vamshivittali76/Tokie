# Changelog

All notable changes to Tokie will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] — 2026-04-20

Week 4 of the build-in-public run: turn Tokie from a passive tracker into
an active advisor. When you hit a limit, Tokie now tells you *which tool
to reach for next* — deterministically, from a hand-tuned routing table,
ranked by your own remaining capacity. No LLM call, no randomness, no
network traffic.

### Added
- **`task_routing.yaml`** (`src/tokie_cli/task_routing.yaml`): hand-tuned
  task → tool preference matrix, versioned and bundled with the wheel.
  Covers 11 task types (code_generation, code_review, debugging,
  refactoring, documentation, research, long_context, quick_question,
  data_analysis, brainstorming) across 13 tools. Tiers 1/2/3 are human
  judgements; community PRs keep it fresh.
- **Routing table loader** (`src/tokie_cli/routing/table.py`): parses and
  validates the YAML into a frozen `RoutingTable` of `ToolEntry` and
  `TaskEntry` dataclasses. Schema errors surface as loud `ValueError`s at
  load time, not silent misrecommendations.
- **Deterministic recommender** (`src/tokie_cli/routing/recommender.py`):
  `recommend(task_id, table, subscriptions)` ranks your active
  subscriptions against the task's preferred tools by tier, then by
  remaining capacity. Pure function, fully reproducible — same inputs,
  same output, every time.
- **Handoff extractor** (`src/tokie_cli/routing/handoff.py`):
  `build_handoff(events, subscription=..., recommendation=...)` builds a
  `HandoffBrief` (goal, source, target, last-N events) from recent usage;
  `render_handoff(brief, fmt="markdown"|"plain")` produces a paste-ready
  summary for the next tool.
- **Auto-handoff bridge** (`src/tokie_cli/routing/auto_handoff.py`):
  `suggest_alternatives(crossings, subscriptions, table)` turns a live
  set of `ThresholdCrossing`s into `HandoffSuggestion`s — "Claude Pro is
  at 100%, try Cursor Pro (tier 1, 40% free)". Used by both the CLI and
  the dashboard so the guidance is consistent.
- **`tokie suggest [task_type]`**: ranked CLI output with tier badges,
  rationale from the routing table, saturation %, and a contextual
  handoff hint block when any subscription is over its limit.
- **`tokie handoff`**: prints a paste-ready brief (markdown by default,
  `--format plain` for copy-to-chat) summarising recent activity and the
  suggested target tool.
- **Dashboard recommender panel** (`src/tokie_cli/dashboard/templates/index.html`):
  new "Recommend a tool" section with a task-type selector. Shows ranked
  subscriptions with tier/saturation chips, rationale, and a cyan
  "suggested handoffs" block when thresholds are armed.
- **Dashboard APIs**:
  - `GET /api/routing` — full routing catalog (tools + tasks + tiers).
  - `GET /api/recommend?task=<id>` — ranked recommendations for a task
    plus auto-handoff suggestions derived from current armed thresholds.
    Returns 404 for unknown task ids so the UI can surface typos.

### Changed
- `tokie status` and `tokie alerts check` now render handoff hints under
  any armed threshold, so "you're at 95%" immediately tells you *where
  to go next*.
- `src/tokie_cli/cli.py` grew a `_load_subscription_views` helper so the
  new `suggest` / `handoff` commands share the exact event + plan load
  path used by `status` and `alerts check`.

### Internal
- New `src/tokie_cli/routing/` package with `table`, `recommender`,
  `handoff`, and `auto_handoff` submodules, plus a thin `__init__.py`
  that re-exports the public surface.
- Full unit coverage: `tests/test_routing_table.py`,
  `tests/test_routing_recommender.py`, `tests/test_routing_handoff.py`,
  `tests/test_routing_auto_handoff.py`, and
  `tests/test_dashboard_recommender.py` pin both pure-function and
  HTTP-endpoint behaviour.

## [0.3.0] — 2026-04-20

Week 3 of the build-in-public run: close the feedback loop with a real
alerting system. Tokie can now watch subscriptions against configurable
thresholds, dedupe fires per window, and deliver alerts through banner,
desktop, and webhook channels. Everything stays opt-in — the default
install still sends zero network traffic.

### Added
- **Threshold engine** (`src/tokie_cli/alerts/thresholds.py`): pure, I/O-free
  evaluation of `SubscriptionView`s against `ThresholdRule`s. Default levels
  are 75 / 95 / 100 and rules can target `plan_id` / `account_id`. Every
  crossing carries a stable dedupe key of
  `(plan_id, account_id, window_type, window_starts_at, threshold_pct)` so
  the same 95% hit never re-fires within the same reset window.
- **Fire storage** (`src/tokie_cli/alerts/storage.py`): new `threshold_fires`
  table in `tokie.db` (idempotent schema creation, indexed on `fired_at`).
  Powers dedupe across CLI invocations and dashboard renders.
- **Delivery channels** (`src/tokie_cli/alerts/channels.py`): `Channel`
  protocol with `BannerChannel` (always-on, renders color-coded lines into
  the `tokie status` header and the dashboard), `DesktopChannel`
  (`desktop-notifier`, opt-in via `alerts_desktop_enabled = true`), and
  `WebhookChannel` with Slack, Discord, and raw-JSON formats. Webhook URLs
  live in the OS keyring under `tokie-webhook/<name>`; the TOML only stores
  the name so configs stay paste-safe.
- **Alert engine** (`src/tokie_cli/alerts/engine.py`): ties config ->
  aggregator -> threshold evaluation -> storage -> channel dispatch into a
  single `check_alerts(...)` call. Returns a structured `AlertRunResult`
  with fired crossings, dispatch results, and the rendered banner.
- **CLI surface**: new `tokie alerts` subtree.
  - `tokie alerts check` — one-shot evaluation, prints banner + per-channel
    dispatch status.
  - `tokie alerts watch` — continuous loop (interval + `--once` escape hatch
    for tests).
  - `tokie alerts reset` — clears the `threshold_fires` table so every armed
    threshold can fire again.
  - `tokie alerts banner` — pure read: renders the current banner without
    triggering dispatch.
  - `tokie status` now prepends the armed banner when any threshold is
    currently crossed.
- **Dashboard threshold editor** (`src/tokie_cli/dashboard/templates/index.html`,
  `src/tokie_cli/dashboard/server.py`):
  - Armed banner rendered above the subscription grid.
  - New endpoints: `GET /api/alerts`, `GET /api/thresholds`, `POST
    /api/thresholds`.
  - Alpine-powered rule editor with add/remove rows, per-rule `plan_id`,
    `account_id`, `levels` (CSV), and `channels` (CSV). Save round-trips
    back to `tokie.toml` with the existing atomic-write path.
- **Config schema** (`src/tokie_cli/config.py`): `ThresholdRuleConfig` and
  `WebhookConfigEntry` dataclasses; `TokieConfig` gains `thresholds`,
  `webhooks`, and `alerts_desktop_enabled`. Round-trip parsing and saving
  preserve existing comments and other sections.
- New tests: `tests/test_alerts_thresholds.py`,
  `tests/test_alerts_storage.py`, `tests/test_alerts_channels.py`,
  `tests/test_alerts_engine.py`, plus extensions to the dashboard and CLI
  suites. 311 tests passing, `mypy --strict` clean, `ruff` clean.

### Changed
- `pyproject.toml` picks up `desktop-notifier` as an optional-but-declared
  dependency; the import is guarded so headless installs still work.
- `tokie status` is now threshold-aware but remains a pure read when no
  thresholds are configured (behaviour from v0.2.0 is preserved).

### Notes
- The alert pipeline never raises through `check_alerts`: a broken Slack
  webhook will never silently break the desktop notification for the same
  crossing. Per-channel failures surface as `ChannelDispatchResult` rows.
- Default config still has zero thresholds and zero webhooks, so v0.2.0
  behaviour is preserved byte-for-byte until the operator opts in.

## [0.2.0] — 2026-04-20

Week 2 of the build-in-public run: expand collector coverage, ship a live
terminal UI, and extend the dashboard with historical timeline, burn-rate,
multi-account switcher, and light/dark theme toggle. All feature flags
stay off-by-default; nothing about v0.1.0 breaks.

### Added
- **`github-copilot-cli` collector** (`src/tokie_cli/collectors/copilot_cli.py`): tails local NDJSON from `~/.config/github-copilot/history/`, `~/.copilot/history/`, and `%APPDATA%/GitHub Copilot/history/` (override via `TOKIE_COPILOT_LOG`). Parses both `prompt_tokens`/`completion_tokens` and `input_tokens`/`output_tokens` shapes. `EXACT` confidence.
- **`perplexity-api` collector** (`src/tokie_cli/collectors/perplexity_api.py`): log-tail collector for user-provided Perplexity response drops (vendor has no public historical usage endpoint; the keyring slot `tokie-perplexity/api_key` is reserved for a future HTTP path). Health surfaces a loud `vendor gap` warning when a key is stored but no drops exist yet.
- **`cursor-ide` collector** (`src/tokie_cli/collectors/cursor_ide.py`): feature-flagged drop-ingest collector. Reads either Cursor's dashboard CSV export (`ESTIMATED` confidence — token counts are derived from a fixed heuristic since the vendor CSV omits them) or user-supplied NDJSON with real `usage` blocks (`EXACT`). Loud warning in `tokie doctor` about the `ESTIMATED` tier and the vendor's lack of a public usage API.
- **`tokie watch` Textual TUI** (`src/tokie_cli/tui.py`): live per-subscription progress bars, confidence-tier glyphs (solid / shaded / dashed to mirror the dashboard), 24-hour sparkline per subscription, human-readable reset countdowns, `q` to quit and `r` to refresh. Reuses the existing aggregator so the TUI and web surface never drift.
- **Dashboard v2** (`src/tokie_cli/dashboard/aggregator.py`, `src/tokie_cli/dashboard/server.py`, `src/tokie_cli/dashboard/templates/index.html`):
  - Hourly timeline (last 7 days) rendered as a Chart.js line chart.
  - Rolling burn-rate chips (`1h` / `6h` / `24h`) in tokens/minute.
  - Multi-account switcher dropdown that filters subscription cards client-side (only shown when more than one `account_id` is present).
  - Light/dark theme toggle persisted in `localStorage` (`tokie.theme`).
  - New API endpoints: `GET /api/timeline`, `GET /api/burn-rate`, `GET /api/accounts`.
- New tests: `tests/test_collectors_copilot_cli.py`, `tests/test_collectors_perplexity_api.py`, `tests/test_collectors_cursor_ide.py`, `tests/test_tui.py`. Existing dashboard tests extended with burn-rate / timeline / account-list assertions. 252 tests passing, 0 mypy --strict errors.

### Changed
- `src/tokie_cli/cli.py` registers three new collectors in `_COLLECTOR_REGISTRY` so `tokie scan`, `tokie doctor`, and `tokie init` pick them up automatically. Adds the new `tokie watch` command.
- `DashboardPayload` now includes `accounts`, `hourly_timeline`, and `burn_rate`. Consumers that ignored unknown fields already stayed compatible; anything that asserted shape explicitly should see additive-only changes.

### Notes
- Gemini CLI coverage was already delivered in `api_gemini` (Day 3); Week 2's "Gemini CLI collector" is considered shipped in v0.1.0 and is not duplicated here.
- No collector sends a network request without an explicit credential in the keyring — every new collector is local-file-only by default.

## [0.1.0] — 2026-04-20

First public release. Delivers the core spec from
`TOKIE_DEVELOPMENT_PLAN_FINAL.md` Phase 1: a local-first CLI, seven
collectors spanning the major AI vendors, a 24-entry plan catalog with
trackability tiers, and a localhost dashboard with honest confidence
rendering. Built in public across five working days.

### Added (Day 5 — ship v0.1.0)
- `LICENSE` (MIT) and `SECURITY.md` (loopback-only dashboard, no prompt content in logs, keyring-only secrets).
- `.github/workflows/release.yml`: tag-triggered build -> TestPyPI -> PyPI -> GitHub Release pipeline using PyPI Trusted Publishing (OIDC). No long-lived API tokens anywhere in the repo. Verifies that the git tag matches `pyproject.toml` version before publishing.
- `.github/workflows/dryrun-testpypi.yml`: manual `workflow_dispatch` for pushing to TestPyPI without cutting a tag.
- `RELEASE.md`: step-by-step guide for cutting a release and the one-time Trusted Publisher setup.
- `LAUNCH.md`: build-in-public launch note draft.

### Changed
- `pyproject.toml` version `0.1.0.dev0` -> `0.1.0`. Project URLs updated to the real repo (`vamshivittali76/Tokie`). Sdist include list cleaned of non-existent files and now pins exactly the files we want to ship.
- Classifiers expanded: `Framework :: FastAPI`, `Typing :: Typed`, POSIX/macOS/Windows OS classifiers, `System :: Monitoring`, `Office/Business :: Financial`.

### Added (Day 4 — localhost dashboard)
- `src/tokie_cli/dashboard/aggregator.py`: pure-function layer that turns raw events + bundled plan templates + user-bound `SubscriptionBinding`s into dashboard view-models. Respects `shared_with` for Claude Pro's multi-product buckets, anchors rolling-5h and weekly windows on the first event inside the lookback, enforces per-`account_id` isolation, and downgrades confidence to `INFERRED` for any `web_only_manual` plan regardless of what the event claims.
- `src/tokie_cli/dashboard/server.py`: FastAPI app with `AppState` dependency-injection seam, endpoints `GET /api/health`, `/api/status`, `/api/subscriptions`, `/api/events`, `/api/daily`, and `GET /` for the HTML. `run()` refuses non-loopback binds without an explicit `allow_remote=True`.
- `src/tokie_cli/dashboard/templates/index.html`: single-page HTMX + Alpine.js + Tailwind CSS + Chart.js dashboard (zero build step, all CDN). Confidence tiers drive bar styling — exact/solid, estimated/striped, inferred/dashed outline — and pct_used drives the emerald → amber → red color ramp at 75/95/100%. Auto-refreshes every 10 s.
- `src/tokie_cli/cli.py`: new `tokie dashboard` command with `--host`, `--port`, `--remote`, and `--open/--no-open` flags. Opens the default browser on loopback binds; prints a red `refusing to bind` error (exit 2) and an amber `non-loopback … no auth yet` warning on remote binds.
- 27 new tests (13 aggregator, 11 server, 3 CLI) bringing the suite to **231 passing, 3 skipped**.

### Changed
- `pyproject.toml`: per-file `B008` ignore extended to `src/tokie_cli/dashboard/server.py` (FastAPI `Depends()` in defaults is idiomatic, same as Typer `Option()`).

### Security
- Dashboard defaults to `127.0.0.1:7878`. Non-loopback binds require both `tokie dashboard --remote` and `allow_remote=True` at the library layer — two independent guards.
- The HTML payload never embeds absolute filesystem paths (contract-tested).
- HTTP access logs run at `WARNING` by default; nothing about request bodies or prompt content is logged.

### Added (Day 3 — collectors, CLI, expanded plan catalog)
- `src/tokie_cli/collectors/base.py`: `Collector` ABC with `detect` / `scan` / `watch` / `health` contract, `CollectorHealth` dataclass, `CollectorError`, and a shared `make_event` helper that stamps `id` + `collected_at`.
- `src/tokie_cli/config.py`: immutable `TokieConfig` with `platformdirs`-based paths, TOML roundtrip (`tomli-w`), `TOKIE_CONFIG_HOME` / `TOKIE_DATA_HOME` overrides, 0600 perms on POSIX. Secrets never land in the file.
- Seven collectors, all producing canonical `UsageEvent` records:
  - `claude_code` — parses `~/.claude/projects/**/*.jsonl` session rollouts (exact).
  - `codex` — parses `~/.codex/sessions/**/rollout-*.jsonl` in both old chat-completions and new responses-API shapes (exact).
  - `api_anthropic` — calls the Admin usage-report endpoint via `httpx` with keyring-backed auth, retry + pagination (exact).
  - `api_openai` — calls `/v1/organization/usage/completions` with keyring-backed Bearer auth, bucket pagination (exact).
  - `api_gemini` — tails Gemini CLI history or a user-supplied NDJSON drop (Google has no historical usage endpoint); handles `thoughtsTokenCount` reasoning (exact).
  - `api_openai_compatible` — generic NDJSON tailer covering **Groq, Together AI, DeepSeek, OpenRouter, Mistral, xAI Grok, Fireworks, Anyscale, Perplexity Sonar, Cerebras, Ollama, vLLM, LiteLLM** — any provider speaking OpenAI's `usage` block (exact).
  - `manual` — CSV/YAML drop-file collector for untrackable web tools, emits `Confidence.INFERRED`.
- `src/tokie_cli/collectors/manual_templates/{README.md, web_tools.csv}`: starter template covering Manus, WisperFlow, Gemini Advanced, Google AI Studio, v0, bolt.new, Lovable, Devin, Mistral Le Chat, DeepSeek web, Grok web, Perplexity Pro, ChatGPT web, Claude.ai web — bundled inside the wheel.
- `src/tokie_cli/plans.py`: new `Trackability` StrEnum (`local_exact` / `api_exact` / `web_only_manual`) on every `PlanTemplate`; parser rejects unknown tiers at load time.
- `src/tokie_cli/plans.yaml`: expanded from 10 → 24 entries. New web-only tags: ChatGPT Pro, Gemini Advanced, Google AI Studio, Le Chat Pro, DeepSeek web, X Premium+ (Grok), Manus, Devin, WisperFlow, v0, bolt.new, Lovable. New API entry: Google Gemini API (paid). New local entry: OpenAI Codex CLI.
- `src/tokie_cli/cli.py`: Typer-based CLI with `version`, `paths`, `init`, `doctor`, `scan`, `status`, `plans` commands. Every command has a `--json` mode for scripting. End-to-end verified against real `~/.claude` data.

### Security
- `tokie doctor` never prints keyring values even when all collectors are configured (contract-tested).
- All collector `source` fields and log lines strip prompt content — only filenames, line numbers, and error type names are logged.
- Pydantic `extra="forbid"` on `UsageEvent`, `Subscription`, `LimitWindow` prevents silent field drift.

### Tests
- 204 passing (3 POSIX-permission tests skipped on Windows). mypy --strict clean on 31 modules. ruff clean.

## [Day 2]

### Added
- Project scaffold: `pyproject.toml`, `ruff`/`mypy` config, CI matrix.
- `src/tokie_cli/schema.py`: canonical `UsageEvent`, `Subscription`, `LimitWindow` Pydantic models.
- `src/tokie_cli/db.py`: SQLite persistence with idempotent schema-v1 migration, `insert_event` + `insert_events` with `raw_hash` dedup, filter-composable `query_events`. No ORM.
- `src/tokie_cli/windows.py`: pure quota math — `window_bounds` / `next_reset_at` for rolling-5h / daily / weekly / monthly / none, `aggregate_events` with half-open intervals, `capacity` with "most constrained" basis selection.
- `src/tokie_cli/plans.yaml`: bundled catalog of 10 subscription templates. Every entry cites a `source_url` for PR refresh.
- `src/tokie_cli/plans.py`: `PlanTemplate` + `load_plans` loader that validates every plan against the `Subscription` Pydantic contract.
- `scripts/v00_discover.py`: Phase 0 throwaway parser for Claude Code JSONL logs.

### Notes
- PyPI package is `tokie-cli` because `tokie` is squatted by an unrelated tokenizer. Python import name is `tokie_cli`; shipped CLI command remains `tokie`.

## [0.1.0] - TBD (Week 1 Friday)

See [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) for the full roadmap.
