# Local Codex Bridge

[English](README.md) | [简体中文](docs/README.zh-CN.md)

Local Codex Bridge is a small, project-profile-based MCP server that lets ChatGPT start and inspect **local Codex CLI** tasks on your own machine.

It is designed for workflows where cloud Codex is not the right executor because you want local repository access, local git remotes, and an operator-controlled Codex model such as `gpt-5.5` when your local Codex CLI supports it.

Local Codex Bridge is an independent, general-purpose developer MCP bridge. It does not assume any downstream project; it works with any configured local repository profile.

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
  -> reviews diffs and verification output before accepting changes

ChatGPT
  -> connects through a custom MCP connector
  -> verifies project profile, git status, HEAD, and remotes
  -> starts bounded local Codex tasks
  -> reads task logs, diffs, and verification output
  -> reviews the result and asks for human approval before acceptance commit/push

Local Codex Bridge
  -> runs on the operator's machine
  -> exposes configured project profiles and allowlisted operations
  -> invokes local Codex CLI inside the selected repo
  -> can create a controlled local work branch before edits
  -> can perform a controlled, human-approved git add/commit/push for approved files

Local Codex CLI
  -> runs with the local model/config chosen by the operator
  -> edits the local project repo when instructed
  -> returns implementation output and verification evidence

GitHub or another VCS host
  -> remains the durable repo truth for commits, PRs, diffs, and landed state
```

## Tool surface

The tool surface is intentionally conservative:

- `list_projects` — list configured project profiles.
- `get_project_status` — report git status, HEAD, and remotes for a project.
- `start_codex_task` — start `codex exec` in a configured project. Optional
  `review_contract` appends bridge-owned guidance asking Codex for concise
  implementation summaries instead of full diffs or full file contents.
- `get_task` — read task metadata and stdout/stderr tails.
- `list_tasks` — list recent bridge task records.
- `abort_task` — terminate a running local Codex process.
- `get_review_package` — return a compact read-only changed-file index with status/stat evidence, without full diffs or full file contents.
- `get_changed_file_diff` — return one bounded targeted diff for a changed/staged/untracked file after reviewing the package index.
- `get_changed_file_text` — return bounded UTF-8 text for one changed/staged/untracked file after targeted diff review.
- `get_git_diff` — inspect git status, unstaged/staged diffs, and bounded untracked file previews.
- `git_get_branch_status` — report current branch, dirty state, HEAD, remotes, upstream, and ahead/behind evidence.
- `git_create_work_branch` — create and switch to a new local work branch from an existing local base branch.
- `get_acceptance_readiness` — read-only preflight for whether the current repo state appears ready for a human-approved `git_commit_and_push`.
- `run_verification` — run an allowlisted verification command.
- `run_verification_bundle` — run multiple existing allowlisted verification commands sequentially with bounded per-command evidence.
- `git_commit_and_push` — after human approval, stage approved files, create one commit, and push it to the current branch on `origin`.
- `github_create_pr` — create a GitHub pull request for an already-pushed current branch via the installed `gh` CLI.
- `github_get_pr_status` — read GitHub pull request status/evidence plus normalized read-only PR readiness evidence via the installed `gh` CLI.
- `get_pr_sync_readiness` — read-only evidence for PR merge consideration and local target-branch sync readiness.

The bridge does **not** expose arbitrary shell execution in v0. Verification commands are allowlisted per project. `git_create_work_branch` and `git_commit_and_push` are bridge-owned Git operations, not general shell or filesystem tools.

The GitHub PR tools use `gh` as an external substrate. Local Codex Bridge does not implement native GitHub API/token handling and does not store, print, or manage GitHub tokens.

The `start_codex_task` review contract is behavior guidance only, not a security boundary. ChatGPT should review actual repository state through `get_review_package`, `get_changed_file_diff`, `get_changed_file_text`, and `run_verification` / `run_verification_bundle` rather than trusting pasted diffs or file contents in Codex output.

## Controlled branch workflow

`git_create_work_branch` is intended to move a clean configured repo onto a safe feature/work branch before Codex edits begin. Its safeguards include:

- It accepts only local branch names and does not hard-code `main` as a universal base.
- If `base_branch` is omitted, it uses the current checked-out branch.
- `base_branch` must be an existing local branch; remote-style bases such as `origin/main`, full refs such as `refs/heads/main`, and `HEAD` are refused.
- The target branch must not already exist; switching existing branches is deferred.
- The worktree must be clean according to `git status --porcelain=v1 --untracked-files=normal`.
- Detached HEAD is refused.
- Branch names must pass conservative Local Codex Bridge validation and `git check-ref-format --branch`.
- It creates and checks out the new branch locally, but does not push, merge, delete branches, create PRs, or touch tags.

## Controlled acceptance flow

The intended acceptance workflow is:

```text
ChatGPT plans/reviews
  -> local Codex CLI edits a configured repo
  -> ChatGPT reviews the package index, targeted diffs, and verification output
  -> ChatGPT checks read-only acceptance readiness for the approved file set
  -> human accepts
  -> Local Codex Bridge performs controlled git add/commit/push
```

`git_commit_and_push` should only be called after the human has reviewed the exact diff and verification evidence from `get_git_diff` and `run_verification` or `run_verification_bundle`. `get_acceptance_readiness` is a read-only preflight for the approved file set: it reports current branch/HEAD/remotes, staged/unstaged/untracked files, approved-file coverage, origin/upstream evidence when available, and whether `git_commit_and_push` would likely block. It does not stage, commit, push, fetch, mutate branches, create PRs, or touch tags/releases. `run_verification_bundle` is read-only orchestration over existing configured verification keys: it accepts keys only, runs their allowlisted argv arrays sequentially with `shell=False`, and returns bounded per-command stdout/stderr evidence. For a first-pass review index, `get_review_package` reports branch/HEAD/remotes, status/stat evidence, changed-file classifications, and bounded untracked preview metadata without returning full diffs or full file contents. `get_changed_file_diff` is a read-only follow-up for one changed/staged/untracked path; it uses targeted fixed git commands, refuses unsafe paths and binary content, and bounds output. `get_changed_file_text` is a further read-only follow-up for bounded UTF-8 content of one currently changed/staged/untracked file; it refuses unchanged paths, deleted/no-content paths, binary/invalid UTF-8 content, unsafe paths, directories, symlinks, and non-regular files. `get_git_diff` distinguishes unstaged and staged changes and includes bounded text previews for untracked files when safe. Its safeguards include:

- Only configured project roots are accessible.
- No arbitrary shell execution is exposed.
- Verification commands remain allowlisted per project.
- Remote is currently limited to `origin`.
- If `branch` is omitted, the bridge uses the current checked-out branch.
- If `branch` is provided, it must match the current checked-out branch.
- Approved file paths are normalized to repo-relative paths and must stay inside the configured project root.
- Git staging uses literal pathspec handling so pathspec magic such as `:(glob)*` is not expanded.
- Modified, newly added, and deleted files are supported.
- Unapproved pre-staged files are refused.
- Staged files must exactly match the approved file list before a commit is created.
- Unsafe input or state returns structured `blocked_*` diagnostics with useful git evidence where relevant.

## Controlled GitHub PR workflow

`github_create_pr` is intended for the post-push review step. It creates a pull request only when the configured repo has an `origin` remote on `github.com`, the current branch is a normal local branch, the worktree is clean, and the current branch already exists on `origin` at the exact local `HEAD`.

Safeguards:

- PR operations use fixed `git` and `gh` argv with `shell=False`; there is no arbitrary `gh` passthrough.
- `gh --version` and `gh auth status -h github.com` must succeed.
- Supported public remotes are common HTTPS and SSH `github.com/OWNER/REPO` forms.
- If `base_branch` is omitted, the bridge reads the repository default branch from `gh repo view`; it does not hard-code `main`.
- Explicit `base_branch` values are conservatively validated and must exist on `origin`.
- PR creation is refused from the GitHub default branch or when the current branch equals the selected base branch.
- Unpublished branches and remote SHA mismatches are refused; C2 does not add push-upstream authority.
- If an open PR already exists for the current branch, the existing PR evidence is returned instead of creating a duplicate.
- New PRs are drafts by default; non-draft creation is allowed, but merge and auto-merge remain out of scope.

`github_get_pr_status` includes a compact normalized `pr_readiness` section with advisory, read-only PR evidence such as draft/open state, mergeability, review decision, checks, local branch/HEAD match, and local dirty state. It does not include target-branch sync readiness or suggested operator commands.

`get_pr_sync_readiness` is a read-only follow-up for the manual PR/acceptance tail. It combines `gh` PR evidence with local git evidence to report whether a PR appears ready for a human/operator to consider merging and whether a local target branch, by default `main`, appears safe to sync to `origin/<target>` using local refs only. It does not merge, auto-merge, mutate PRs, fetch, reset, switch, pull, push, delete branches, or touch tags/releases. Suggested operator commands, when present, are advisory text only and are not executed by the bridge.

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

The recommended public ChatGPT-compatible mode is `auth.mode = "oidc_proxy"`, using FastMCP built-in OIDC proxy auth. Set `server.public_base_url` to your real public HTTPS tunnel/domain without `/mcp`, then use `{public_base_url}/mcp` as the ChatGPT connector URL and `{public_base_url}/auth/callback` as the IdP redirect URI. OIDC client ID and client secret must come from environment variables. `example.com` and `YOUR-...` values in docs are placeholders and do not exist.

`auth.mode = "static_bearer"` is also available via a token stored in an environment variable such as `LCB_AUTH_TOKEN`. This is for local/internal/test clients that can send `Authorization: Bearer ...`; it is not the recommended public ChatGPT custom MCP path. Do not use query-string tokens. Cloudflare Tunnel and ngrok are transport only; LCB auth is the security boundary. See [docs/AUTH.md](docs/AUTH.md).


## 1. Install Local Codex Bridge

For a pinned release install, use the GitHub tag. This is the recommended user install path for the current tagged release.

With `pipx`:

```bash
pipx install "git+https://github.com/coolsidsudo/local-codex-bridge.git@v0.2.0"
local-codex-bridge --help
```

With `uv`:

```bash
uv tool install "git+https://github.com/coolsidsudo/local-codex-bridge.git@v0.2.0"
local-codex-bridge --help
```

Tag installs do not automatically create `~/.local-codex-bridge/config.toml`. Versions `v0.2.0` and later include `local-codex-bridge init`, which is the recommended setup path. If your installed version does not have `init`, use the manual `config.example.toml` fallback below.

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

## 4. Check auth/setup with doctor

Before starting the server, run:

```bash
local-codex-bridge doctor --config ~/.local-codex-bridge/config.toml
```

`doctor` validates the config without starting MCP, running Codex, or contacting your identity provider. For `auth.mode = "oidc_proxy"`, it prints the ChatGPT connector URL, IdP redirect URI, provider config URL, and whether the configured OIDC credential environment variables are set. It prints environment variable names only, never bearer tokens, OIDC client IDs, or OIDC client secrets.

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
- Env vars: OIDC client ID and client secret.

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
get_pr_sync_readiness
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

## 14. Normal operating checklist

Before starting real implementation work:

```text
1. Start local-codex-bridge serve.
2. Start a secure HTTPS tunnel, for example ngrok http 8765.
3. Refresh the ChatGPT connector URL if the tunnel URL changed.
4. Select Local Codex Bridge in the chat.
5. Run list_projects.
6. Run get_project_status for the target project.
7. Run git_status verification.
8. Confirm branch, HEAD, remote, and clean worktree.
9. Start a bounded local Codex task, preferably with `review_contract: true` for concise review-oriented output.
10. Review stdout/stderr, `get_review_package`, targeted `get_changed_file_diff` / `get_changed_file_text` evidence as needed, and verification output.
11. If changes are not acceptable, ask Codex to revise or stop.
12. If changes are acceptable, explicitly approve the exact files and commit message.
13. Call git_commit_and_push only after human approval.
14. Confirm the returned branch, remote, commit, push output, and final status.
15. After PR creation and review, use get_pr_sync_readiness for read-only PR merge-consideration and local target sync evidence before any manual merge/sync commands.
```

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

This bridge can cause local Codex to modify files in configured repositories and can perform a controlled acceptance commit/push after explicit human approval. Treat it as powerful local automation.

Recommended defaults:

- Bind the bridge to `127.0.0.1`.
- Do not expose it publicly without LCB auth configured; tunnels are transport only and are not the security boundary.
- Configure only repos you are willing to let ChatGPT/Codex work on.
- Keep verification commands allowlisted.
- Review staged/unstaged diffs, bounded untracked previews, and verification output before accepting changes.
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
