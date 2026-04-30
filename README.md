# local-codex-bridge

A small, project-agnostic local MCP bridge for using **local Codex CLI** as the implementation worker while ChatGPT remains the planner/reviewer.

Local Codex Bridge is an independent, general-purpose developer bridge. It does not assume any downstream project: you define configured project profiles in TOML, and the bridge works against those local repositories.

The exposed tool surface is intentionally small:

- list configured projects
- inspect git status
- start a local Codex task in a chosen project
- read task logs
- inspect git diff
- run allowlisted verification commands
- perform a controlled, human-approved `git_commit_and_push`
- abort a running task

## Why this exists

Cloud Codex through Linear/GitHub may not expose or pin the model you want. This bridge keeps execution local, so you can run your local Codex setup with the model/config you choose, while ChatGPT can still issue instructions and read results for any configured local repository.

## Important safety model

This bridge can run local Codex against configured repos and includes one controlled Git acceptance operation. Treat it as powerful.

Default safety choices:

- only configured project directories are accessible
- no arbitrary shell tool is exposed
- verification commands must be allowlisted per project
- Codex is launched as a subprocess without `shell=True`
- task logs are stored locally under `task_dir`
- `git_commit_and_push` is a bridge-owned operation intended only after human review and approval
- `git_commit_and_push` stages only explicitly approved files, rejects non-`origin` remotes, refuses branch mismatches, and verifies the staged file list before committing

Do not expose this server publicly without a tunnel/access-control layer such as Cloudflare Access, Tailscale, or another authenticated reverse proxy.

## Install

```bash
cd local-codex-bridge
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Verify local Codex is installed and logged in:

```bash
codex --version
codex exec -m gpt-5.5 "Print a one-line readiness check."
```

If your local Codex CLI does not support `gpt-5.5`, use the exact local model id that your Codex installation supports.

## Configure

```bash
mkdir -p ~/.local-codex-bridge
cp config.example.toml ~/.local-codex-bridge/config.toml
$EDITOR ~/.local-codex-bridge/config.toml
```

Configure one or more project profiles for the local repositories you want the bridge to support.

## Run locally

```bash
local-codex-bridge serve --config ~/.local-codex-bridge/config.toml
```

Default bind address is `127.0.0.1:8765`.

## Expose to ChatGPT

ChatGPT custom MCP connectors require a remote HTTPS endpoint; local MCP servers are not directly supported.

Tunnels are external deployment layers, not part of the Python bridge runtime. The bridge continues to serve MCP on the configured host/port, normally `127.0.0.1:8765`.

Temporary/dev tunnel options include ngrok. Cloudflare Tunnel is also a future/preferred deployment option for many setups. Do not expose this unauthenticated on the public internet; use access control appropriate to your tunnel or network layer.

ChatGPT-side developer MCP errors such as `FORBIDDEN: This conversation does not support developer MCPs` are platform/conversation gating. Local Codex Bridge cannot guarantee that bridge-code changes will fix those errors.

## Tool flow

1. ChatGPT calls `list_projects`.
2. ChatGPT calls `get_project_status`.
3. ChatGPT calls `start_codex_task` with a bounded prompt for a configured project.
4. ChatGPT polls `get_task`.
5. ChatGPT calls `get_git_diff`.
6. ChatGPT asks for verification through `run_verification`.
7. You review the diff and verification output.
8. After explicit human approval, ChatGPT calls `git_commit_and_push` with the approved file list, commit message, `remote="origin"`, and either no branch or the current checked-out branch.
9. The bridge stages only the approved files, verifies the staged file list, commits, and pushes to the current branch.

## Generic prompt pattern

```text
Work in the configured project `<project_id>`.

Use the current repo state.
Read AGENTS.md and relevant project docs.
Do not broaden scope.
Make the smallest safe implementation.
Run allowlisted verification.
Return exact files changed, verification output, risks, and next action.
Do not commit or push unless explicitly approved.
```
