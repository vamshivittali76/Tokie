# Release runbook

The release flow is **fully automated** via GitHub Actions Trusted Publishing
— there is no `PYPI_TOKEN` stored in this repo or in any secret. Each release
exchanges a short-lived OIDC token for PyPI credentials on every run.

## One-time setup (per PyPI project)

Do this once for `pypi.org` and once for `test.pypi.org`. After the first
publish the project already exists and Trusted Publishing can be attached
directly to the existing project. Before the first publish use the
**pending publisher** flow below.

### 1. Create Trusted Publishers (pending flow — first release only)

1. Sign in to https://pypi.org/manage/account/publishing/ (and repeat for https://test.pypi.org/manage/account/publishing/).
2. Click **Add a pending publisher** and fill in:
   - PyPI Project Name: `tokie-cli`
   - Owner: `vamshivittali76`
   - Repository name: `Tokie`
   - Workflow name: `release.yml` (for PyPI) or `dryrun-testpypi.yml` (for TestPyPI dry-runs)
   - Environment name: `pypi` (for PyPI) or `testpypi` (for TestPyPI)
3. Repeat on TestPyPI with the same values but environment `testpypi`.

### 2. Create GitHub Environments

On https://github.com/vamshivittali76/Tokie/settings/environments, create:

- `testpypi` — no required reviewers (runs automatically).
- `pypi` — add yourself as a **required reviewer** so the PyPI publish step pauses for you to approve before it actually uploads.

### 3. Done

The workflow permissions (`id-token: write` on each publish job) are already
declared in `.github/workflows/release.yml`. No other secrets, no API tokens,
nothing to rotate.

## Dry-run a build to TestPyPI (anytime)

1. Go to **Actions -> Dry-run to TestPyPI -> Run workflow**.
2. Optional: set `local_version_suffix` to `.dev1` / `rc1` / similar so you
   don't consume your real version slot on TestPyPI.
3. Workflow builds the wheel+sdist, pip-installs it into a clean venv, runs
   `tokie version --json` to confirm the entry point works, and uploads to
   TestPyPI.
4. Verify end-to-end install on your machine:

   ```bash
   uv tool install --index-url https://test.pypi.org/simple/ \
                   --extra-index-url https://pypi.org/simple/ \
                   tokie-cli
   tokie version
   tokie init
   tokie dashboard --no-open
   ```

## Cut a real release

```bash
# 1. Finalize CHANGELOG.md — move items out of [Unreleased] under the new version.
# 2. Bump version in pyproject.toml (must match the tag without the leading 'v').
# 3. Commit the version bump.
git add pyproject.toml CHANGELOG.md
git commit -m "Release v0.1.0"
git push origin main

# 4. Tag and push the tag.
git tag -a v0.1.0 -m "Tokie v0.1.0"
git push origin v0.1.0
```

The `release.yml` workflow then:

1. **Build** — runs `uv build`, verifies the tag matches `pyproject.toml`
   version, and smoke-tests the wheel in a clean venv. If the tag does not
   match the pyproject version the build fails fast and nothing is
   published.
2. **Publish to TestPyPI** — runs automatically in the `testpypi`
   environment via OIDC.
3. **Publish to PyPI** — runs in the `pypi` environment. **Pauses for your
   manual approval** if you've added yourself as a required reviewer.
4. **Create GitHub Release** — auto-generates release notes from commits
   since the previous tag, attaches the wheel + sdist, and marks the
   release as pre-release if the version contains `a`, `b`, or `rc`.

## Rollback

You cannot delete a PyPI release. If a release is broken:

- Yank it: https://pypi.org/manage/project/tokie-cli/releases/ -> **Yank release**. Yanked releases stay resolvable for pinned installs but are excluded from default `pip install`.
- Ship a fix forward as the next patch version.

## Version scheme

Tokie uses [Semantic Versioning](https://semver.org). During the v0.x line:

- `0.x.y` — minor version bumps are allowed to break CLI flags and config
  schemas. The CHANGELOG must call out every break.
- `v1.0.0` — stabilizes the CLI surface, the `UsageEvent` schema, the
  collector SDK, and the MCP server contract. From there on SemVer applies
  strictly.
