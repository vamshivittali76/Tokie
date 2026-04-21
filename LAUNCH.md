# Launch — Tokie v0.1.0

> Draft copy for the first public build-in-public post. Edit freely before
> posting. Meant for X / LinkedIn / HackerNews / your newsletter.

---

## One-line pitch

Tokie is a local-first CLI and dashboard that tracks token usage and
subscription quotas across every AI tool you pay for. v0.1.0 ships seven
collectors, a 24-entry plan catalog, and a honest-by-default dashboard that
never mixes *exact* and *inferred* numbers.

## Launch post (X / short form)

> I built Tokie in public in five working days.
>
> It's a local-first CLI + localhost dashboard that shows how much of your
> Claude Pro / ChatGPT / Gemini / Codex / every-other-AI-sub you've burned
> this cycle, across every tool that sends you bills.
>
> No telemetry. No cloud. Keyring-only secrets. Loopback-only by default.
>
> uv tool install tokie-cli
> tokie init && tokie dashboard
>
> Source: https://github.com/vamshivittali76/Tokie

## Launch post (long form — blog / HN)

**Title:** I built a local-first AI usage tracker in 5 days and shipped it to PyPI

**Body:**

Five working days ago I had a design doc called `TOKIE_DEVELOPMENT_PLAN_FINAL.md`
and no code. Today `tokie-cli` is on PyPI at v0.1.0.

Here's what it does in one screen of terminal:

```
$ uv tool install tokie-cli
$ tokie init
  detected: claude-code (~/.claude/projects/...), codex (~/.codex/sessions/...)
$ tokie scan
  claude-code: 41 new events
  codex: 12 new events
$ tokie dashboard
  tokie dashboard -> http://127.0.0.1:7878  (Ctrl-C to stop)
```

The dashboard shows one progress bar per subscription. Claude Pro's rolling-5h
and weekly windows are rendered as a single bar each because Tokie models the
`shared_with` relationship between `claude-code`, `claude-web`, and
`claude-desktop` — one bucket, not three. Exact numbers render as solid bars;
estimated numbers as diagonal stripes; inferred numbers (web-only tools) as
dashed outlines. Tokie never silently averages them together.

### What's in v0.1.0

- **Seven collectors.** Local logs (Claude Code, Codex) and admin APIs
  (Anthropic, OpenAI, Gemini, any OpenAI-compatible provider, and a manual
  drop-file collector for the 14 web-only tools that have no local signal).
- **24-entry plan catalog** with trackability tiers so the UI is honest
  about what it can and can't observe.
- **Loopback-only dashboard** with FastAPI + HTMX + Tailwind + Chart.js,
  zero frontend build step, auto-refresh every 10s, Chart.js stacked 14-day
  bar chart.
- **231 tests, 36 source modules, mypy `--strict` clean, ruff clean.**
- **Trusted Publishing to PyPI.** There is no `PYPI_TOKEN` in this repo or
  in any of its secrets. Each release exchanges a GitHub OIDC token for
  PyPI credentials on the fly.

### What's explicitly not in v0.1.0

ChatGPT web, Claude.ai web, Gemini Advanced, Perplexity Pro web, Cursor
IDE, Copilot CLI, and the live Textual TUI. Everything in that list is on
the roadmap for v0.2.0 (shipping next Friday).

### Architecture notes

Read the planning docs — they have been maintained alongside the code:

- [`TOKIE_DEVELOPMENT_PLAN_FINAL.md`](TOKIE_DEVELOPMENT_PLAN_FINAL.md) — the
  *what and why* (architecture, schema, scope decisions).
- [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) — the *when* (the
  6-week solo sprint).

### Caveats before you try it

- Pre-alpha. The `UsageEvent` schema may change before v1.0.
- Windows + macOS + Linux all work, but every test-session I ran was on
  Windows 11. Please open issues for platform-specific breakage.
- The `manual` collector is the only honest way to track web-only AI
  tools. Tokie will not scrape your browser history or reverse-engineer
  session tokens.

### Try it

```bash
uv tool install tokie-cli   # or: pipx install tokie-cli
tokie init
tokie doctor
tokie scan
tokie dashboard
```

Source, issues, roadmap: https://github.com/vamshivittali76/Tokie

---

## Asset checklist (before posting)

- [ ] Record a 10-15s GIF of `tokie dashboard` with real data and drop it at
      `docs/assets/dashboard.gif`. Embed in the README's `## Dashboard`
      section.
- [ ] Take a clean terminal screenshot of `tokie doctor` for the X post.
- [ ] Confirm that `uv tool install tokie-cli` on a fresh venv on a fresh
      machine completes and `tokie version` prints `0.1.0`.
- [ ] Update the PyPI long-description once rendered there (verify code
      blocks and badges look right at https://pypi.org/project/tokie-cli/).
