# Local Codex Bridge

[English](README.md) | [简体中文](docs/README.zh-CN.md)

Local Codex Bridge is a lightweight local MCP bridge for ChatGPT ↔ Codex workflows. It lets ChatGPT start and inspect **local Codex CLI** tasks against configured local repository profiles without giving ChatGPT broad local machine authority.

It is designed for workflows where cloud Codex is not the right executor because you want local repository access, local git remotes, and an operator-controlled Codex model such as `gpt-5.5` when your local Codex CLI supports it.

Local Codex Bridge is independent and project-agnostic. It does not assume a downstream project, a required PR workflow, or a personal engineering methodology.

## Product layers

LCB should read as a bridge first:

- **Core bridge** is the default mental model: project profiles, local Codex task execution, task logs, repo status, changed-file inspection, and allowlisted verification. You can use only this layer.
- **Controlled actions** are optional bridge-owned mutation tools for users who want LCB to handle branch creation, commit/push, PR creation, PR merge, or post-merge local sync. The capabilities are optional, but their runtime safety gates are mandatory when used because they protect real authority boundaries.
- **Engineering-control workflow** is optional guidance for stricter ChatGPT ↔ Codex loops: review contracts, readiness checks, evidence-first review, human approval gates, and operating checklists. It is not required for lightweight bridge usage.

More detail:

- [Product shape](docs/PRODUCT_SHAPE.md) — product-boundary source of truth.
- [Engineering-control workflow](docs/ENGINEERING_CONTROL.md) — optional strict workflow guidance.
- [Full controlled-loop smoke runbook](docs/FULL_LOOP_SMOKE.md) — optional maintainer release-smoke guidance for the complete controlled loop.
- [Tool profiles design](docs/TOOL_PROFILES.md) — design-only notes for possible future runtime profiles; not implemented today.

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
  -> reviews evidence before accepting authority-changing steps

ChatGPT
  -> connects through a custom MCP connector
  -> verifies project profile, git status, HEAD, and remotes
  -> starts bounded local Codex tasks
  -> reads task logs, changed-file evidence, diffs, and verification output

Local Codex Bridge
  -> runs on the operator's machine
  -> exposes configured project profiles and allowlisted operations
  -> invokes local Codex CLI inside the selected repo
  -> optionally performs controlled branch, commit/push, PR, merge, and local sync actions when explicitly used

Local Codex CLI
  -> runs with the local model/config chosen by the operator
  -> edits the local project repo when instructed
  -> returns implementation output and verification evidence

GitHub or another VCS host
  -> remains the durable repo truth for commits, PRs, diffs, and landed state
```

## Tool surface

The current runtime exposes the following tools. Runtime profiles are not implemented yet; see [docs/TOOL_PROFILES.md](docs/TOOL_PROFILES.md) for design-only profile notes.

### Core bridge tools

These tools are enough to use LCB as a lightweight bridge:

- `list_projects` — list configured project profiles.
- `get_project_status` — report git status, HEAD, remotes, and Codex CLI preflight for a project.
- `check_codex_cli` — report the configured Codex executable, PATH lookup, version check, launch cwd, and remediation hint.
- `start_codex_task` — start `codex exec` in a configured project. Optional `review_contract` appends bridge-owned guidance for concise review-oriented summaries.
- `get_task` — read task metadata and stdout/stderr tails.
- `list_tasks` — list recent bridge task records.
- `abort_task` — terminate a running local Codex process.
- `git_get_branch_status` — report current branch, dirty state, HEAD, remotes, upstream, and ahead/behind evidence.
- `get_git_diff` — inspect git status, unstaged/staged diffs, and bounded untracked file previews.
- `get_review_package` — return a compact read-only changed-file index with status/stat evidence, without full diffs or full file contents.
- `get_changed_file_diff` — return one bounded targeted diff for a changed/staged/untracked file.
- `get_changed_file_text` — return bounded UTF-8 text for one changed/staged/untracked file.
- `run_verification` — run an allowlisted verification command.
- `run_verification_bundle` — run multiple configured verification keys sequentially with bounded per-command evidence.

### Optional controlled action tools

These tools mutate authority-bearing state. They are optional capabilities, but their safety gates are not optional when called:

- `git_create_work_branch` — create and switch to a new local work branch from an existing local base branch.
- `git_commit_and_push` — after human approval, stage approved files, create one commit, and push it to the current branch on `origin`.
- `github_create_pr` — create a GitHub pull request for an already-pushed current branch via the installed `gh` CLI.
- `github_merge_pr` — after human approval, merge one ready GitHub pull request via fixed `gh pr merge` argv.
- `git_sync_local_branch_to_origin` — after review, sync a clean local target branch to its local `origin/<target>` ref without fetching, pulling, pushing, merging, or mutating PRs.

### Optional engineering-control / readiness helpers

These tools and options support stricter review loops but do not make that workflow mandatory:

- `start_codex_task` with `review_contract: true` — asks Codex for concise implementation summaries instead of full diffs or full file contents. This is behavior guidance, not a security boundary.
- `get_acceptance_readiness` — read-only preflight for whether the current repo state appears ready for a human-approved `git_commit_and_push`.
- `github_get_pr_status` — read GitHub pull request status/evidence plus conservative advisory PR-only readiness evidence via the installed `gh` CLI.
- `get_pr_sync_readiness` — read-only advisory evidence combining PR readiness with local target-branch sync readiness.

The bridge does **not** expose arbitrary shell execution in v0. Verification commands are allowlisted per project. Bridge-owned Git/GitHub tools use fixed argv and structured `blocked_*` diagnostics for unsafe input or state.

The GitHub PR tools use `gh` as an external substrate. Local Codex Bridge does not implement native GitHub API/token handling and does not store, print, or manage GitHub tokens.

Optional workflow guidance is not a security boundary. Runtime safety gates on mutation tools are security-relevant and remain enforced even if you are not following the engineering-control workflow.

## Requirements

- macOS, Linux, or another environment that can run Python and Codex CLI.
- Python 3.11+.
- Local OpenAI Codex CLI installed and authenticated.
- A local git repository you want Codex to work in.
- Optional for GitHub PR tools: GitHub CLI `gh` installed and authenticated for `github.com`.
- A tunnel provider such as ngrok or Cloudflare Tunnel if you want ChatGPT to connect from the web.
- ChatGPT custom MCP connector access.

Tunnels are external deployment layers. They are not Python runtime dependencies of Local Codex Bridge.

## Authentication status

Local Codex Bridge has first-class auth configuration and fails closed for public-style no-auth deployments. The default `auth.mode = "auto"` permits no-auth only for loopback local development with no `server.public_base_url`. Explicit `auth.mode = "disabled"` is also loopback-only.

The recommended public ChatGPT-compatible mode is `auth.mode = "oidc_proxy"`, using FastMCP built-in OIDC proxy auth. Set `server.public_base_url` to your real public HTTPS tunnel/domain without `/mcp`, then use `{public_base_url}/mcp` as the ChatGPT connector URL and `{public_base_url}/auth/callback` as the IdP redirect URI. OIDC scopes are non-secret config and default narrowly to `["openid"]`; add `email` and/or `profile` only if your provider policy requires those claim scopes. OIDC client ID and client secret must come from environment variables. `example.com` and `YOUR-...` values in docs are placeholders and do not exist.

`auth.mode = "static_bearer"` is also available via a token stored in an environment variable such as `LCB_AUTH_TOKEN`. This is for local/internal/test clients that can send `Authorization: Bearer ...`; it is not the recommended public ChatGPT custom MCP path. Do not use query-string tokens. Cloudflare Tunnel and ngrok are transport only; LCB auth is the security boundary. See [docs/AUTH.md](docs/AUTH.md).


## 1. Install Local Codex Bridge

For a pinned release install, use the GitHub tag. This is the recommended user install path for the current tagged release.

With `pipx`:

```bash
pipx install "git+https://github.com/coolsidsudo/local-codex-bridge.git@v0.3.4"
local-codex-bridge --help
```

With `uv`:

```bash
uv tool install "git+https://github.com/coolsidsudo/local-codex-bridge.git@v0.3.4"
local-codex-bridge --help
```

Tag installs do not automatically create `~/.local-codex-bridge/config.toml`. Current tagged installs include `local-codex-bridge init`, which is the recommended setup path. If your installed version does not have `init`, use the manual `config.example.toml` fallback below.

For contributor/development work, use a clone and editable install:

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

Recommended for versions that include the init wizard:

```bash
local-codex-bridge init --config ~/.local-codex-bridge/config.toml
local-codex-bridge doctor --config ~/.local-codex-bridge/config.toml
```

`init` writes a project-agnostic config step by step, keeps server defaults loopback-local, asks for one project profile, and only allowlists a safe `git_status` verification command by default. It does not start MCP, run Codex, contact your OIDC provider, collect secrets, or write token/client-secret values to TOML. For OIDC or static bearer auth it writes environment variable names only and prints placeholder export examples.

Advanced/manual fallback, including for `v0.1.0` installs:

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
# Use an absolute path here if the bridge process PATH cannot find codex.
codex_bin = "codex"
default_model = "gpt-5.5"
default_codex_args = ["--json"]

[projects.my_project]
name = "My Project"
path = "~/Projects/my-project"
default_model = "gpt-5.5"
# Optional per-project override:
# codex_bin = "/absolute/path/to/codex"

[projects.my_project.verification]
git_status = ["git", "status", "--short", "--branch"]
test = ["python3", "-m", "pytest"]
```

Codex executable resolution is project override, non-default server `codex_bin`,
`LCB_CODEX_BIN`, then bridge-process `PATH` lookup for the default `codex`.
`doctor`, `get_project_status`, and `check_codex_cli` show the bridge process `PATH`,
launch cwd, resolved executable, and `codex --version` result.

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

## 4. Check auth/setup with doctor

Before starting the server, run:

```bash
local-codex-bridge doctor --config ~/.local-codex-bridge/config.toml
```

`doctor` validates the config without starting MCP, running Codex, or contacting your identity provider. For `auth.mode = "oidc_proxy"`, it prints the ChatGPT connector URL, IdP redirect URI, provider config URL, configured OIDC scope names, and whether the configured OIDC credential environment variables are set. It prints environment variable names and non-secret scope names only, never bearer tokens, OIDC client IDs, or OIDC client secrets.

## 5. Run the bridge locally

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

## 6. Test the local MCP endpoint

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

## 7. Install and configure ngrok

ngrok is one temporary/dev tunnel option. Cloudflare Tunnel is also a preferred/future deployment option for many setups. Tunnel clients are external deployment tools; Local Codex Bridge does not depend on `ngrok`, `cloudflared`, or any tunnel provider package at Python runtime.

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

## 8. Start an HTTPS tunnel

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

If you use Cloudflare Tunnel or another provider, treat it as transport only: point it at the same local bridge endpoint, normally `http://127.0.0.1:8765`. For public ChatGPT work, configure LCB `auth.mode = "oidc_proxy"`; LCB auth is the security boundary. For a stable Cloudflare Tunnel setup, see [docs/CLOUDFLARE_TUNNEL.md](docs/CLOUDFLARE_TUNNEL.md).

## 9. Test the tunnel endpoint

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

## 10. What auth values go where

For `auth.mode = "oidc_proxy"`:

- `server.public_base_url`: your real HTTPS tunnel/domain, without `/mcp`.
- ChatGPT connector URL: `{public_base_url}/mcp`.
- IdP redirect URI: `{public_base_url}/auth/callback`.
- `oidc_scopes`: non-secret OIDC scopes; defaults to `["openid"]`. Add `email` and/or `profile` only if your provider policy requires those claim scopes.
- Env vars: OIDC client ID and client secret.

For verified ChatGPT + Cloudflare Access OIDC setup and troubleshooting notes, see [docs/AUTH.md](docs/AUTH.md) and [docs/CLOUDFLARE_TUNNEL.md](docs/CLOUDFLARE_TUNNEL.md).

`example.com` domains and `YOUR-...` values are placeholders. They do not exist; replace them with your real values.

## 11. Add the bridge as a ChatGPT custom MCP connector

In ChatGPT web:

1. Open **Settings**.
2. Go to **Apps & Connectors**.
3. Open **Advanced settings**.
4. Enable **Developer mode** if needed.
5. Go back to **Apps & Connectors**.
6. Create a new custom connector.
7. Use a name such as `Local Codex Bridge`.
8. Set the MCP server URL to your current tunnel URL plus `/mcp`:

```text
https://example-name.ngrok-free.dev/mcp
```

9. For public ChatGPT-compatible deployment, use `auth.mode = "oidc_proxy"`. Static bearer is local/internal/test only.
10. Save/connect the connector.

After connecting, ChatGPT settings should show the bridge actions, including:

```text
list_projects
get_project_status
check_codex_cli
start_codex_task
get_task
list_tasks
abort_task
get_review_package
get_changed_file_diff
get_changed_file_text
get_git_diff
git_get_branch_status
git_create_work_branch
get_acceptance_readiness
run_verification
run_verification_bundle
git_commit_and_push
github_create_pr
github_get_pr_status
github_merge_pr
get_pr_sync_readiness
git_sync_local_branch_to_origin
```

ChatGPT-side developer MCP errors such as `FORBIDDEN: This conversation does not support developer MCPs` are platform/conversation gating. Local Codex Bridge cannot guarantee that bridge-code changes will enable developer MCPs for a gated ChatGPT conversation.

## 12. Select the connector in a chat

In a new or refreshed ChatGPT chat:

1. Open the `+` menu beside the message box.
2. Open **More** if needed.
3. Select **Local Codex Bridge**.

Then ask:

```text
Use Local Codex Bridge and list configured projects.
```

A healthy response should show your configured project profiles.

## 13. Smoke test a project without editing files

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

## 14. Optional engineering-control workflow

For teams or operators who want a stricter ChatGPT ↔ Codex loop, see [docs/ENGINEERING_CONTROL.md](docs/ENGINEERING_CONTROL.md). That document contains the evidence-first review posture, readiness guidance, and operating checklist that used to make this README heavier.

You do not need that workflow to use LCB as a lightweight bridge. If you do use controlled mutation tools, their runtime safety gates still apply regardless of workflow style.

## 15. Common issues

### `curl /mcp` returns 406 or 400

This is usually fine. The MCP endpoint is alive, but curl is not a complete MCP client.

### ChatGPT settings show the connector, but the chat cannot use it

Try a new chat after connecting the app. Then select the connector from the `+` menu.

If ChatGPT reports `FORBIDDEN: This conversation does not support developer MCPs`, treat it as platform/conversation gating. Try a supported ChatGPT surface/conversation with developer MCP access; bridge code cannot guarantee a fix for that platform gate.

### `gpt-5.5` requires a newer Codex version

Upgrade Codex CLI and retry.

### Codex stderr shows unrelated MCP token errors

Your local Codex config may contain an MCP server with an expired token. That is usually separate from this bridge unless the task requires that MCP server.

### Worktree is dirty before a task

Stop and inspect before starting Codex. The bridge intentionally makes dirty state visible so the reviewer can avoid mixing unrelated changes.

### `git_commit_and_push` returns `blocked_*`

Read the structured diagnostics. Common causes include an empty file list, blank commit message, a non-`origin` remote, a branch mismatch, path escaping the project root, unapproved pre-staged files, or staged files that do not exactly match the approved file list. Inspect git state before retrying, especially if a failed operation may have left approved changes staged.

## 16. Security notes

This bridge can cause local Codex to modify files in configured repositories. When optional controlled action tools are explicitly used, it can also create local work branches, commit and push approved files, create GitHub PRs, merge approved PRs, and sync a local target branch to `origin/<target>`. Treat it as powerful local automation.

Recommended defaults:

- Bind the bridge to `127.0.0.1`.
- Do not expose it publicly without LCB auth configured; tunnels are transport only and are not the security boundary.
- Configure only repos you are willing to let ChatGPT/Codex work on.
- Keep verification commands allowlisted.
- Review staged/unstaged diffs, bounded untracked previews, and verification output before accepting changes.
- Use mutation tools only after explicit human approval of the exact operation, files, message, PR, merge method, or sync target involved.
- Use `git_commit_and_push` only for reviewed and approved files.
- Do not pass secrets in prompts.
- Do not publish temporary tunnel URLs, auth env vars, or bearer tokens in public issues or docs.
- Do not add arbitrary shell execution unless you fully understand the risk.

See [`docs/SECURITY.md`](docs/SECURITY.md) for more detail.

## Development

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
python3 -m compileall src
python3 -m pytest
```

## Status

Research-preview local workflow utility. Use carefully.
