# Extending Tokie: Connectors & MCP

Tokie is designed to be extended without forking. There are two supported
extension points:

1. **Connectors** — new data sources plugged in via the
   `tokie.collectors` entry-point group.
2. **MCP server** — the `tokie mcp serve` stdio server exposes the same
   usage + routing intelligence to LLM agents (Claude Code, Cursor,
   Codex) as structured tools.

This document covers both. If you only want to hand your agent a read-only
view of your subscription state, skip to [Wiring Tokie into your agent
(MCP)](#wiring-tokie-into-your-agent-mcp).

---

## Writing a new connector

A connector is a subclass of `tokie_cli.collectors.base.Collector` that
knows how to turn "a log file, API, or trace store somewhere on this
machine" into a stream of `UsageEvent` records. Tokie dedupes events by
their `raw_hash` on ingest, so connectors can be re-run safely.

### The contract, in one paragraph

A valid `Collector`:

- Declares a **class-level `name`** (used in `tokie scan`, logs, and the
  dashboard).
- Declares a **class-level `default_confidence`** drawn from
  `tokie_cli.schema.Confidence` — use `EXACT` only when the source gives
  you vendor-reported tokens, otherwise `ESTIMATED` or `INFERRED`.
- Implements a classmethod `detect() -> bool` that is **fast and side-effect-free**
  (no network, no writes). Tokie calls it on every `tokie doctor` run.
- Implements `async def scan(self, since: datetime | None) -> AsyncIterator[UsageEvent]`
  that yields every event from the source since `since`. **Must be
  idempotent** — the same inputs must produce events with the same
  `raw_hash` on a rescan, or Tokie will count usage twice.

That's it. The default `watch()` polls `scan()` on a cursor; override it
only if your source has a better notification channel (inotify, fsevents,
websocket).

### Fastest path: copy the bundled template

`templates/tokie-connector-example/` in this repo is a working starter
package. It ships:

- A minimal `AcmeCollector` that demonstrates `detect`, `scan`, and
  `make_event` usage.
- A `pyproject.toml` with the correct `tokie.collectors` entry point.
- Contract tests that use the `pytest-tokie-connector` plugin bundled
  with `tokie-cli`.

Copy the directory, rename `acme_connector` -> your vendor name, and
replace the fake `scan()` body with your source-specific parsing.

### Registering via entry points

The one piece you cannot skip is the entry point. Add to your
`pyproject.toml`:

```toml
[project.entry-points."tokie.collectors"]
my_vendor = "my_tokie_connector:MyCollector"
```

On install, Tokie's registry discovers your collector automatically. No
core changes, no PR to Tokie required. Built-ins win on name collision
(so you can't accidentally shadow `claude-code`), but the collision is
logged visibly so you know to pick a different name.

You can confirm discovery with:

```bash
python -c "from tokie_cli.collectors import load_registry; print(sorted(load_registry()))"
```

### Testing: `pytest-tokie-connector`

`tokie-cli` installs a pytest plugin under the `pytest11` entry point
group. Once you have `tokie-cli` and `pytest` in your dev deps, you get:

- `assert_collector_contract(cls)` — structural checks: class
  attributes, method signatures, ABC compliance.
- `assert_event_is_valid(event)` — schema checks for a single event
  (non-negative tokens, known confidence, etc.).
- `assert_scan_yields_valid_events(collector, min_events=...)` — end-to-end
  check that `scan()` produces valid events.
- `assert_idempotent_rescan(factory)` — runs `scan()` twice and compares
  `raw_hash` sets to catch dedup bugs early.

Example:

```python
from tokie_cli.testing import (
    assert_collector_contract,
    assert_idempotent_rescan,
    assert_scan_yields_valid_events,
)

from my_tokie_connector import MyCollector


def test_collector_meets_contract() -> None:
    assert_collector_contract(MyCollector)


def test_scan_returns_events(tmp_fixture) -> None:
    assert_scan_yields_valid_events(MyCollector(tmp_fixture), min_events=1)


def test_rescan_is_idempotent(tmp_fixture) -> None:
    assert_idempotent_rescan(lambda: MyCollector(tmp_fixture))
```

If `assert_collector_contract` raises `ContractViolationError`, read the
message — it names the exact attribute or method that's missing or
malformed.

### What a good connector looks like

- **Ingests from local state**, not a paid third-party API, unless the
  vendor explicitly exposes a usage-reporting endpoint.
- **Stable `raw_hash`.** Derive it from fields the source will never
  rewrite (timestamp + message id + model), not from anything formatted
  for display.
- **Cheap `detect()`.** Check a file path or env var, not a network call.
- **Honest confidence.** Prefer `INFERRED` over claiming `EXACT` — Tokie's
  threshold engine deliberately discounts low-confidence events when
  it's close to firing an alert.

---

## Wiring Tokie into your agent (MCP)

Tokie ships an optional [Model Context Protocol](https://modelcontextprotocol.io)
server. Once you start it, any MCP-capable agent can ask Tokie four
questions in a structured way:

| Tool                 | What it returns                                                                |
|----------------------|--------------------------------------------------------------------------------|
| `list_subscriptions` | Every configured subscription with current saturation + reset times.           |
| `get_usage`          | Aggregated tokens / messages / cost, optionally filtered by plan or account.   |
| `get_remaining`      | Remaining capacity per window (daily / weekly / 5h) for each subscription.     |
| `suggest_tool`       | Deterministic recommendation for a task id, plus auto-handoff if over limit.   |

All tools are read-only. The MCP server never writes to `tokie.toml`,
`tokie.db`, or your keyring.

### Install + smoke-test

```bash
pip install 'tokie-cli[mcp]'
tokie mcp tools         # prints the tool catalog as JSON
tokie mcp serve         # starts the stdio server (blocks on stdin)
```

If you see `The 'mcp' Python package is not installed`, you skipped the
`[mcp]` extra — install it and retry.

### Claude Desktop / Claude Code

Add Tokie to your Claude MCP config (typically
`~/Library/Application Support/Claude/claude_desktop_config.json` on
macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "tokie": {
      "command": "tokie",
      "args": ["mcp", "serve"]
    }
  }
}
```

Restart Claude. In the prompt, type `@tokie` — the four tools should
appear in the tool menu. Ask it _"what subscriptions am I close to
exhausting today?"_ and confirm it calls `list_subscriptions` rather
than guessing.

### Cursor

Cursor reads MCP servers from `~/.cursor/mcp.json` (or the per-project
`.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "tokie": {
      "command": "tokie",
      "args": ["mcp", "serve"]
    }
  }
}
```

Reload the window. In the Cursor agent chat, the `tokie` server appears
under "Available tools". You can scope it per-workspace by dropping the
same file in your project root.

### Codex CLI

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.tokie]
command = "tokie"
args = ["mcp", "serve"]
```

### Troubleshooting

- **Agent says "server didn't respond":** Run `tokie mcp serve` in a
  terminal and confirm it blocks without printing anything. Any
  stderr output (other than logging) means the server is crashing
  during startup — usually a missing `tokie.toml` from a fresh
  install. Run `tokie init` first.
- **Tools show up but return empty results:** Check
  `tokie status` outside the agent. An empty DB means no scan has run —
  trigger one with `tokie scan --all`.
- **Permissions errors on Windows:** Use an absolute path in `command`
  (e.g. `C:\\Users\\you\\.venvs\\tokie\\Scripts\\tokie.exe`) if `tokie`
  isn't on the system `PATH` the agent inherits.

### Security notes

The MCP server is **read-only and local-only** by design:

- It binds to stdio, not a TCP port. Only the process that spawned it
  can talk to it.
- Every tool re-reads the config + DB on each call. There is no
  long-lived privileged state for an agent to corrupt.
- No tool writes, sends webhooks, or hits a vendor API. If you want to
  expose write operations to an agent, build a thin wrapper server of
  your own and keep this one unchanged.
