# Security notes

This bridge can start local Codex against configured local repositories. That means it can indirectly modify files in any configured project. Configure it conservatively.

Local Codex Bridge is project-agnostic: configured project roots are trust boundaries, and bridge behavior should not assume a particular downstream repository.

## Strong recommendations

- Bind to `127.0.0.1`.
- Use an authenticated tunnel or private network before connecting from ChatGPT.
- Do not expose this server directly to the public internet.
- Configure only the repositories you are willing to let ChatGPT/Codex work on.
- Keep verification commands allowlisted.
- Do not add arbitrary shell execution unless you fully understand the risk.
- Review diffs and verification output before accepting changes.
- Keep secrets out of task prompts and logs.

## Controlled `git_commit_and_push`

`git_commit_and_push` is a bridge-owned acceptance operation for the review flow:

ChatGPT plans/reviews → local Codex edits a configured repo → ChatGPT reviews diff and verification → human approves → the bridge performs a controlled add/commit/push.

Safeguards:

- It requires an explicit approved file list and non-blank commit message.
- It stages only the approved files.
- Approved paths are normalized to repo-relative paths and must remain inside the configured project root.
- Modified, newly added, and deleted files are supported.
- The only supported remote is currently `origin`.
- If a branch is omitted, the bridge uses the current checked-out branch.
- If a branch is provided, it must match the current checked-out branch.
- Before committing, the bridge inspects staged files and refuses to commit if they differ from the approved file list.
- It refuses unapproved pre-staged files.
- Failures return structured diagnostics such as status, HEAD, branch, staged files, final status, and git command output where relevant.

If a failure may leave changes staged, the response reports that staged-state risk. Operators should inspect git state before retrying.

## Tunnels and platform gating

Tunnels are external deployment layers, not core bridge runtime. The Python bridge serves MCP on the configured host/port, normally `127.0.0.1:8765`.

Temporary/dev tunnel options include ngrok. Cloudflare Tunnel is a future/preferred deployment option for many setups. Tunnel-provider clients such as `cloudflared` and `ngrok` are not Python runtime dependencies of the bridge.

ChatGPT-side developer MCP errors such as `FORBIDDEN: This conversation does not support developer MCPs` should be treated as platform/conversation gating unless repository evidence proves otherwise. Local Codex Bridge cannot guarantee that code changes in this repo will enable developer MCPs for a gated ChatGPT conversation.

## Current v0 limitations

- No built-in OAuth.
- No built-in Cloudflare Access validation.
- No arbitrary shell tool.
- No streaming live logs; polling is supported.
