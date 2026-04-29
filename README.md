# local-codex-bridge

A small local bridge for using **local Codex CLI** as the implementation worker while ChatGPT remains the planner/reviewer.

The bridge is intentionally project-agnostic. You define project profiles in a TOML config, then expose a small tool surface:

- list configured projects
- inspect git status
- start a local Codex task in a chosen project
- read task logs
- inspect git diff
- run allowlisted verification commands
- abort a running task

## Why this exists

Cloud Codex through Linear/GitHub may not expose or pin the model you want. This bridge keeps execution local, so you can run your local Codex setup with the model/config you choose, while ChatGPT can still issue instructions and read results.

## Important safety model

This bridge can run local Codex against your repos. Treat it as powerful.

Default safety choices:

- only configured project directories are accessible
- no arbitrary shell tool is exposed
- verification commands must be allowlisted per project
- Codex is launched as a subprocess without `shell=True`
- task logs are stored locally under `task_dir`
- git commit/push tools are deliberately not included in v0

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

Example VHF profile is included.

## Run locally

```bash
local-codex-bridge serve --config ~/.local-codex-bridge/config.toml
```

Default bind address is `127.0.0.1:8765`.

## Expose to ChatGPT

ChatGPT custom MCP connectors require a remote HTTPS endpoint; local MCP servers are not directly supported.

Use a secure tunnel, for example Cloudflare Tunnel:

```bash
cloudflared tunnel --url http://127.0.0.1:8765
```

Then add the resulting HTTPS URL to ChatGPT Developer Mode / custom MCP connector settings.

Use access control. Do not expose this unauthenticated on the public internet.

## Tool flow

1. ChatGPT calls `list_projects`.
2. ChatGPT calls `get_project_status`.
3. ChatGPT calls `start_codex_task` with a bounded prompt.
4. ChatGPT polls `get_task`.
5. ChatGPT calls `get_git_diff`.
6. ChatGPT asks for verification through `run_verification`.
7. You review/merge/push according to your repo policy.

## First VHF prompt pattern

```text
Work in project `vhf`.

Use the current repo state.
Read AGENTS.md and the relevant docs named in the task.
Do not broaden scope.
Make the smallest safe implementation.
Run verification.
Return exact files changed, verification output, risks, and next action.
```
