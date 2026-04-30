# Local Codex Bridge

[English](README.md) | [简体中文](docs/README.zh-CN.md)

Local Codex Bridge is a small, project-profile-based MCP server that lets ChatGPT start and inspect **local Codex CLI** tasks on your own machine.

It is designed for workflows where cloud Codex is not the right executor because you want local repository access, local git remotes, and an operator-controlled Codex model such as `gpt-5.5` when your local Codex CLI supports it.

## Why this exists

Some cloud task routes hide or auto-route the model, may not have a working PR/push path, and may force the human to relay prompts and results manually. Local Codex Bridge keeps the executor local:

```text
ChatGPT -> MCP connector -> HTTPS tunnel -> Local Codex Bridge -> local Codex CLI -> local repo
```

That gives ChatGPT a narrow, inspectable tool surface while keeping Codex execution on the operator's machine.

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

## Tool surface

The initial tool surface is intentionally conservative:

- `list_projects` — list configured project profiles.
- `get_project_status` — report git status, HEAD, and remotes for a project.
- `start_codex_task` — start `codex exec` in a configured project.
- `get_task` — read task metadata and stdout/stderr tails.
- `list_tasks` — list recent bridge task records.
- `abort_task` — terminate a running local Codex process.
- `get_git_diff` — inspect git status, diff stat, and diff.
- `run_verification` — run an allowlisted verification command.

The bridge does **not** expose arbitrary shell execution in v0. Verification commands are allowlisted per project.

## Requirements

- macOS, Linux, or another environment that can run Python and Codex CLI.
- Python 3.11+.
- Local OpenAI Codex CLI installed and authenticated.
- A local git repository you want Codex to work in.
- A tunnel provider such as ngrok or Cloudflare Tunnel if you want ChatGPT to connect from the web.
- ChatGPT custom MCP connector access.

## 1. Install Local Codex Bridge

```bash
git clone https://github.com/coolsidsudo/local-codex-bridge.git
cd local-codex-bridge

python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
```

Check that the command exists:

```bash
local-codex-bridge --help
```

## 2. Verify local Codex CLI and model

```bash
codex --version
codex exec -m gpt-5.5 "Print exactly: local codex bridge readiness ok"
```

Expected result should include a Codex run and the final line:

```text
local codex bridge readiness ok
```

If your Codex CLI says `gpt-5.5` requires a newer version, upgrade Codex CLI, then retry:

```bash
codex --upgrade
hash -r
codex --version
codex exec -m gpt-5.5 "Print exactly: local codex bridge readiness ok"
```

If `codex --upgrade` is unavailable or does not update, use your platform's Codex install/update path. For npm installs, that is commonly:

```bash
npm install -g @openai/codex@latest
hash -r
codex --version
```

If your local Codex CLI does not support `gpt-5.5`, use the exact model id your installation supports and set that in the bridge config.

## 3. Configure project profiles

Create a config file:

```bash
mkdir -p ~/.local-codex-bridge
cp config.example.toml ~/.local-codex-bridge/config.toml
$EDITOR ~/.local-codex-bridge/config.toml
```

Example generic profile:

```toml
[server]
host = "127.0.0.1"
port = 8765
task_dir = "~/.local-codex-bridge/tasks"
codex_bin = "codex"
default_model = "gpt-5.5"
default_codex_args = ["--json"]

[projects.my_project]
name = "My Project"
path = "~/Projects/my-project"
default_model = "gpt-5.5"

[projects.my_project.verification]
git_status = ["git", "status", "--short", "--branch"]
test = ["python", "-m", "pytest"]
```

Example docs-site profile:

```toml
[projects.docs_site]
name = "Docs Site"
path = "~/Projects/docs-site"
default_model = "gpt-5.5"

[projects.docs_site.verification]
git_status = ["git", "status", "--short", "--branch"]
build = ["npm", "run", "build"]
```

Use one project profile per repo. The bridge is intentionally generic; do not hardcode project-specific behavior into bridge source code.

## 4. Run the bridge locally

In terminal 1:

```bash
cd ~/Projects/local-codex-bridge
source .venv/bin/activate
local-codex-bridge serve --config ~/.local-codex-bridge/config.toml
```

The server should report something like:

```text
Starting MCP server 'Local Codex Bridge' with transport 'streamable-http'
on http://127.0.0.1:8765/mcp
```

Keep this terminal open.

## 5. Test the local MCP endpoint

In another terminal:

```bash
curl -i http://127.0.0.1:8765/mcp
```

A plain curl request may return `406 Not Acceptable` or a similar MCP protocol response. That is normal; plain curl is not a full MCP client. The important signal is that the server responds and logs the request.

A common successful reachability response looks like:

```text
HTTP/1.1 406 Not Acceptable
...
{"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,"message":"Not Acceptable: Client must accept text/event-stream"}}
```

## 6. Install and configure ngrok

Install ngrok with Homebrew on macOS:

```bash
brew install ngrok/ngrok/ngrok
ngrok version
```

Create or log into an ngrok account, then add your authtoken:

```bash
ngrok config add-authtoken YOUR_NGROK_TOKEN
```

The command should report something like:

```text
Authtoken saved to configuration file: /Users/<you>/Library/Application Support/ngrok/ngrok.yml
```

## 7. Start an HTTPS tunnel

In terminal 2, while the bridge server is still running:

```bash
ngrok http 8765
```

ngrok should print a forwarding URL:

```text
Forwarding  https://example-name.ngrok-free.dev -> http://localhost:8765
```

Your MCP endpoint is the forwarding URL plus `/mcp`:

```text
https://example-name.ngrok-free.dev/mcp
```

Do **not** commit this URL to a repo. Free ngrok URLs are often temporary and should be treated as session-local operational details.

## 8. Test the ngrok endpoint

In a third terminal:

```bash
curl -i -H "Accept: text/event-stream" https://example-name.ngrok-free.dev/mcp
```

A response like this is fine:

```text
HTTP/2 400
...
{"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,"message":"Bad Request: Missing session ID"}}
```

That means the HTTPS tunnel reaches the MCP server. A real MCP client will manage session setup; curl does not.

## 9. Add the bridge as a ChatGPT custom MCP connector

In ChatGPT web:

1. Open **Settings**.
2. Go to **Apps & Connectors**.
3. Open **Advanced settings**.
4. Enable **Developer mode** if needed.
5. Go back to **Apps & Connectors**.
6. Create a new custom connector.
7. Use a name such as `Local Codex Bridge`.
8. Set the MCP server URL to your current ngrok URL plus `/mcp`:

```text
https://example-name.ngrok-free.dev/mcp
```

9. Use no authentication for a first local proof, or put the tunnel behind access control for serious use.
10. Save/connect the connector.

After connecting, ChatGPT settings should show the bridge actions, including:

```text
list_projects
get_project_status
start_codex_task
get_task
list_tasks
abort_task
get_git_diff
run_verification
```

## 10. Select the connector in a chat

In a new or refreshed ChatGPT chat:

1. Open the `+` menu beside the message box.
2. Open **More** if needed.
3. Select **Local Codex Bridge**.

Then ask:

```text
Use Local Codex Bridge and list configured projects.
```

A healthy response should show your configured project profiles.

## 11. Smoke test a project without editing files

Ask ChatGPT to run this sequence through the bridge:

```text
Use Local Codex Bridge.

Smoke test only. Do not edit files.

1. Call list_projects.
2. Call get_project_status for project_id `my_project`.
3. Call run_verification for project_id `my_project`, command_key `git_status`.
4. If the worktree is dirty, stop.
5. If the worktree is clean, start one local Codex task with a no-edit prompt:

   Read the repo root and report the current directory, git branch, visible Codex run metadata, and final status `bridge_smoke_test_completed`. Do not edit files.

6. Poll get_task until it exits.
7. Call get_git_diff and confirm no files changed.
```


## 12. Normal operating checklist

Before starting real implementation work:

```text
1. Start local-codex-bridge serve.
2. Start ngrok http 8765.
3. Refresh the ChatGPT connector URL if the ngrok URL changed.
4. Select Local Codex Bridge in the chat.
5. Run list_projects.
6. Run get_project_status for the target project.
7. Run git_status verification.
8. Confirm branch, HEAD, remote, and clean worktree.
9. Start a bounded local Codex task.
10. Review stdout/stderr, git diff, and verification before accepting anything.
```

## 13. Common issues

### `curl /mcp` returns 406 or 400

This is usually fine. The MCP endpoint is alive, but curl is not a complete MCP client.

### ChatGPT settings show the connector, but the chat cannot use it

Try a new chat after connecting the app. Then select the connector from the `+` menu.

### `gpt-5.5` requires a newer Codex version

Upgrade Codex CLI and retry.

### Codex stderr shows unrelated MCP token errors

Your local Codex config may contain an MCP server with an expired token. That is usually separate from this bridge unless the task requires that MCP server.

### Worktree is dirty before a task

Stop and inspect before starting Codex. The bridge intentionally makes dirty state visible so the reviewer can avoid mixing unrelated changes.

## 14. Security notes

This bridge can cause local Codex to modify files in configured repositories. Treat it as powerful local automation.

Recommended defaults:

- Bind the bridge to `127.0.0.1`.
- Expose it only through an authenticated or private tunnel.
- Configure only repos you are willing to let ChatGPT/Codex work on.
- Keep verification commands allowlisted.
- Review diffs before committing, pushing, or merging.
- Do not pass secrets in prompts.
- Do not publish temporary tunnel URLs in public issues or docs.
- Do not add arbitrary shell execution unless you fully understand the risk.

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
