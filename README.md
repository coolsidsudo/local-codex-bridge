# Local Codex Bridge

Local Codex Bridge is a small, project-profile-based MCP server that lets ChatGPT start and inspect **local Codex CLI** tasks on your own machine.

It is designed for workflows where cloud Codex is not the right executor because you want local repository access, local git remotes, and an operator-controlled Codex model such as `gpt-5.5` when your local Codex CLI supports it.

## Route

```text
Human
  -> starts the bridge server and secure HTTPS tunnel once per work session

ChatGPT
  -> connects through a custom MCP connector
  -> verifies project profile, git status, HEAD, and remotes
  -> starts bounded local Codex tasks
  -> reads task logs, diffs, and verification output
  -> reviews the result

Local Codex Bridge
  -> runs on the operator's machine
  -> exposes configured project profiles and allowlisted operations
  -> invokes local Codex CLI inside the selected repo

Local Codex CLI
  -> runs with the local model/config chosen by the operator
  -> edits the local project repo when instructed
  -> returns implementation output and verification evidence

GitHub or another VCS host
  -> remains the durable repo truth for commits, PRs, diffs, and landed state
```

## What the bridge exposes

The initial tool surface is intentionally conservative:

- `list_projects`
- `get_project_status`
- `start_codex_task`
- `get_task`
- `list_tasks`
- `abort_task`
- `get_git_diff`
- `run_verification`

The bridge does **not** expose arbitrary shell execution in v0. Verification commands are allowlisted per project.

## Install

```bash
git clone https://github.com/coolsidsudo/local-codex-bridge.git
cd local-codex-bridge

python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Verify local Codex is installed and logged in:

```bash
codex --version
codex exec -m gpt-5.5 "Print exactly: local codex bridge readiness ok"
```

If your Codex CLI does not support `gpt-5.5`, use the model id supported by your local installation.

## Configure

```bash
mkdir -p ~/.local-codex-bridge
cp config.example.toml ~/.local-codex-bridge/config.toml
$EDITOR ~/.local-codex-bridge/config.toml
```

Add project profiles under `[projects.<id>]`. Example:

```toml
[projects.my_project]
name = "My Project"
path = "~/Projects/my-project"
default_model = "gpt-5.5"

[projects.my_project.verification]
git_status = ["git", "status", "--short", "--branch"]
test = ["python", "-m", "pytest"]
```

## Run locally

```bash
local-codex-bridge serve --config ~/.local-codex-bridge/config.toml
```

By default the server binds to:

```text
http://127.0.0.1:8765/mcp
```

## Expose to ChatGPT

ChatGPT custom MCP connectors require a remote HTTPS endpoint. A local-only URL such as `http://127.0.0.1:8765/mcp` is not enough.

For a quick private proof, use a tunnel such as ngrok or Cloudflare Tunnel:

```bash
ngrok http 8765
```

Then register the HTTPS `/mcp` URL as a custom MCP connector in ChatGPT developer mode.

Do not publish temporary tunnel URLs in repo docs or issues. Treat them as session-local operational details.

## Safety notes

This bridge can cause local Codex to modify files in configured repositories. Treat it as powerful local automation.

Recommended defaults:

- Bind the bridge to `127.0.0.1`.
- Expose it only through an authenticated or private tunnel.
- Configure only repos you are willing to let ChatGPT/Codex work on.
- Keep verification commands allowlisted.
- Review diffs before committing, pushing, or merging.
- Do not pass secrets in prompts.

See [`docs/SECURITY.md`](docs/SECURITY.md) for more detail.

## Development

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check .
```

## Status

Research-preview local workflow utility. Use carefully.
