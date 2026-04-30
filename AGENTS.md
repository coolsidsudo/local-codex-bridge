# AGENTS.md

Project instructions for Codex work in this repository.

## Project identity

- This repo is **Local Codex Bridge**.
- It is an independent, general-purpose developer MCP bridge.
- It must not assume any particular downstream project.
- It should support any configured local repository/project.

## Architecture

- The bridge serves MCP locally through the configured host and port.
- Local Codex CLI may edit configured project worktrees.
- Bridge-owned tools may inspect status, diffs, logs, and run allowlisted verification.
- Git commit and push must be controlled, explicit, and human-approved.
- Tunneling is an external deployment layer, not core bridge runtime.

## Safety rules

- Do not add arbitrary shell execution.
- Do not broaden filesystem access beyond configured project roots.
- Keep verification commands allowlisted.
- Use subprocess with `shell=False`.
- Treat configured project paths as trust boundaries.
- Prefer structured `blocked_*` responses over uncaught exceptions for user/tool input errors.
- Do not hide staged-state or git-state risks.

## Git operation rules

- Any commit/push tool must be project-generic.
- Do not hard-code a branch such as `main`.
- Inspect and report the current branch before pushing.
- Refuse branch mismatches.
- Stage only explicitly approved files.
- Verify staged files match approved files before committing.
- Support modified, added, and deleted files.
- Do not commit unapproved pre-staged files.

## Testing rules

- Add or update tests for behavior changes.
- Tests should use temporary generic repositories.
- Tests must not depend on user-specific paths or downstream projects.
- Run:
  - `python3 -m compileall src`
  - `python3 -m pytest`
- Report exact verification output.

## Documentation rules

- Keep docs project-agnostic.
- Document limitations honestly.
- ChatGPT-side developer MCP errors such as `FORBIDDEN: This conversation does not support developer MCPs` should be described as platform/conversation gating unless repo evidence proves otherwise.
- Mention ngrok and Cloudflare Tunnel only as external tunnel/deployment options, not Python runtime dependencies.

## Workflow rules

- Keep changes minimal and reviewable.
- Prefer small focused patches.
- Do not commit or push unless explicitly asked.
- Return files changed, summary, verification, risks, diff/stat evidence, and current git status.
