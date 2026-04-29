# Security notes

This bridge can start local Codex against local repositories. That means it can indirectly modify files in any configured project. Configure it conservatively.

## Strong recommendations

- Bind to `127.0.0.1`.
- Use an authenticated tunnel or private network before connecting from ChatGPT.
- Do not expose this server directly to the public internet.
- Configure only the repositories you are willing to let ChatGPT/Codex work on.
- Keep verification commands allowlisted.
- Do not add arbitrary shell execution unless you fully understand the risk.
- Review diffs before accepting or pushing changes.
- Keep secrets out of task prompts and logs.

## Current v0 limitations

- No built-in OAuth.
- No built-in Cloudflare Access validation.
- No commit/push tools.
- No arbitrary shell tool.
- No streaming live logs; polling is supported.
