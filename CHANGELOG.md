# Changelog

## [Unreleased]

### Added

- `get_review_package` MCP tool for a compact read-only changed-file review index without full diffs or full file contents.
- `get_changed_file_diff` MCP tool for one bounded targeted changed-file diff with staged, unstaged, untracked, and auto source modes.

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
