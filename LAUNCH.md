# Launch — Tokie v1.0.0

> Draft copy for the v1.0.0 launch post. Edit freely before posting.
> Meant for X / LinkedIn / Hacker News / your newsletter.

---

## One-line pitch

Tokie is a local-first CLI and dashboard that tracks token usage and
subscription quotas across every AI tool you pay for. v1.0.0 ships a
plugin SDK, an MCP server so your agents can ask Tokie directly, and
a honest-by-default dashboard that never mixes *exact* and *inferred*
numbers.

## Launch post (X / short form)

> I shipped Tokie v1.0.0 today — a local-first CLI + dashboard that
> shows how much Claude Pro / ChatGPT / Gemini / Codex / Cursor / etc.
> you've burned this cycle. Across every tool that bills you.
>
> New in 1.0: plugin SDK (`tokie.collectors` entry-points +
> pytest-tokie-connector) and an MCP server so Claude Code / Cursor /
> Codex can ask Tokie "how much do I have left?" directly. Read-only,
> stdio-only, zero network surface.
>
> Six weeks, six releases, one solo developer. No telemetry. No cloud.
> Keyring-only secrets. Loopback-only dashboard.
>
> uv tool install 'tokie-cli[mcp]'
> tokie init && tokie dashboard
>
> Source: https://github.com/vamshivittali76/Tokie

## Launch post (long form — blog / HN)

**Title:** I shipped a local-first AI usage tracker with a plugin SDK in 6 weeks

**Body:**

Six weeks ago I had a design doc and no code. Today Tokie v1.0.0 is on
PyPI, complete with a plugin SDK, an MCP server, and a 403-test suite
that runs mypy `--strict` clean.

Here's what it does:

```
$ uv tool install 'tokie-cli[mcp]'
$ tokie init
  detected: claude-code, codex, cursor-ide
$ tokie scan
  claude-code: 41 new / 53 seen (0.18s)
  codex: 12 new / 15 seen (0.21s)
  cursor-ide: 8 new / 8 seen (0.04s)
  total new events: 61 (3 collectors in 0.22s)
$ tokie dashboard
  http://127.0.0.1:7878
```

Everything runs in parallel. The dashboard shows one progress bar per
subscription — solid = exact, diagonal stripes = estimated, dashed =
inferred. Tokie never silently averages tiers; if your Claude Pro
weekly bucket has any inferred events, the bar renders dashed.

### What's new in v1.0.0

- **Plugin SDK.** Third parties ship collectors as installable
  packages that register under the `tokie.collectors` entry-point
  group. Built-ins win on name collision, but collisions are logged so
  plugin authors know to pick a different name. A
  `pytest-tokie-connector` contract plugin (shipped with `tokie-cli`
  itself) gives authors `assert_collector_contract`,
  `assert_scan_yields_valid_events`, and `assert_idempotent_rescan` for
  free.
- **MCP stdio server.** Four read-only tools for LLM agents:
  `list_subscriptions`, `get_usage`, `get_remaining`, `suggest_tool`.
  No TCP port. No network. No writes. Drop it into Claude Desktop /
  Claude Code / Cursor / Codex with a four-line JSON config.
- **Task recommender + handoff.** `tokie suggest code_review` ranks
  your active subscriptions against a hand-tuned `task_routing.yaml`
  matrix, discounting anything near its threshold. When you hit a
  cap, `tokie handoff` emits a paste-ready brief so you can switch
  tools without losing state.
- **Threshold alerts** at 75 / 95 / 100 % of any window, dispatched to
  desktop notifications or Slack/Discord webhooks, deduped per reset
  cycle.
- **Live TUI** (`tokie watch`) via Textual — per-subscription bars,
  sparklines, and reset countdowns for terminal lovers.
- **Parallel scans.** Every collector's `scan()` runs concurrently via
  `asyncio.gather`. Wall-clock scan time tracks the slowest collector,
  not the sum.

### Honest tracking

Tokie models the `shared_with` relationship between `claude-code`,
`claude-web`, and `claude-desktop` so Claude Pro's rolling-5h and
weekly buckets render as a single bar — one bucket, not three. If the
vendor's own dashboard would show 68 %, Tokie shows 68 %.

Web-only tools (Claude.ai, ChatGPT, Gemini Advanced, Perplexity Pro,
Grok, v0, Lovable, bolt.new, Manus, Devin, WisperFlow, Le Chat,
DeepSeek web) use the **manual collector**: you drop a CSV and Tokie
renders it with dashed outlines so you always know which numbers are
inferred. We will not scrape your browser history or reverse-engineer
session tokens.

### Built in public

Weekly releases, every Friday, all on the public repo:

- v0.1.0: core + 7 collectors + minimal dashboard
- v0.2.0: Cursor / Copilot / Gemini + Textual TUI + dashboard v2
- v0.3.0: threshold alerts + desktop + Slack/Discord + editor UI
- v0.4.0: task recommender + guided handoff + dashboard panel
- v1.0.0-rc1: plugin SDK + MCP server + connector template
- v1.0.0: launch-ready polish

Every week's CHANGELOG entry explains what shipped and why. See
[CHANGELOG.md](CHANGELOG.md) for the full set.

### Architecture

Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the layered
diagram and data flow. Short version: collectors → SQLite → pure
aggregator → (CLI, TUI, dashboard, alert engine, MCP server).
Everything downstream of the aggregator is a read-only view, which is
why the same JSON powers the dashboard and the MCP tools without a
cache-invalidation dance.

For writing your own connector, read
[`docs/CONNECTORS.md`](docs/CONNECTORS.md). The
[`templates/tokie-connector-example/`](templates/tokie-connector-example)
directory is a working minimal package you can `cp -R` and edit.

### Try it

```bash
uv tool install 'tokie-cli[mcp]'   # or: pipx install 'tokie-cli[mcp]'
tokie init
tokie doctor
tokie scan
tokie dashboard                    # or: tokie watch, or: tokie mcp serve
```

Source, issues, roadmap: https://github.com/vamshivittali76/Tokie

---

## Asset checklist (before posting)

- [ ] Record a 10-15 s GIF of `tokie dashboard` with real data in
      light + dark theme; drop at `docs/assets/dashboard.gif`.
- [ ] Record a 10-15 s GIF of `tokie watch` (Textual TUI); drop at
      `docs/assets/tui.gif`.
- [ ] Record a 20-30 s screencap of Claude Desktop calling
      `list_subscriptions` via MCP; drop at `docs/assets/mcp.gif`.
- [ ] Take a clean terminal screenshot of `tokie doctor` output showing
      the plans.yaml freshness line.
- [ ] Confirm `uv tool install 'tokie-cli[mcp]'` on a fresh venv +
      fresh machine completes and `tokie version` prints `1.0.0`.
- [ ] Verify PyPI long-description renders correctly (badges, code
      blocks, links) at https://pypi.org/project/tokie-cli/.
- [ ] Publish the GitHub release with the CHANGELOG entry as the body.
