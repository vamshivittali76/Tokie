# Security policy

## Supported versions

| Version | Supported |
|---------|-----------|
| `0.1.x` | Yes — current pre-alpha line. |
| `< 0.1` | No. |

A v1.0.0 release will introduce a formal support matrix. Until then, every
new minor version obsoletes the previous one.

## How Tokie treats your data

Tokie is a local-first tool. The promises below are *contract* — if any one of
them breaks, that is a security bug and we want to hear about it.

- **Nothing leaves your machine by default.** No telemetry, no crash pings,
  no anonymized metrics. The dashboard binds `127.0.0.1:7878`. Non-loopback
  binds require the explicit `--remote` flag *and* print an amber warning
  that Tokie has no auth layer yet.
- **Secrets live in your OS keyring.** API keys, admin tokens, webhook
  secrets all go through `keyring`. They are never written to `tokie.toml`,
  never printed in logs, and never embedded in the dashboard HTML.
  `tokie doctor` is contract-tested to never leak keyring values even when
  every collector is configured.
- **No prompt content in logs.** Every collector's `source` field and every
  log line strips prompt and completion text. Only filenames, line numbers,
  and exception class names are logged.
- **Config files use `0600` permissions on POSIX.** On Windows we rely on
  the default per-user profile ACL.
- **No auto-updates, no phone-home.** Tokie does not check for new versions
  on startup. Updating is always opt-in via `uv tool upgrade tokie-cli`.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security reports.

1. Email the maintainers at `security@tokie.dev` *(coming with v1.0 — until
   then use a private GitHub Security Advisory).*
2. Include: the version of Tokie, the affected command or endpoint, a
   minimal reproduction, and the impact you observed.
3. You will get an acknowledgement within **72 hours**. We aim to have a
   patch released within **14 days** for high-severity issues.

We treat any of the following as a security issue:

- An output path through which Tokie could exfiltrate DB contents, prompts,
  keyring values, or environment variables.
- A way to trick the `manual` collector (or any other) into writing attacker-
  controlled data into `tokie.db`.
- Dashboard endpoints that accept user input and reflect it without escaping.
- A missing guard around the non-loopback bind warning.

Thank you for helping keep Tokie trustworthy.
