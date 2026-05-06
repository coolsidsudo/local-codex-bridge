# Changelog

## [Unreleased]

## [0.3.3] - 2026-05-06

### Documentation

- Added stability guidance for the verified ChatGPT custom MCP connector setup using Cloudflare Tunnel, Cloudflare Access, and `auth.mode = "oidc_proxy"`.
- Documented Cloudflare Access policy, OIDC SaaS app, connector URL, scope, and stale connector-auth checks from the verified end-to-end v0.3.2 setup.
- Clarified that this release makes no OAuth/OIDC runtime behavior changes.

## [0.3.2] - 2026-05-06

### Fixed

- Added an `auth.mode = "oidc_proxy"` compatibility response for `GET /.well-known/openid-configuration` so clients that probe OpenID Discovery can discover the existing FastMCP OAuth proxy endpoints. The response intentionally omits `jwks_uri` because LCB does not expose a public JWKS document for FastMCP-issued tokens.

## [0.3.1] - 2026-05-06

### Fixed

- Added configurable OIDC scopes for `auth.mode = "oidc_proxy"`, defaulting narrowly to `["openid"]`, and passed them to FastMCP `OIDCProxy` as `required_scopes` so OAuth/OIDC metadata and upstream authorization requests include the required OpenID scope.
- `doctor` and init-generated OIDC config now show/write non-secret OIDC scope names while continuing to keep OIDC credential values environment-only.

## [0.3.0] - 2026-05-05

### Added

- `git_get_branch_status` MCP tool for current branch, dirty state, HEAD, remotes, upstream, and ahead/behind evidence.
- `git_create_work_branch` MCP tool for narrow local work-branch creation with clean-worktree and branch-name safety gates.
- `get_review_package` MCP tool for a compact read-only changed-file review index without full diffs or full file contents.
- `get_changed_file_diff` MCP tool for one bounded targeted changed-file diff with staged, unstaged, untracked, and auto source modes.
- `get_changed_file_text` MCP tool for bounded UTF-8 text from one currently changed/staged/untracked file.
- `run_verification_bundle` MCP tool for sequential execution of configured verification keys with bounded per-command evidence; runtime argv remains fixed and allowlisted, while command side effects are those configured by the operator.
- `get_acceptance_readiness` MCP tool for read-only preflight evidence before a human-approved `git_commit_and_push`.
- `get_pr_sync_readiness` MCP tool for conservative advisory PR merge-consideration and local target-branch sync readiness evidence.
- `git_sync_local_branch_to_origin` MCP tool for narrow post-merge local target-branch sync to local `origin/<target>` refs.
- `github_create_pr` MCP tool for controlled GitHub PR creation through fixed `gh pr create` argv for already-pushed branches.
- `github_get_pr_status` MCP tool for GitHub PR status/evidence through fixed `gh` argv.
- `github_merge_pr` MCP tool for narrow human-approved GitHub PR merge execution through fixed `gh pr merge` argv with conservative fresh readiness gates.
- Optional `start_codex_task` review contract guidance for concise Codex implementation summaries.
- Normalized conservative advisory PR-only readiness evidence in `github_get_pr_status`.

### Changed

- README/docs restructuring around core bridge versus optional engineering control, keeping LCB framed as a lightweight bridge first.
- Expanded release-readiness docs, security wording, and full controlled-loop smoke guidance.
- Added optional engineering-control workflow guidance and design-only tool profile documentation.

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
