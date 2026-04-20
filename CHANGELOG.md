# Changelog

All notable changes to Tokie will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Project scaffold: `pyproject.toml`, `ruff`/`mypy` config, CI matrix.
- `src/tokie_cli/schema.py`: canonical `UsageEvent`, `Subscription`, `LimitWindow` Pydantic models.
- `scripts/v00_discover.py`: Phase 0 throwaway parser for Claude Code JSONL logs.

### Notes
- PyPI package is `tokie-cli` because `tokie` is squatted by an unrelated tokenizer. Python import name is `tokie_cli`; shipped CLI command remains `tokie`.

## [0.1.0] - TBD (Week 1 Friday)

See [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) for the full roadmap.
