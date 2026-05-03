# Security notes

This bridge can start local Codex against configured local repositories. That means it can indirectly modify files in any configured project. It can also create a controlled local work branch and perform a controlled Git acceptance commit/push after explicit human approval. Configure it conservatively.

Local Codex Bridge is project-agnostic: configured project roots are trust boundaries, and bridge behavior should not assume a particular downstream repository.

## Strong recommendations

- Bind to `127.0.0.1` for local development.
- For public ChatGPT use, configure `auth.mode = "oidc_proxy"`.
- Treat Cloudflare Tunnel, ngrok, and reverse proxies as transport only; LCB itself must enforce auth.
- Do not expose this server directly to the public internet without LCB auth; LCB auth is the security boundary.
- Configure only the repositories you are willing to let ChatGPT/Codex work on.
- Keep verification commands allowlisted.
- Do not add arbitrary shell execution unless you fully understand the risk.
- Use `get_review_package` for a compact read-only changed-file index before requesting targeted diffs or text.
- Review staged/unstaged diffs, bounded untracked previews, and verification output before accepting changes.
- Use `git_create_work_branch` only when the configured repo is clean and the intended base branch is clear.
- Use `git_commit_and_push` only after explicit human approval of the exact files and commit message.
- Use `github_create_pr` only after the current branch has already been intentionally pushed to `origin`.
- Keep secrets out of task prompts and logs.
- Do not publish temporary tunnel URLs in public issues or documentation.


## Built-in auth

LCB auth configuration is first-class:

- `auth.mode = "auto"` is the default and permits no-auth only for loopback hosts with no `server.public_base_url`.
- `auth.mode = "disabled"` is explicit no-auth and is also loopback-only with no `server.public_base_url`.
- `auth.mode = "static_bearer"` uses FastMCP's static bearer verifier with the token read from an environment variable such as `LCB_AUTH_TOKEN`. It is for local/internal/test clients only, not recommended public ChatGPT use.
- `auth.mode = "oidc_proxy"` uses FastMCP's OIDC proxy and is the recommended public ChatGPT-compatible mode.

For `oidc_proxy`, `server.public_base_url` must be the real HTTPS tunnel/domain without `/mcp`; ChatGPT uses `{public_base_url}/mcp`, and the IdP redirect URI is `{public_base_url}/auth/callback`. OIDC client ID and client secret must come from environment variables. `example.com` and `YOUR-...` values in docs are placeholders and do not exist.

Public-style no-auth configurations fail closed at startup. Query-string tokens are not supported.

## Controlled `git_create_work_branch`

`git_create_work_branch` is a bridge-owned local Git operation for creating a new work branch before Codex edits begin.

Safeguards:

- It creates and switches to a new local branch only; it does not push, merge, delete branches, create PRs, or touch tags.
- It refuses dirty worktrees based on `git status --porcelain=v1 --untracked-files=normal`.
- It refuses detached HEAD.
- If `base_branch` is omitted, it uses the current checked-out branch; no universal base such as `main` is hard-coded.
- `base_branch` must be an existing local branch name. Remote-style bases such as `origin/main`, full refs such as `refs/heads/main`, and `HEAD` are refused.
- The target branch must not already exist; switching existing branches is deferred.
- Branch names must pass conservative Local Codex Bridge validation and `git check-ref-format --branch`.
- Failures return structured `blocked_*` diagnostics with relevant git evidence.

## Controlled `git_commit_and_push`

`git_commit_and_push` is a bridge-owned acceptance operation for this review flow:

```text
ChatGPT plans/reviews
  -> local Codex CLI edits a configured repo
  -> ChatGPT reviews package index, targeted diffs, bounded untracked previews, and verification output
  -> human approves
  -> bridge performs controlled git add/commit/push
```

`get_review_package` is read-only. It uses fixed git status/name-status/stat/numstat commands and existing safe untracked preview checks to report a changed-file index without full diffs or full file contents. It does not run verification commands, mutate branches, stage files, commit, push, create PRs, or touch tags/releases.

`get_changed_file_diff` is read-only. It returns one bounded targeted diff for an exact changed/staged/untracked repo-relative path. It uses fixed git argv only, refuses clean/absent paths, outside-repo paths, directories, symlinks, binary or unsafe untracked files, and does not run verification commands or perform any Git/GitHub mutations.

`get_changed_file_text` is read-only. It returns bounded UTF-8 text for one exact currently changed/staged/untracked repo-relative path. It is not a generic file reader: it first proves targeted changed-file state, refuses unchanged paths, deleted/no-content paths, directories, symlinks, non-regular files, binary/invalid UTF-8 content, and unsafe untracked paths, and never returns bytes or base64.

Safeguards:

- It requires an explicit approved file list and non-blank commit message.
- It stages only the approved files.
- Approved paths are normalized to repo-relative paths and must remain inside the configured project root.
- Git staging uses literal pathspec handling so pathspec magic such as `:(glob)*` is not expanded.
- Modified, newly added, and deleted files are supported.
- The only supported remote is currently `origin`.
- If a branch is omitted, the bridge uses the current checked-out branch.
- If a branch is provided, it must match the current checked-out branch.
- Before committing, the bridge inspects staged files and refuses to commit if they differ from the approved file list.
- It refuses unapproved pre-staged files.
- Unsafe input or state returns structured `blocked_*` diagnostics.
- Failures include useful git evidence where relevant, such as status, HEAD, current branch, staged files, final status, command output, or latest log entry.

## Staged-state risks

The bridge tries to avoid staging unapproved files in the first place and verifies staged files before committing. Still, Git operations can fail after some state changes. For example, an approved `git add` may succeed and a later commit may fail.

When a failure may leave changes staged, the response reports that staged-state risk. Operators should inspect git state before retrying, especially after `blocked_add`, `blocked_staged_files`, `blocked_commit`, or `blocked_push` responses.

## Controlled GitHub PR tools

`github_create_pr` and `github_get_pr_status` are bridge-owned GitHub PR operations backed only by the installed GitHub CLI (`gh`). They do not implement native GitHub API calls, do not manage GitHub tokens, and do not print token values. `gh` authentication remains external operator-controlled state.

Safeguards:

- PR operations use fixed `git` and `gh` argument vectors with `shell=False`; no arbitrary shell execution or generic `gh` passthrough is exposed.
- The `origin` remote must be a supported public `github.com` HTTPS or SSH URL. Non-GitHub remotes fail closed with structured `blocked_remote` evidence.
- `gh --version` and `gh auth status -h github.com` must succeed before GitHub PR operations.
- `github_create_pr` requires a normal local branch, refuses detached HEAD, and requires a clean worktree.
- The current branch must already exist on `origin` at the exact local `HEAD`; unpublished branches and remote SHA mismatches are refused.
- C2 intentionally does not add push-upstream authority. Pushing an unpublished branch would require a separate, explicit controlled Git slice.
- If `base_branch` is omitted, the GitHub default branch is read through `gh repo view`; no universal `main` base is hard-coded.
- Explicit base branches are conservatively validated and must exist on `origin`.
- PR creation is refused from the GitHub default branch and when the current branch equals the selected base branch.
- Existing open PRs for the current branch are returned as evidence instead of creating duplicates.
- Draft PRs are the default. Non-draft creation is allowed, but merge, auto-merge, admin bypass, and release/tag operations are not exposed.

## Tunnels and platform gating

Tunnels are external deployment layers, not core bridge runtime. The Python bridge serves MCP on the configured host/port, normally `127.0.0.1:8765`.

Temporary/dev tunnel options include ngrok. Cloudflare Tunnel is a preferred/future deployment option for many setups. Tunnel-provider clients such as `cloudflared` and `ngrok` are not Python runtime dependencies of the bridge. For an operational Cloudflare Tunnel outline, see [CLOUDFLARE_TUNNEL.md](CLOUDFLARE_TUNNEL.md).

ChatGPT-side developer MCP errors such as `FORBIDDEN: This conversation does not support developer MCPs` should be treated as platform/conversation gating unless repository evidence proves otherwise. Local Codex Bridge cannot guarantee that code changes in this repo will enable developer MCPs for a gated ChatGPT conversation.

## Current v0 limitations

- No built-in Cloudflare Access validation.
- No arbitrary shell tool.
- No streaming live logs; polling is supported.
- Branch creation is local-only and refuses dirty or detached state.
- Remote selection for `git_commit_and_push` is currently constrained to `origin`.
- GitHub PR creation/status requires `gh` and supports public `github.com` remotes only; no merge or push-upstream tool is included.
