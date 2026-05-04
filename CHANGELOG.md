# Changelog

## [Unreleased]

### Added

- `get_review_package` MCP tool for a compact read-only changed-file review index without full diffs or full file contents.
- `get_changed_file_diff` MCP tool for one bounded targeted changed-file diff with staged, unstaged, untracked, and auto source modes.
- `get_changed_file_text` MCP tool for bounded UTF-8 text from one currently changed/staged/untracked file.
- `run_verification_bundle` MCP tool for sequential execution of configured verification keys with bounded per-command evidence; runtime argv remains fixed and allowlisted, while command side effects are those configured by the operator.
- `get_acceptance_readiness` MCP tool for read-only preflight evidence before a human-approved `git_commit_and_push`.
- `get_pr_sync_readiness` MCP tool for conservative advisory PR merge-consideration and local target-branch sync readiness evidence.
- `git_sync_local_branch_to_origin` MCP tool for narrow post-merge local target-branch sync to local `origin/<target>` refs.
- Optional `start_codex_task` review contract guidance for concise Codex implementation summaries.
- Normalized conservative advisory PR-only readiness evidence in `github_get_pr_status`.

## [0.2.0] - 2026-05-02

### Added

- `local-codex-bridge init` interactive setup wizard for generating a safe starter config and directing users to `doctor`.

### Fixed

- Piped `local-codex-bridge init --dry-run` now keeps generated TOML on stdout while prompts go to stderr.

### Known limitations

- No native OAuth server.
- No npm wrapper.
- No PyPI publication yet.
- No PR/merge tools yet.

## [0.1.0] - 2026-05-01

Initial release-readiness baseline for the first tagged Local Codex Bridge release.
Replace `Unreleased` with the actual release date before tagging.

### Added

- Local MCP bridge with project profiles for configured local repositories.
- Bounded local Codex task start/inspect flow.
- Allowlisted verification commands per project profile.
- Controlled `git_commit_and_push` acceptance operation for human-approved files.
- `static_bearer` auth mode for local/internal/test clients.
- OIDC proxy auth mode for public ChatGPT-compatible connector use.
- `doctor` diagnostics for setup/auth checks without starting MCP or printing secrets.
- Cloudflare Tunnel operational guide.

### Security

- Fail-closed auth configuration for public-style no-auth deployments.
- Loopback-only no-auth defaults for `auto` and `disabled` auth modes.
- Environment-variable indirection for bearer tokens and OIDC client credentials.
- Project-profile roots remain the filesystem trust boundary.
- Verification commands remain allowlisted and run without arbitrary shell execution.
- Controlled git acceptance checks approved files, branch state, staged files, and remote constraints before commit/push.

### Known limitations

- No init wizard yet.
- No native OAuth server.
- No npm wrapper.
- No PyPI publication yet.
- No PR/merge tools yet.
