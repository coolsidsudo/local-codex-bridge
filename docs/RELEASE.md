# Release checklist

This checklist is for maintainers preparing a tagged Local Codex Bridge release. It is documentation only; do not tag, publish, or create a GitHub release unless you are intentionally performing a release.

## Preconditions

- Confirm the worktree is clean before starting release work:

  ```bash
  git status --short --branch
  ```

- Confirm you are on the intended release branch, normally `main` for tagged releases:

  ```bash
  git branch --show-current
  git rev-parse HEAD
  ```

- Confirm local `main` matches `origin/main` if the release is intended to tag the public mainline:

  ```bash
  git rev-list --left-right --count origin/main...main
  ```

- Confirm version consistency across:
  - `pyproject.toml`
  - `src/local_codex_bridge/__init__.py`
  - `CHANGELOG.md`
  - pinned install examples in `README.md` and `docs/README.zh-CN.md`

- Confirm release notes do not include real tunnel URLs, credentials, tokens, client secrets, or downstream-project-specific assumptions.

- Confirm runtime profiles are still documented as design-only unless a separate runtime-profile implementation has landed with tests and migration notes.

## Release-prep edits

For a normal `vX.Y.Z` release prep slice:

1. Update package version fields in `pyproject.toml` and `src/local_codex_bridge/__init__.py`.
2. Update pinned install examples in `README.md` and `docs/README.zh-CN.md` to `vX.Y.Z`.
3. Finalize `CHANGELOG.md` by moving current entries from `Unreleased` to `## [X.Y.Z] - YYYY-MM-DD` and adding a new empty `Unreleased` section above it.
4. Confirm `CHANGELOG.md` covers all user-visible tool, docs, safety, auth, and workflow changes since the previous tag.
5. Confirm security docs still describe mutation authority accurately: local Codex edits, branch creation, commit/push, PR creation, PR merge, and local post-merge sync.
6. If the release includes controlled-loop changes, run or review the optional [full controlled-loop smoke runbook](FULL_LOOP_SMOKE.md) in a disposable or intentionally prepared repository.

Do not combine release-prep edits with unrelated runtime behavior or downstream-project-specific changes.

## Verification

Run verification from a prepared Python 3.11 environment with the project and test dependencies installed:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
```

Then run the standard project checks from that active environment before tagging:

```bash
git diff --check
python -m compileall src
python -m pytest
```

This repository's CI is configured to install and run `ruff check .`. For release parity, run `ruff check .` when `ruff` is available in the release environment or after installing the same CI dependency:

```bash
python -m pip install ruff
ruff check .
```

## Install smoke checks

After the tag exists, test pinned installs in a fresh environment. Replace `vX.Y.Z` with the release tag.

```bash
pipx install "git+https://github.com/coolsidsudo/local-codex-bridge.git@vX.Y.Z"
local-codex-bridge --help
printf '\nrelease_smoke\nRelease Smoke\n~/Projects/release-smoke\n' | local-codex-bridge init --dry-run
pipx uninstall local-codex-bridge
```

```bash
uv tool install "git+https://github.com/coolsidsudo/local-codex-bridge.git@vX.Y.Z"
local-codex-bridge --help
printf '\nrelease_smoke\nRelease Smoke\n~/Projects/release-smoke\n' | local-codex-bridge init --dry-run
uv tool uninstall local-codex-bridge
```

For releases that include the init wizard, `local-codex-bridge init` is the recommended config creation path. Tag installs still do not create `~/.local-codex-bridge/config.toml` automatically; use `init` or copy `config.example.toml` from the repository checkout/source archive, then edit it for the operator's own project profiles and auth settings.

## Tagging steps

These are instructions only; do not run them during ordinary release-readiness work.

1. Re-run verification from this checklist on the exact commit to tag.
2. Create an annotated tag:

   ```bash
   git tag -a vX.Y.Z -m "Release vX.Y.Z"
   ```

3. Push the tag:

   ```bash
   git push origin vX.Y.Z
   ```

Do not move or recreate a published tag unless you are intentionally performing an exceptional maintainer recovery and have coordinated it outside this checklist.

## GitHub release steps

1. Open the pushed tag on GitHub.
2. Draft a GitHub release for `vX.Y.Z`.
3. Include release notes with:
   - highlights
   - tool-surface changes
   - security posture and authority boundaries
   - pinned GitHub install commands
   - known limitations
4. Do not add PyPI, npm, deployment, or tunnel-provider publishing steps unless a future release explicitly adds them.
5. Publish the GitHub release only after final verification and review.

## Post-release checks

- Confirm the GitHub release links to the intended tag and commit.
- Install from the pushed tag with `pipx` and `uv tool` in clean environments.
- Run:

  ```bash
  local-codex-bridge --help
  local-codex-bridge init --dry-run
  local-codex-bridge doctor --config ~/.local-codex-bridge/config.toml
  ```

- Confirm README install examples point to the released tag.
- Confirm documentation still describes Cloudflare Tunnel and ngrok as transport only, with LCB auth as the security boundary.
- Confirm `docs/TOOL_PROFILES.md` still says runtime profiles are design-only unless runtime profile code has actually landed.

## Do not

- Do not publish to PyPI in this release-readiness slice.
- Do not publish an npm wrapper in this release-readiness slice.
- Do not add release upload, package publishing, deployment, tagging, or GitHub release automation to CI.
- Do not include real tunnel URLs, tokens, OIDC client IDs, OIDC client secrets, or service credentials in release notes or docs.
- Do not weaken auth defaults or change runtime/tool behavior as part of release documentation work.
- Do not add unrelated runtime behavior, tool schema changes, or downstream-project-specific assumptions in a release-prep slice.
- Do not create tags, GitHub releases, commits, pushes, PRs, or merges unless the current task explicitly authorizes that release operation.
