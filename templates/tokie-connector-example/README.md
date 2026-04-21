# tokie-connector-example

A minimal, copy-and-paste-ready starter for building a third-party
Tokie connector.

Drop this directory somewhere, rename `acme_connector` / `acme-connector`
to your vendor name, fill in the collector, publish, and users can
`pip install` it to light up your tool inside `tokie doctor`, `tokie
scan`, and `tokie suggest` automatically.

## What you get

```
tokie-connector-example/
├── pyproject.toml                # Registers the tokie.collectors entry point.
├── README.md                     # This file.
├── src/acme_connector/
│   ├── __init__.py
│   └── collector.py              # Your Collector subclass lives here.
└── tests/
    └── test_contract.py          # Uses tokie_cli.testing helpers.
```

## The moving parts

1. **`src/acme_connector/collector.py`** implements a
   `tokie_cli.collectors.Collector` subclass. The example emits a
   single synthetic event so you can run `tokie scan --collector acme`
   end-to-end before you wire up a real data source.

2. **`pyproject.toml`** declares two things:
   * `dependencies = ["tokie-cli>=0.4"]` so `Collector` resolves.
   * An entry point under `tokie.collectors`:

     ```toml
     [project.entry-points."tokie.collectors"]
     acme = "acme_connector.collector:AcmeCollector"
     ```

   That entry point is the *only* wiring required. Once your package
   is installed, Tokie discovers it automatically via
   `importlib.metadata` — no patches to Tokie itself.

3. **`tests/test_contract.py`** pulls in `tokie_cli.testing` and runs
   the full contract battery against your collector. Your CI will fail
   loudly if you ever break the `UsageEvent` schema, the idempotency
   guarantee, or the class-level metadata. Install dev deps with
   `pip install -e ".[dev]"` and run `pytest`.

## Rename checklist

Before publishing:

- [ ] Rename the package dir (`src/acme_connector/` → `src/<you>_connector/`).
- [ ] Update `pyproject.toml` `name`, `description`, and the entry point
      value (both the key `acme` and the `acme_connector.collector:...`
      target).
- [ ] Set `AcmeCollector.name` to your vendor's canonical slug — the
      same string `tokie doctor` will print.
- [ ] Replace the synthetic event in `scan()` with real reads from your
      source (log file, API endpoint, whatever).
- [ ] Update `README.md` to describe what the real connector does.

## Next steps

- Read `docs/CONNECTORS.md` in the main Tokie repo for the full design
  rationale (log tailing vs. API polling, confidence tiers, etc.).
- Run `pytest` — the bundled contract test imports your collector via
  the installed entry point, exactly like Tokie does at runtime.
- When you're ready, publish to PyPI with `uv build && uv publish`.
