# Changelog

All notable changes to Tokie will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
