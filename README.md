# Tokie

> Local-first CLI and localhost dashboard that tracks token usage and subscription quotas across every AI tool you pay for. Warns before limits hit, and recommends the tool with the most capacity left.

**Status:** pre-alpha. Building in public — [see the 6-week plan](IMPLEMENTATION_PLAN.md).
**License:** MIT
**Port:** `127.0.0.1:7878`

---

## Why

You probably pay for at least two AI subscriptions. Nobody today gives you a single view of what's left this cycle across all of them — existing tools are single-vendor (ccusage, claude-monitor), coding-agent-only (tokscale), or enterprise-focused (cursor-usage-tracker).

Tokie is the unclaimed middle: a unified control plane for a solo developer's mixed AI stack.

## Install (coming Friday of Week 1)

```bash
uv tool install tokie-cli
tokie init
tokie dashboard
```

The PyPI package is `tokie-cli` (the bare `tokie` slot is squatted by an unrelated tokenizer). The installed command is still `tokie`.

## What Tokie tracks

| Source | Supported | Confidence |
|---|---|---|
| Claude Code CLI | v0.1 | exact |
| Codex CLI | v0.1 | exact |
| Anthropic API | v0.1 | exact |
| OpenAI API | v0.1 | exact |
| Cursor IDE (individual Pro) | v0.2 | exact when endpoint works |
| Gemini CLI | v0.2 | exact |
| GitHub Copilot CLI | v0.2 | exact |
| Perplexity API | v0.2 | exact |
| Claude.ai web chat | not trackable locally | inferred (labeled) |
| Perplexity web Pro Searches | not trackable locally | inferred (labeled) |
| ChatGPT Plus web chat | not trackable locally | inferred (labeled) |

Tokie never silently averages `exact` and `inferred` data. If it can't track something honestly, the UI says so.

## How it's built

Python 3.11+, Typer, FastAPI, SQLite, HTMX + Alpine + Tailwind (no frontend build step), Textual for the live TUI. Collectors plug in via an entry-point SDK — third-party connectors ship as `tokie-connector-*` on PyPI.

Design docs:

- [TOKIE_DEVELOPMENT_PLAN_FINAL.md](TOKIE_DEVELOPMENT_PLAN_FINAL.md) — the *what and why* (architecture, schema, scope decisions)
- [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) — the *when* (6-week sprint, weekly releases)

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

Not open for contributions until v0.2. After that, see `CONTRIBUTING.md`.

## Security

No telemetry by default. Credentials live in the OS keyring. Dashboard binds loopback only. Full policy in `SECURITY.md` (ships with v1.0).
