# Tokie — Final Development Plan

> **A local-first, open-source CLI and localhost dashboard that tracks token usage and subscription quotas across every AI tool you pay for, warns before limits hit, and helps you continue work on the tool with the most capacity left.**

> **Status:** Pre-implementation specification. Synthesized from three research iterations (Claude Opus v1, Perplexity v1, Perplexity merged v2). This document is the authoritative plan.
> **Last updated:** 2026-04-19
> **License:** MIT
> **Primary user:** Solo developer running ≥2 paid AI subscriptions

---

## 1. Executive summary

Tokie is a **local-first AI usage control plane**. One tool that discovers your AI stack, normalizes heterogeneous usage data (JSONL logs, API endpoints, session tokens, manual entries) into a single schema, tracks quota windows correctly (rolling 5-hour, daily, weekly, monthly), warns at configurable thresholds, and recommends the cheapest or safest continuation path when one tool is close to exhaustion.

It is not a RAG pipeline. It is not a full agent. It is a collector + normalizer + dashboard, with MCP exposure added in v1.0 so other AI tools can query Tokie.

The unclaimed niche is **subscription-quota modeling + cross-tool handoff for mixed stacks**. Every existing tool (ccusage, tokscale, TokenTracker, claude-monitor) is single-vendor, coding-agent-only, or enterprise-focused. Nobody today gives an individual user with Claude Pro + Cursor Pro + Perplexity Pro a unified view of what's left this cycle.

---

## 2. Competitive landscape

| Tool | Scope | Approach | Gap Tokie fills |
|---|---|---|---|
| [ccusage](https://github.com/ryoppippi/ccusage) | Claude Code / Codex CLI | Parses local JSONL | No cross-tool handoff, no quota modeling |
| [tokscale](https://github.com/junhoyeo/tokscale) | 20+ coding agents | Rust TUI | Coding-only, no subscription awareness |
| [TokenTracker](https://github.com/mm7894215/TokenTracker) | 11 CLI tools | Zero-config hooks, macOS-native | Coding-only, no handoff |
| [claude-monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor) | Claude Code only | Terminal + ML predictions | Single-vendor |
| [cursor-usage-tracker](https://github.com/ofershap/cursor-usage-tracker) | Cursor Enterprise | Docker + Slack alerts | Requires Enterprise API keys |

If Tokie ships as another Claude Code log parser it loses on day one. If it ships as the subscription-aware, cross-tool control plane, it owns unclaimed territory.

---

## 3. The trackability wall (read this before scoping)

This is the single most important scoping constraint. Not every AI subscription leaves a local trail.

| Source | Locally trackable? | Method | Confidence |
|---|---|---|---|
| Claude Code CLI | ✅ | JSONL at `~/.claude/projects/` | **exact** |
| Codex CLI | ✅ | Local session files | **exact** |
| Gemini CLI | ✅ | Local session files | **exact** |
| GitHub Copilot CLI | ✅ | Local session files | **exact** |
| Anthropic / OpenAI / Perplexity API (direct) | ✅ | Official usage endpoints + response token counts | **exact** |
| Cursor IDE (individual Pro/Ultra) | ⚠️ | Unofficial `/api/usage` endpoint via locally-stored session token | **exact when it works**, may break without notice |
| Cursor IDE (team) | ✅ | Official Admin API (admin-only) | **exact** |
| Claude.ai web/mobile chat | ❌ | No local signal; no usage endpoint; Anthropic surfaces the number only after limit-hit | **not trackable in v0.1** |
| Perplexity Pro web (Pro Searches, Deep Research) | ❌ | No public API for web quota | **not trackable in v0.1** |
| ChatGPT Plus web chat | ❌ | Opaque soft caps, no endpoint | **not trackable in v0.1** |

### The critical Claude Pro modeling note

Claude Pro is **not one counter**. It's two overlapping windows that share a single bucket across surfaces:

- A **5-hour rolling window** (resets 5 hours after each session's first message)
- A **weekly cap** (resets 7 days after the session starts)
- **Claude.ai web, Claude mobile, Claude Desktop, and Claude Code all draw from the same bucket.** Max plans multiply the allowance.

This means Tokie cannot accurately track Claude Pro usage by watching Claude Code alone — it will always underreport by the amount of web/mobile chat the user does. **This must be surfaced honestly in the UI** (see §6 on confidence tiers).

### v0.1 scope decision

**CLI and API-based tools only.** Manual web logging and browser-extension coverage are deferred to post-v1. Ship fast with accurate data; don't silently lie with incomplete data.

---

## 4. What Tokie is and isn't

| Pattern | Use? | Why |
|---|---|---|
| RAG pipeline | No | No documents to retrieve over. This is structured telemetry. |
| Full agentic loop | No | The recommender is one structured LLM call plus deterministic rules. No loop needed. |
| Local collector + normalizer + dashboard | **Yes** | 80% of the codebase. |
| MCP server (adapter) | Yes, v1.0 | So Claude Code / Cursor / etc. can query their own remaining quota mid-session. |
| Plugin SDK | Yes, v1.0 | Community-maintained connectors for long-tail tools. |

---

## 5. Technology stack (committed)

| Layer | Choice | Rationale |
|---|---|---|
| Language | **Python 3.11+** | Proven by claude-monitor and phuryn/claude-usage. Rich + Textual are unmatched for TUI. The hot collector paths can be ported to Rust in v1+ if profiling demands it; do not optimize prematurely. |
| Package manager & installer | **uv** (`uv tool install tokie`) | One command, no venv dance, fastest modern Python tooling. |
| CLI framework | **Typer** | Type hints → autocomplete → docs, free. |
| API server | **FastAPI + uvicorn** | Async, OpenAPI for free (the MCP adapter reuses it). |
| Database | **SQLite** via `sqlite3` stdlib + SQL migrations | Single file, zero infra, mode 0600. No ORM in v0.1 — keep queries inspectable. |
| Dashboard frontend | **HTMX + Alpine.js + Tailwind CSS** | No build step. Contributors clone and run. Charts via **Chart.js** from CDN. |
| TUI | **Textual** (live), **Rich** (static output) | Same ecosystem, well-maintained. |
| Notifications | **desktop-notifier** | Cross-platform (macOS, Windows, Linux). |
| Credential storage | **keyring** | Lands in macOS Keychain / Windows Credential Manager / libsecret. Never plaintext. |
| MCP server | **mcp** (official Anthropic SDK) | Adapter, v1.0. |
| Testing | **pytest + pytest-asyncio + pytest-recording** | Fixture-based collector tests (see §12). |
| Lint/format | **ruff** (both) | Single tool. |
| Type checking | **mypy --strict** | Catches schema-drift bugs early. |
| CI | **GitHub Actions** | Matrix over Python 3.11/3.12/3.13 × macOS/Linux/Windows. |
| Release | **Trusted Publishing to PyPI** from tagged releases | No long-lived tokens. |
| License | **MIT** | Matches every peer tool (ccusage, tokscale, TokenTracker). Maximum adoption. |

**Port:** Dashboard binds `127.0.0.1:7878` by default. `--host` and `--port` override; `--remote` required to bind non-loopback with a visible warning.

---

## 6. Canonical schema

Every collector produces rows in this exact shape. This is the contract.

```python
# src/tokie/schema.py
from datetime import datetime
from enum import Enum
from pydantic import BaseModel

class Confidence(str, Enum):
    EXACT     = "exact"      # official API response or structured log
    ESTIMATED = "estimated"  # parsed from semi-structured logs
    INFERRED  = "inferred"   # statistical model, manual entry, or heuristic

class WindowType(str, Enum):
    ROLLING_5H = "rolling_5h"
    DAILY      = "daily"
    WEEKLY     = "weekly"
    MONTHLY    = "monthly"
    NONE       = "none"      # pay-as-you-go, no window

class UsageEvent(BaseModel):
    # Identity
    id: str                       # uuid
    collected_at: datetime        # when Tokie saw it
    occurred_at: datetime         # when the LLM call happened

    # Source
    provider: str                 # "anthropic" | "openai" | "cursor" | ...
    product: str                  # "claude-code" | "claude-web" | "cursor-ide" | ...
    account_id: str               # hashed email or workspace id — supports multi-account
    session_id: str | None
    project: str | None

    # Call
    model: str                    # "claude-opus-4-7" | "gpt-5" | ...
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float | None        # null if subscription-included

    # Provenance (critical)
    confidence: Confidence
    source: str                   # "jsonl:~/.claude/projects/..." | "api:anthropic/usage" | "manual"
    raw_hash: str                 # sha256 of the original record for dedup
```

The `confidence` field is non-negotiable. It drives dashboard rendering: **exact → solid bar; estimated → striped; inferred → dashed outline.** Mixing these silently destroys user trust the first time a number looks wrong.

### Limits are modeled separately

```python
class Subscription(BaseModel):
    id: str                       # "claude_pro_personal"
    provider: str
    product: str
    plan: str                     # "pro" | "max5" | "max20" | "free"
    account_id: str
    # A subscription can have multiple overlapping windows
    windows: list["LimitWindow"]

class LimitWindow(BaseModel):
    window_type: WindowType
    limit_tokens: int | None      # null if message-based
    limit_messages: int | None
    limit_usd: float | None
    resets_at: datetime | None    # computed dynamically
    shared_with: list[str] = []   # other product ids sharing this bucket
                                  # e.g. claude-pro shares between claude-web + claude-code
```

The `shared_with` field is how Tokie models the Claude Pro shared bucket. The 5-hour and weekly windows for `claude_pro_personal` declare `shared_with=["claude-web", "claude-code"]`. The dashboard then shows one progress bar for the subscription, not two, and labels the web portion as `INFERRED` unless the user has a browser-extension companion installed later.

---

## 7. Architecture

```
                   ┌─────────────────────────────────────────────┐
                   │                  Tokie Core                 │
                   │                                             │
  ┌────────────┐   │   ┌──────────────┐     ┌──────────────┐    │   ┌────────────┐
  │ JSONL logs ├──►│   │              │     │              │    │   │  CLI TUI   │
  │ ~/.claude  │   │   │  Collectors  ├────►│   SQLite     │    ├──►│  tokie     │
  └────────────┘   │   │ (per source) │     │  tokie.db    │    │   │  watch     │
                   │   │              │     │              │    │   └────────────┘
  ┌────────────┐   │   │  - claude    │     └──────┬───────┘    │
  │ Cursor     ├──►│   │  - cursor    │            │            │   ┌────────────┐
  │ session    │   │   │  - codex     │            ▼            │   │ Dashboard  │
  │ token      │   │   │  - gemini    │     ┌──────────────┐    ├──►│ 127.0.0.1  │
  └────────────┘   │   │  - copilot   │     │  Normalizer  │    │   │  :7878     │
                   │   │  - apis      │     │  + Windows   │    │   └────────────┘
  ┌────────────┐   │   │  - manual    │     └──────┬───────┘    │
  │ API usage  ├──►│   │              │            │            │   ┌────────────┐
  │ endpoints  │   │   └──────────────┘            ▼            │   │ OS Notif   │
  └────────────┘   │                        ┌──────────────┐    ├──►│  (native)  │
                   │   ┌──────────────┐     │    Alerts    │    │   └────────────┘
  ┌────────────┐   │   │  MCP Server  │     │ 75/95/100%   │    │
  │  Manual    ├──►│   │    (v1.0)    │     └──────┬───────┘    │   ┌────────────┐
  │   CSV      │   │   └──────┬───────┘            │            │   │ Webhook    │
  └────────────┘   │          │                    ▼            ├──►│ (Slack/    │
                   │          │             ┌──────────────┐    │   │  Discord)  │
                   │          └────────────►│  Recommender │    │   └────────────┘
                   │                        │  + Handoff   │    │
                   │                        └──────────────┘    │   ┌────────────┐
                   │                                            ├──►│ MCP client │
                   │                                            │   │ (external) │
                   └────────────────────────────────────────────┘   └────────────┘
```

---

## 8. The Collector contract

Every connector — built-in and third-party — implements this interface. This is what the plugin SDK standardizes.

```python
# src/tokie/collectors/base.py
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

class Collector(ABC):
    name: str                    # "claude-code"
    default_confidence: Confidence

    @classmethod
    @abstractmethod
    def detect(cls) -> bool:
        """Return True if this collector's data source exists on this machine."""

    @abstractmethod
    async def scan(self, since: datetime | None) -> AsyncIterator[UsageEvent]:
        """Yield all UsageEvents since the given timestamp. Idempotent by raw_hash."""

    @abstractmethod
    async def watch(self) -> AsyncIterator[UsageEvent]:
        """Yield new UsageEvents as they appear. Long-running. Called by `tokie watch`."""

    def health(self) -> CollectorHealth:
        """Called by `tokie doctor`. Return whether the source is readable,
        last successful scan time, and any warnings."""
```

Third-party collectors publish as `tokie-connector-<name>` on PyPI and register via the `tokie.collectors` entry point. Tokie discovers them automatically on startup.

---

## 9. CLI surface

```bash
tokie init              # Interactive setup: detect sources, confirm plans, write config
tokie doctor            # Health check: which sources are readable, last scan times,
                        # OS permissions, keyring status, schema version
tokie scan              # One-shot parse of all sources. Idempotent. Safe to re-run.
tokie watch             # Background collector daemon (daemonized with --detach)
tokie status            # Current snapshot: per-subscription progress bars in terminal
tokie status --json     # Machine-readable for scripting
tokie forecast          # Burn-rate projection to next reset
tokie dashboard         # Opens 127.0.0.1:7878, starts server if not running
tokie suggest "task"    # Recommend which tool to use for a given task
tokie handoff           # Package recent session context for handoff to another tool
tokie mcp               # Start the MCP server (v1.0+)
tokie purge --older-than 90d
tokie export --format csv|json
```

---

## 10. Phased roadmap with estimates

One milestone every 1–2 weeks of focused work. Estimates assume solo developer part-time.

### Phase 0 — Schema discovery (3–5 days, before repo)

- Write a 50-line Python script: read `~/.claude/projects/*.jsonl`, sum tokens per day, print a table
- Write the same for one other source (Codex CLI or Cursor session token)
- Finalize `schema.py` against real data
- Grep PyPI, npm, GitHub for "tokie" name collisions. If taken, fall back to `tokie-cli` or `tokied`. (At time of writing, `tokie` on PyPI appears unused but verify before committing.)
- Confirm: Python (committed), MIT (committed), CLI/API-only for v0.1 (committed)

**Exit criteria:** a working throwaway script and a schema doc.

### Phase 1 — Local core + minimal dashboard (v0.1, ~2 weeks)

> **Decision:** The dashboard ships in v0.1 alongside the CLI, not in v0.2. The original product vision is "CLI + localhost dashboard" — splitting them delays the differentiator. The FastAPI server is built anyway to feed `tokie status`; serving HTMX templates off it is essentially free.

- [ ] Repo scaffolding: `pyproject.toml`, ruff, mypy, pre-commit, GitHub Actions matrix CI
- [ ] `tokie init` interactive setup
- [ ] `tokie doctor` health check
- [ ] Collectors: Claude Code, Codex CLI, Anthropic API, OpenAI API, manual CSV/JSON
- [ ] SQLite schema + migrations (use `yoyo-migrations` or hand-rolled)
- [ ] `tokie scan` idempotent by `raw_hash`
- [ ] `tokie status` in terminal with Rich progress bars + confidence indicators
- [ ] Minimal dashboard (one page): per-subscription cards with progress rings, recent sessions table, daily token bar chart. HTMX, no frontend build step.
- [ ] `plans.yaml` bundled with known limits for Claude Pro/Max, OpenAI tiers, etc.
- [ ] PyPI release: `uv tool install tokie`
- [ ] README with honest scope: "v0.1 tracks CLI and direct-API usage. Claude.ai web chat and Perplexity web Pro Searches are not locally trackable and are shown as INFERRED."

**Exit criteria:** you install your own tool, it sees your real usage, the dashboard is accurate.

### Phase 2 — More collectors + live TUI (v0.2, ~2 weeks)

- [ ] Collectors: Cursor IDE (individual Pro via session token), Gemini CLI, GitHub Copilot CLI
- [ ] Document the Cursor session-token extraction procedure in the README (from `~/Library/Application Support/Cursor/User/globalStorage/` on macOS; equivalent paths on other OSes). Token goes into OS keyring, not config.
- [ ] `tokie watch` — Textual-based live TUI with per-tool progress bars and burn rate
- [ ] Dashboard v2: historical timeline, burn-rate chart, reset countdowns, light/dark mode
- [ ] Multi-account support (personal Claude Pro + work Claude Team on same machine, differentiated by `account_id`)

### Phase 3 — Alerts (v0.3, ~1 week)

- [ ] Threshold engine: fires at 75%, 95%, 100% by default; 25% and 50% are opt-in (25% is almost always noise)
- [ ] Alert de-duplication (don't fire the same threshold twice in the same window)
- [ ] Desktop notifications via `desktop-notifier`
- [ ] Optional Slack/Discord webhooks configured in `tokie.toml`
- [ ] Terminal bell + color-coded `tokie status` banner
- [ ] Dashboard: threshold config UI

### Phase 4 — Task recommender + handoff (v0.4, ~2 weeks)

**Recommender — MVP:**
- Deterministic rules v1: for each subscription, `score = capacity_remaining_pct × task_fit × (1 / relative_token_cost)`
- `task_fit` is a hand-tuned YAML matrix (Perplexity → "web research"; Cursor → "in-repo edits"; Claude → "long-form reasoning"; etc.)
- Editable by users; PRs welcome
- v2 (stretch): single structured LLM call to classify the task, feeding into the same scoring function

**Handoff — MVP (guided, not automatic):**
1. When a collector sees a `usage_limit_exceeded` error or the user runs `tokie handoff`:
2. Extract last N turns from the active session log
3. One cheap LLM call to produce a "continuation prompt" (or skip LLM and use a literal transcript if the user opts out)
4. List subscriptions with remaining capacity, ranked by recommender score
5. User picks one → Tokie copies the prompt to clipboard and opens the target tool (via `cursor://` URL, `code` CLI, or browser)

Full automatic injection via MCP is deferred to v1.0.

### Phase 5 — Plugin SDK + MCP server (v1.0, ~2 weeks)

- [ ] Entry-point discovery for third-party `tokie-connector-*` packages
- [ ] Connector development guide + cookiecutter template
- [ ] `tokie mcp` server exposing: `get_usage`, `get_remaining`, `list_subscriptions`, `suggest_tool`
- [ ] Documented integration snippets for Claude Code and Cursor MCP config
- [ ] `SECURITY.md` + responsible disclosure policy
- [ ] v1.0 PyPI release

### Stretch (post-v1.0)

- Browser extension companion (Claude.ai, Perplexity, ChatGPT)
- Menu-bar apps (native macOS, Windows tray, Linux AppIndicator)
- Cost-comparison reports ("you'd have paid $X on API vs your $20 Pro sub")
- Per-project attribution (which repo chewed through your quota)
- Optional encrypted multi-machine sync
- Team edition

---

## 11. Three things not in either prior plan

### 11.1 `plans.yaml` update strategy

Vendors change subscription limits. Anthropic adjusted Pro quotas twice in 2025–2026. Tokie ships with a bundled `plans.yaml` but it must stay current.

**Mechanism:**
1. Primary: bundled with each Tokie release. Pinned to a schema version.
2. On `tokie doctor`, check (with user consent, off by default) for a newer `plans.yaml` at `https://raw.githubusercontent.com/<org>/tokie/main/plans.yaml`. Notify, don't auto-apply.
3. Community maintains via PRs. Each entry has a `source_url` citation to the vendor page.
4. Users can override with `~/.tokie/plans.override.yaml`.

### 11.2 Handling plan changes mid-cycle

User upgrades Pro → Max on April 12; weekly window started April 10.

**Rule:** plan changes are recorded as `SubscriptionChangeEvent` rows with an effective timestamp. Historical `UsageEvent` rows are never rewritten. The current-window math uses the plan that was active at `occurred_at` for each event. The progress bar shows the **effective remaining capacity under the current plan** but labels the total consumed so far with a split (e.g., "8k used pre-upgrade + 2k used post-upgrade").

### 11.3 Testing strategy

Collectors are the fragile surface. They break when vendors change file formats.

- **Fixture-based unit tests.** Every collector ships with 3–5 real (sanitized) JSONL/API-response fixtures in `tests/fixtures/<collector>/`. Collector tests are pure: given fixture → expect `list[UsageEvent]`. No I/O, no network.
- **Golden-file schema tests.** Pydantic schema changes trigger a regeneration of golden JSONs; CI fails if diff is unreviewed.
- **Contract tests for third-party connectors.** The plugin SDK ships a pytest plugin (`pytest-tokie-connector`) that third parties run against their own collector to verify compliance.
- **Network tests gated.** Any test requiring network is marked `@pytest.mark.network` and skipped in default CI.

---

## 12. Security model

- **No telemetry by default.** Opt-in crash reporting (scrubbed) can be added in v0.3 if needed.
- **Read-only log access.** Collectors open files read-only. They never write to vendor directories.
- **Credentials in OS keyring.** Never plaintext in `tokie.toml`. API keys, Cursor session tokens, webhook secrets all go through `keyring`.
- **Localhost by default.** Dashboard binds `127.0.0.1:7878`. `--remote` flag required to bind non-loopback and prints a warning.
- **File permissions.** `tokie.db`, audit log, and config at mode `0600`.
- **No auto-update.** Explicit user command required.
- **No prompt content collection.** Only metadata (tokens, timestamps, model, cost). Full-content indexing is behind an explicit `--index-content` flag with a storage warning.
- **Audit log.** Every collection run appends to `~/.tokie/audit.log` (timestamp, collector, rows added, rows deduped).
- **Source labeling in the UI.** Visually distinguish `exact` / `estimated` / `inferred` confidence. Never silently average across them.
- **Plugin provenance.** Third-party connectors run in-process (Python limitations), so the docs must warn: you are installing code. Only install `tokie-connector-*` packages you trust. Consider a curated "blessed connectors" list in the README.

---

## 13. Open decisions — now committed

| Decision | Resolution |
|---|---|
| Primary user | Solo dev. Teams deferred to stretch. |
| Web coverage in v0.1 | CLI/API-only. Browser extension in v1.x+. |
| Language | Python 3.11+. |
| License | MIT. |
| Database | SQLite. |
| Dashboard ships in | v0.1 (minimal), expanded in v0.2. |
| Default port | `127.0.0.1:7878`. |
| Alert thresholds | 75 / 95 / 100% default. 25 / 50% opt-in. |
| Cursor individual-user support | Ship with disclaimer that the endpoint is unofficial. |
| Recommender | Deterministic rules v1. Optional LLM classification v2. |
| Handoff | Guided (clipboard + open tool) in v0.4. Automatic MCP injection in v1.0. |
| Telemetry | None. |
| MCP server | Optional, v1.0. Not part of the core. |
| Credentials | OS keyring. |

Decisions that stay open and are fine to defer:
- Exact dashboard framework choice if HTMX feels limiting in v0.2 (may swap to React/Svelte for chart-heavy views)
- Whether to port collector hot paths to Rust in v1.x (profile first)

---

## 14. Repo layout

```
tokie/
├── pyproject.toml
├── README.md
├── CONTRIBUTING.md
├── SECURITY.md
├── ROADMAP.md
├── LICENSE                       # MIT
├── plans.yaml                    # bundled, PR-driven updates
├── task_routing.yaml             # editable recommender matrix
├── src/tokie/
│   ├── __init__.py
│   ├── cli.py                    # Typer entry
│   ├── config.py                 # tokie.toml + keyring bridge
│   ├── schema.py                 # Pydantic models (the contract)
│   ├── db.py                     # sqlite3 + migrations
│   ├── windows.py                # rolling-5h / daily / weekly / monthly math
│   ├── collectors/
│   │   ├── base.py               # Collector ABC
│   │   ├── claude_code.py
│   │   ├── cursor.py
│   │   ├── codex.py
│   │   ├── gemini.py
│   │   ├── copilot.py
│   │   ├── api_anthropic.py
│   │   ├── api_openai.py
│   │   ├── api_perplexity.py
│   │   └── manual.py
│   ├── alerts/
│   │   ├── engine.py
│   │   └── channels.py
│   ├── dashboard/
│   │   ├── server.py             # FastAPI
│   │   ├── routes.py
│   │   ├── templates/            # HTMX
│   │   └── static/
│   ├── tui/live.py               # Textual
│   ├── recommender/
│   │   └── suggest.py
│   ├── handoff/
│   │   └── bridge.py
│   └── mcp/server.py             # v1.0
├── tests/
│   ├── fixtures/
│   │   ├── claude_code/
│   │   ├── cursor/
│   │   └── ...
│   ├── test_schema.py
│   ├── test_windows.py
│   ├── test_collectors/
│   └── test_recommender.py
└── .github/
    └── workflows/
        ├── ci.yml
        └── release.yml
```

---

## 15. Immediate next actions

In order. Do not skip.

1. **Run the v0.0 script.** 50 lines, reads `~/.claude/projects/*.jsonl`, prints daily totals. Confirms you understand the data before designing around it.
2. **Clear the name.** Check PyPI, npm, and top GitHub results for `tokie`. If taken: `tokie-cli`, `tokied`, `tokey`, or rename entirely.
3. **Create the repo.** `README.md` + `CONTRIBUTING.md` + this plan as `docs/DEVELOPMENT_PLAN.md` + `schema.py` + empty `ROADMAP.md` — before any logic.
4. **Wire the CI skeleton.** Ruff + mypy + pytest running on an empty test, matrix over 3.11/3.12/3.13 × macOS/Linux/Windows. Green badge on day one.
5. **Ship `tokie doctor` and one collector.** Claude Code. Nothing else. Push it to TestPyPI. Install it on your own machine and watch it work. This is your first real milestone.

Everything after that falls out of the phase plan.

---

## 16. Why Tokie wins if built right

1. **One normalized schema** for heterogeneous AI usage sources.
2. **Correct subscription-window modeling** — rolling 5-hour, weekly, daily, monthly, shared buckets.
3. **Cross-tool intelligence** — when to switch, where to go, how to hand off.
4. **Trust through transparency** — `exact` / `estimated` / `inferred` confidence on every number.
5. **Honest scope** — the README says up front what can and can't be tracked; no silent lies.

Ship this and Tokie owns the unclaimed territory the current tools leave open.

---

*Final plan synthesizing Claude Opus 4.7 v1, Perplexity v1, and Perplexity v2 research iterations. 2026-04-19.*
