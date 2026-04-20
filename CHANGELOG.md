# Changelog

All notable changes to Tokie will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Project scaffold: `pyproject.toml`, `ruff`/`mypy` config, CI matrix.
- `src/tokie_cli/schema.py`: canonical `UsageEvent`, `Subscription`, `LimitWindow` Pydantic models.
- `src/tokie_cli/db.py`: SQLite persistence with idempotent schema-v1 migration, `insert_event` + `insert_events` with `raw_hash` dedup, filter-composable `query_events`. No ORM.
- `src/tokie_cli/windows.py`: pure quota math — `window_bounds` / `next_reset_at` for rolling-5h / daily / weekly / monthly / none, `aggregate_events` with half-open intervals, `capacity` with "most constrained" basis selection.
- `src/tokie_cli/plans.yaml`: bundled catalog of 10 subscription templates (Claude Pro / Max5 / Max20, Anthropic API, OpenAI tiers 1-3, ChatGPT Plus, Cursor Pro, Perplexity Pro). Every entry cites a `source_url` for PR refresh.
- `src/tokie_cli/plans.py`: `PlanTemplate` + `load_plans` loader that validates every plan against the `Subscription` Pydantic contract.
- `scripts/v00_discover.py`: Phase 0 throwaway parser for Claude Code JSONL logs.
- Tests: 52 passing (schema 8, db 10+1 POSIX skip, windows 21, plans 13), full mypy --strict clean.

### Notes
- PyPI package is `tokie-cli` because `tokie` is squatted by an unrelated tokenizer. Python import name is `tokie_cli`; shipped CLI command remains `tokie`.

## [0.1.0] - TBD (Week 1 Friday)

See [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) for the full roadmap.
