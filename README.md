# Tokie

> Local-first CLI and localhost dashboard that tracks token usage and subscription quotas across every AI tool you pay for. Warns before limits hit, and recommends the tool with the most capacity left.

[![PyPI version](https://img.shields.io/pypi/v/tokie-cli.svg)](https://pypi.org/project/tokie-cli/)
[![Python versions](https://img.shields.io/pypi/pyversions/tokie-cli.svg)](https://pypi.org/project/tokie-cli/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![CI](https://github.com/vamshivittali76/Tokie/actions/workflows/ci.yml/badge.svg)](https://github.com/vamshivittali76/Tokie/actions/workflows/ci.yml)

**Status:** v1.0.0-rc1 (release candidate). Feature-complete;
polishing for v1.0.0. Build-in-public log:
[CHANGELOG.md](CHANGELOG.md).
**License:** MIT · **Default bind:** `127.0.0.1:7878`

---

## Why

You probably pay for at least two AI subscriptions. Nobody today gives
you a single view of what's left this cycle across all of them —
existing tools are single-vendor (ccusage, claude-monitor),
coding-agent-only (tokscale), or enterprise-focused
(cursor-usage-tracker).

Tokie is the unclaimed middle: a unified control plane for a solo
developer's mixed AI stack, with a typed plugin SDK and an MCP server
so your agents can ask "how much Claude Pro do I have left?" directly.

## Install

```bash
uv tool install tokie-cli               # or: pipx install tokie-cli
uv tool install 'tokie-cli[mcp]'        # add the MCP stdio server

tokie init                              # detects local collectors + writes default config
tokie doctor                            # shows which sources are ready (flags stale plans.yaml)
tokie scan                              # ingests detected usage in parallel across collectors
tokie dashboard                         # opens http://127.0.0.1:7878
tokie watch                             # live Textual TUI for terminal lovers
```

The PyPI project is **`tokie-cli`** (the bare `tokie` slot is squatted
by an unrelated tokenizer). The installed command is still `tokie`.

## What Tokie does

- **Ingests usage** from local log files (Claude Code, Codex, Cursor,
  Copilot CLI), vendor admin APIs (Anthropic, OpenAI, Gemini,
  Perplexity), and the generic OpenAI-compatible endpoint covering 14+
  providers. Web-only tools (Claude.ai, ChatGPT, Gemini Advanced,
  Grok, v0, etc.) use the manual CSV collector.
- **Dedupes by `raw_hash`.** Every scan is idempotent; re-running
  after a crash never double-counts.
- **Fires alerts** at configurable thresholds (default 75 / 95 / 100 %
  of a window) to desktop notifications, Slack, or Discord webhooks.
  Dedupes per reset cycle so you don't get pinged every minute.
- **Recommends tools** deterministically from a hand-tuned
  `task_routing.yaml` matrix, ranked by your remaining capacity. No
  LLM call, no randomness. `tokie suggest` + dashboard panel.
- **Hands off.** When a session hits a cap, `tokie handoff` builds a
  structured brief (last N events, active files, open questions) so
  you can switch tools without losing state.
- **Plugs in.** Third parties ship their own collectors as
  `tokie-connector-*` packages registered under the
  `tokie.collectors` entry-point group. Built-in
  `pytest-tokie-connector` contract plugin for authors.
- **Talks to agents.** Optional MCP stdio server exposes
  `list_subscriptions`, `get_usage`, `get_remaining`, and
  `suggest_tool` as read-only tools for Claude Desktop / Cursor /
  Codex CLI.

Tokie never silently averages `exact` and `inferred` data. If it can't
track something honestly, the UI says so.

## Dashboard

```bash
tokie dashboard              # 127.0.0.1:7878, opens your browser
tokie dashboard --port 9000  # custom port (still loopback)
tokie dashboard --remote     # explicit opt-in for non-loopback bind (prints a warning)
```

Every quota window renders with its own confidence tier:

- **solid bar** → `exact` (parsed from a local session log or pulled
  from a vendor admin API)
- **diagonal stripes** → `estimated` (reasonable math over the source
  data)
- **dashed outline** → `inferred` (web-only tool; you logged it
  manually via `tokie scan --collector manual`)

Color ramps emerald → amber → red at 75 / 95 / 100 % utilization.
Claude Pro's rolling-5h and weekly buckets render as a single bar
because Tokie models the `shared_with` relationship between
`claude-code`, `claude-web`, and `claude-desktop`.

The dashboard also carries an in-page **threshold editor** (POSTs back
to `tokie.toml`) and a **task recommender panel** (calls
`/api/recommend`). Everything stays local — no data leaves your
machine.

## How it's built

Python 3.11+, Typer, FastAPI, SQLite (WAL), HTMX + Alpine + Tailwind
(no frontend build step), Textual for the live TUI, `mcp` for the
agent-facing server. Collectors are discovered via Python entry points
so third-party connectors install like any pip package.

Deeper reading:

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — diagram, layer
  responsibilities, data flow of one `tokie scan`.
- [docs/CONNECTORS.md](docs/CONNECTORS.md) — write your own collector,
  wire the MCP server into Claude Code / Cursor / Codex.
- [docs/FAQ.md](docs/FAQ.md) — accuracy, privacy, alerts, common
  setup gotchas.
- [TOKIE_DEVELOPMENT_PLAN_FINAL.md](TOKIE_DEVELOPMENT_PLAN_FINAL.md) —
  the original *what and why* (schema, scope decisions).
- [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) — the *when* (6-week
  sprint, weekly releases).

## Roadmap

| Week | Version      | Highlights                                                    |
|------|--------------|---------------------------------------------------------------|
| 1    | v0.1.0       | Core + Claude/Codex/API collectors + minimal dashboard        |
| 2    | v0.2.0       | Cursor + Gemini + Copilot + live Textual TUI + dashboard v2   |
| 3    | v0.3.0       | Threshold alerts (desktop + Slack/Discord + editor UI)        |
| 4    | v0.4.0       | Task recommender + guided handoff + dashboard panel           |
| 5    | v1.0.0-rc1   | Plugin SDK + MCP stdio server + connector template            |
| 6    | v1.0.0       | Polish, docs, performance pass, launch                        |

## Contributing

Open for issues and bug reports — [file one
here](https://github.com/vamshivittali76/Tokie/issues). Code
contributions open after v1.0.0; watch the repo or join the Discussions
tab to be notified. If you want to ship a third-party collector today,
start from [templates/tokie-connector-example/](templates/tokie-connector-example/)
— the entry-point API is stable and covered by `pytest-tokie-connector`.

## Security

No telemetry by default. Credentials live in the OS keyring (never in
`tokie.toml`). The MCP server is read-only and stdio-bound, so there's
no network surface for an agent to abuse. Dashboard binds loopback
only; `--remote` is required to bind non-loopback interfaces and prints
a visible warning because Tokie has no auth layer yet. Full policy:
[`SECURITY.md`](SECURITY.md).
