# Tokie FAQ

Quick answers to questions that come up in the first hour of using
Tokie. For architecture and internals, read
[docs/ARCHITECTURE.md](./ARCHITECTURE.md). For writing connectors and
wiring the MCP server into your agent, read
[docs/CONNECTORS.md](./CONNECTORS.md).

---

## Installation & setup

### Why `pip install tokie-cli`, not `tokie`?

The bare `tokie` slot on PyPI is squatted by an unrelated tokenizer
from before this project existed. The installed command is still
`tokie` — the distribution name is the only difference.

### What does `tokie init` actually do?

It creates `~/.config/tokie/tokie.toml` with sensible defaults, a
database path under `~/.local/share/tokie/tokie.db`, and a plans
template with a handful of subscriptions. No data is collected, no
network calls are made. You can delete the file and re-run `init`
safely — your database is untouched.

### Where does Tokie store its files?

- **Config:** `$XDG_CONFIG_HOME/tokie/tokie.toml` (or the platform
  equivalent).
- **Database:** `$XDG_DATA_HOME/tokie/tokie.db`.
- **Keyring entries:** OS keyring under the service name `tokie`. API
  keys and webhook URLs live here; never in `tokie.toml`.
- **Logs:** None by default. `tokie scan -vv` prints to stderr only.

Override with `TOKIE_HOME` if you want everything under one directory.

---

## Tracking & accuracy

### What counts as "exact" vs "estimated" vs "inferred"?

- **Exact (solid bar):** Tokens are reported by the vendor — either
  from a parseable local session log (Claude Code JSONL, Codex rollout
  JSONL) or from a vendor admin-usage endpoint (Anthropic usage
  report, OpenAI completions usage).
- **Estimated (diagonal stripes):** Tokie re-derives the token count
  from raw text it can see (e.g. a request/response log) using the
  model's tokenizer. Close enough to trust, not exact.
- **Inferred (dashed outline):** The tool is web-only and you logged
  usage manually (`tokie scan --collector manual`). Tokie never
  invents data here; the bar shows exactly what you entered.

Tokie never silently averages tiers. If you've got `exact` and
`inferred` numbers for the same plan, the aggregator presents both.

### Why is my Claude Pro rolling-5h window half full when I've barely used it?

Because it's rolling. The bar shows what you've used in the trailing 5
hours, not what you've used since the current reset. If Claude
reported a usage spike at `T-4h`, that volume is still inside the
window until `T+1h`. This matches Anthropic's own dashboard behaviour.

### Tokie says I have `0 events` but I know I used Claude Code today.

Run `tokie doctor` first. If `claude-code` shows `detected: no`, Tokie
can't see `~/.claude/projects/`. Common causes:

- You're running Tokie as a different user than the one that uses
  Claude Code.
- You have Claude Code's telemetry off and are using an older version
  that doesn't write JSONL files. Upgrade and retry.
- Your `HOME` is set oddly (e.g. `sudo` shells). Use `tokie doctor
  --json` to see the absolute path Tokie is probing.

If detected but no events after `tokie scan`, check the file permissions.
Tokie reads only — if the files are mode 0400 owned by root, even
`tokie scan --collector claude-code -vv` will show zero parses.

---

## Alerts

### How do the default 75 / 95 / 100% thresholds dedupe?

Dedupe key is `(plan_id, account_id, window_type, threshold,
reset_epoch)`. Once a threshold fires for a given reset cycle, it
cannot fire again until the window resets. So the 95% desktop
notification for Claude Pro's weekly window fires once per week, not
once every minute you stay above 95%.

### How do I disable a threshold?

Remove its entry from `tokie.toml` under `[[thresholds]]`, or edit
inline in the dashboard (the threshold editor POSTs back to the same
file). To disable alerts entirely, set `alerts_desktop_enabled =
false` and remove any `[[webhooks]]` blocks.

### Where do webhook secrets live?

In the OS keyring, not `tokie.toml`. When you run `tokie webhook add`,
you're prompted for the URL and it's stored under the keyring service
`tokie` with a key derived from the webhook id. `tokie.toml` only
stores the id, never the URL itself.

---

## Dashboard & MCP

### Is the dashboard safe to expose on my LAN?

**No.** Tokie has no auth layer. The default bind is `127.0.0.1`, and
`tokie dashboard --remote` explicitly opts in to binding `0.0.0.0`
with a visible warning. If you need shared access, put it behind an
authenticating reverse proxy (e.g. Caddy + basic auth, Tailscale
Funnel + oauth).

### What does the MCP server let an agent do?

Four read-only tools:

- `list_subscriptions` — every configured subscription with current
  saturation.
- `get_usage` — aggregated tokens/messages/cost, filterable.
- `get_remaining` — per-window remaining capacity.
- `suggest_tool` — deterministic recommendation for a task id.

The MCP server never writes to your config, database, or keyring, and
it never makes network calls. See [CONNECTORS.md](./CONNECTORS.md)
for Claude Desktop / Cursor / Codex setup.

### Why is the MCP server an optional extra?

The `mcp` Python SDK drags in a non-trivial dependency tree. Users
who only want the CLI + dashboard shouldn't pay for it. Install with
`pip install 'tokie-cli[mcp]'` or `uv tool install 'tokie-cli[mcp]'`.

---

## Extending Tokie

### Can I write a connector without forking?

Yes. Publish a package that registers a `tokie.collectors` entry
point and Tokie discovers it on the next run. Start from
`templates/tokie-connector-example/` — it's a working minimal
package with contract tests wired up. Full workflow in
[CONNECTORS.md](./CONNECTORS.md).

### Is there a typed SDK for talking to Tokie's data?

Yes — `from tokie_cli.dashboard.aggregator import build_payload`.
Passing it `(bindings, plans, events)` gives you the same
`DashboardPayload` the dashboard renders. If you want JSON over
stdio, use the MCP server.

### How do I pin a specific vendor plan definition?

`plans.yaml` lives inside the wheel, so pinning the `tokie-cli`
version pins the catalog. If you need to override one plan locally,
point Tokie at your own file via `TOKIE_PLANS_PATH`. `tokie doctor`
will show where the loaded plans came from.

---

## Data & privacy

### Does Tokie send anything off-device?

No. No telemetry, no crash reports, no "anonymous usage stats." The
only outbound traffic comes from vendor admin-usage endpoints *you*
configure (to pull your own billing data) and from webhooks *you*
configure (to send alerts to your own Slack/Discord).

### How do I wipe Tokie's state?

```bash
# Linux/macOS
rm -rf ~/.local/share/tokie   # tokie.db + WAL
rm -rf ~/.config/tokie        # tokie.toml

# Windows
Remove-Item -Recurse $env:LOCALAPPDATA\tokie
Remove-Item -Recurse $env:APPDATA\tokie
```

To also clear stored webhook URLs / API keys from the OS keyring,
delete any entries under the service name `tokie` via your keyring
GUI (Keychain Access on macOS, Credential Manager on Windows, Seahorse
on Linux). Re-running `tokie init` gives you a clean install.

### Will Tokie ever become a hosted service?

Not on the roadmap. The architecture is deliberately local-first (see
[ARCHITECTURE.md](./ARCHITECTURE.md)): your prompt metadata, session
files, and API keys never need to cross a network boundary for Tokie
to work. If that changes, it will be opt-in and never default.

---

## Got a question not covered here?

Open an issue: <https://github.com/vamshivittali76/Tokie/issues>.
