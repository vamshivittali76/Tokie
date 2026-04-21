# Tokie

> Local-first CLI and localhost dashboard that tracks token usage and subscription quotas across every AI tool you pay for. Warns before limits hit, and recommends the tool with the most capacity left.

[![PyPI version](https://img.shields.io/pypi/v/tokie-cli.svg)](https://pypi.org/project/tokie-cli/)
[![Python versions](https://img.shields.io/pypi/pyversions/tokie-cli.svg)](https://pypi.org/project/tokie-cli/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![CI](https://github.com/vamshivittali76/Tokie/actions/workflows/ci.yml/badge.svg)](https://github.com/vamshivittali76/Tokie/actions/workflows/ci.yml)

**Status:** v0.1.0 (alpha). Building in public — [see the 6-week plan](IMPLEMENTATION_PLAN.md).
**License:** MIT · **Default bind:** `127.0.0.1:7878`

---

## Why

You probably pay for at least two AI subscriptions. Nobody today gives you a single view of what's left this cycle across all of them — existing tools are single-vendor (ccusage, claude-monitor), coding-agent-only (tokscale), or enterprise-focused (cursor-usage-tracker).

Tokie is the unclaimed middle: a unified control plane for a solo developer's mixed AI stack.

## Install

```bash
uv tool install tokie-cli   # or: pipx install tokie-cli
tokie init                  # detects local collectors + writes default config
tokie doctor                # shows which sources are ready
tokie scan                  # ingest detected usage into ~/.local/share/tokie/tokie.db
tokie dashboard             # opens http://127.0.0.1:7878
```

The PyPI project is **`tokie-cli`** (the bare `tokie` slot is squatted by an unrelated tokenizer). The installed command is still `tokie`.

## What Tokie tracks in v0.1

**Local log collectors (exact):**
- Claude Code CLI — parses `~/.claude/projects/**/*.jsonl`
- Codex CLI — parses `~/.codex/sessions/**/rollout-*.jsonl` (both chat-completion and Responses API shapes)

**API collectors (exact):** (credentials live in the OS keyring — never in config files)
- Anthropic Admin usage-report endpoint
- OpenAI Org usage-completions endpoint
- Google Gemini (via local log tailing — Google has no historical usage endpoint)
- **Generic OpenAI-compatible** — covers Groq, Together AI, DeepSeek, OpenRouter, Mistral, xAI Grok, Fireworks, Anyscale, Perplexity Sonar, Cerebras, Ollama, vLLM, LiteLLM — any provider that speaks OpenAI's `usage` block

**Manual / inferred (web-only tools):**
- Claude.ai web, ChatGPT web, Gemini Advanced, Google AI Studio, Le Chat, DeepSeek web, Grok web, Perplexity Pro, Manus, Devin, WisperFlow, v0, bolt.new, Lovable — 14 web-only plans with bundled CSV templates under `tokie scan --collector manual`.

Tokie never silently averages `exact` and `inferred` data. If it can't track something honestly, the UI says so.

## Dashboard

```bash
tokie dashboard              # 127.0.0.1:7878, opens your browser
tokie dashboard --port 9000  # custom port (still loopback)
tokie dashboard --remote     # explicit opt-in for non-loopback bind
```

Every quota window renders with its own confidence tier:

- **solid bar** → `exact` (parsed from a local session log or pulled from a vendor admin API)
- **diagonal stripes** → `estimated` (reasonable math over the source data)
- **dashed outline** → `inferred` (web-only tool; you logged it manually via `tokie scan --collector manual`)

Color ramps emerald → amber → red at 75 / 95 / 100% utilization. Claude Pro's rolling-5h and weekly buckets are rendered as a single bar each because Tokie models the `shared_with` relationship between `claude-code`, `claude-web`, and `claude-desktop`.

Everything stays local. The JSON feed at `/api/status` never leaves your machine.

## How it's built

Python 3.11+, Typer, FastAPI, SQLite, HTMX + Alpine + Tailwind (no frontend build step), Textual for the live TUI. Collectors plug in via an entry-point SDK — third-party connectors ship as `tokie-connector-*` on PyPI.

Design docs:

- [TOKIE_DEVELOPMENT_PLAN_FINAL.md](TOKIE_DEVELOPMENT_PLAN_FINAL.md) — the *what and why* (architecture, schema, scope decisions)
- [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) — the *when* (6-week sprint, weekly releases)
- [docs/CONNECTORS.md](docs/CONNECTORS.md) — write your own collector, wire the MCP server into Claude Code / Cursor / Codex

## Roadmap

| Week | Version | Highlights |
|------|---------|------------|
| 1 | v0.1.0 | Core + Claude/Codex/API collectors + minimal dashboard |
| 2 | v0.2.0 | Cursor + Gemini + Copilot + live Textual TUI |
| 3 | v0.3.0 | Threshold alerts (desktop + Slack/Discord) |
| 4 | v0.4.0 | Task recommender + guided handoff |
| 5 | v1.0.0-rc | Plugin SDK + MCP server |
| 6 | v1.0.0 | Polish, docs, launch |

## Contributing

Not open for contributions until v0.2. After that, see `CONTRIBUTING.md`. Issues and bug reports are welcome now — [open one here](https://github.com/vamshivittali76/Tokie/issues).

## Security

No telemetry by default. Credentials live in the OS keyring (never in `tokie.toml`). Dashboard binds loopback only — `--remote` is required to bind non-loopback interfaces and prints a visible warning because Tokie has no auth layer yet. Full policy: [`SECURITY.md`](SECURITY.md).
