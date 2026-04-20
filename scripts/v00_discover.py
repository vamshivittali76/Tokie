#!/usr/bin/env python3
"""Phase 0 / v0.0 discovery script.

Read every Claude Code JSONL session under ``~/.claude/projects/**/*.jsonl``,
sum tokens per calendar day, and print a table. Its only job is to confirm we
understand the real data shape before locking the Pydantic schema.

Usage::

    python scripts/v00_discover.py
    python scripts/v00_discover.py --root ~/.claude/projects
    python scripts/v00_discover.py --since 2026-04-01

Throwaway by design. Once a proper ``collectors/claude_code.py`` exists this
file can be deleted.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path


def parse_args() -> argparse.Namespace:
    default_root = Path.home() / ".claude" / "projects"
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=default_root, help="Claude projects dir")
    p.add_argument("--since", type=str, default=None, help="ISO date, e.g. 2026-04-01")
    return p.parse_args()


def extract_usage(record: dict) -> tuple[datetime, int, int, str] | None:
    """Pull (timestamp, input_tokens, output_tokens, model) from one JSONL row.

    Claude Code writes assistant turns with ``message.usage``. Other row types
    (user, tool_result, system) are ignored.
    """

    ts_raw = record.get("timestamp")
    msg = record.get("message") or {}
    usage = msg.get("usage") or {}
    if not ts_raw or not usage:
        return None

    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)

    input_tokens = int(usage.get("input_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    model = str(msg.get("model") or record.get("model") or "unknown")

    return ts, input_tokens + cache_read + cache_write, output_tokens, model


def main() -> None:
    args = parse_args()
    root: Path = args.root.expanduser()
    if not root.exists():
        raise SystemExit(f"No such directory: {root}")

    since: date | None = None
    if args.since:
        since = date.fromisoformat(args.since)

    daily: dict[date, dict[str, int]] = defaultdict(lambda: {"in": 0, "out": 0, "n": 0})
    models: set[str] = set()
    files = sorted(root.rglob("*.jsonl"))
    if not files:
        raise SystemExit(f"No .jsonl files under {root}")

    for path in files:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            extracted = extract_usage(rec)
            if not extracted:
                continue
            ts, tok_in, tok_out, model = extracted
            d = ts.astimezone(UTC).date()
            if since and d < since:
                continue
            daily[d]["in"] += tok_in
            daily[d]["out"] += tok_out
            daily[d]["n"] += 1
            models.add(model)

    print(f"Scanned {len(files)} files under {root}")
    print(f"Models seen: {sorted(models)}")
    print()
    print(f"{'date':<12} {'calls':>6} {'input':>12} {'output':>12} {'total':>12}")
    print("-" * 58)
    total_in = total_out = total_calls = 0
    for d in sorted(daily):
        row = daily[d]
        print(
            f"{d.isoformat():<12} "
            f"{row['n']:>6} "
            f"{row['in']:>12,} "
            f"{row['out']:>12,} "
            f"{row['in'] + row['out']:>12,}"
        )
        total_in += row["in"]
        total_out += row["out"]
        total_calls += row["n"]
    print("-" * 58)
    print(
        f"{'TOTAL':<12} {total_calls:>6} "
        f"{total_in:>12,} {total_out:>12,} {total_in + total_out:>12,}"
    )


if __name__ == "__main__":
    main()
