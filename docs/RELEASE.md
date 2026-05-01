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

- Confirm version consistency across:
  - `pyproject.toml`
  - `src/local_codex_bridge/__init__.py`
  - `CHANGELOG.md`

- Confirm release notes do not include real tunnel URLs, credentials, tokens, client secrets, or downstream-project-specific assumptions.

## Verification

Run verification from a prepared Python 3.11 environment with the project and test dependencies installed:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
python -m pip install ruff
```

Then run the standard project checks from that active environment before tagging:

```bash
git diff --check
python -m compileall src
python -m pytest
ruff check .
```

`ruff check .` is required for CI/release readiness while the GitHub Actions workflow runs it.

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

1. Finalize `CHANGELOG.md` by replacing the placeholder date for the release.
2. Re-run verification from this checklist.
3. Create an annotated tag:

   ```bash
   git tag -a vX.Y.Z -m "Release vX.Y.Z"
   ```

4. Push the tag:

   ```bash
   git push origin vX.Y.Z
   ```

## GitHub release steps

1. Open the pushed tag on GitHub.
2. Draft a GitHub release for `vX.Y.Z`.
3. Include release notes with:
   - highlights
   - security posture
   - pinned GitHub install commands
   - known limitations
4. Do not add PyPI, npm, deployment, or tunnel-provider publishing steps unless a future release explicitly adds them.
5. Publish the GitHub release only after final verification and review.

## Post-release checks

- Install from the pushed tag with `pipx` or `uv tool` in a clean environment.
- Run:

  ```bash
  local-codex-bridge --help
  local-codex-bridge doctor --config ~/.local-codex-bridge/config.toml
  ```

- Confirm the GitHub release links to the intended tag and commit.
- Confirm documentation still describes Cloudflare Tunnel and ngrok as transport only, with LCB auth as the security boundary.

## Do not

- Do not publish to PyPI in this release-readiness slice.
- Do not publish an npm wrapper in this release-readiness slice.
- Do not add release upload, package publishing, deployment, tagging, or GitHub release automation to CI.
- Do not include real tunnel URLs, tokens, OIDC client IDs, OIDC client secrets, or service credentials in release notes or docs.
- Do not weaken auth defaults or change runtime/tool behavior as part of release documentation work.
- Do not add PR/merge tools, unrelated runtime behavior, or downstream-project-specific assumptions in this slice.
