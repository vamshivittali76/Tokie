# Changelog

All notable changes to Tokie will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
