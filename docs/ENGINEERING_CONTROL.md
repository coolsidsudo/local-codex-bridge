# Optional engineering-control workflow

This document describes an optional operating style for users who want a stricter ChatGPT ↔ Codex engineering loop with evidence-first review and explicit acceptance gates.

You do **not** need this workflow to use Local Codex Bridge as a lightweight local MCP bridge. Core LCB usage can stop at project profiles, local Codex task execution, repository evidence, changed-file inspection, and allowlisted verification.

## What this workflow is for

Use this workflow when you want ChatGPT to coordinate local Codex work with conservative review discipline before authority-changing steps such as commit/push, PR creation, PR merge, or local post-merge sync.

It is useful when:

- multiple tools or agents may contribute to a local worktree;
- you want ChatGPT to review actual repository evidence, not just Codex summaries;
- you want readiness checks before mutation tools;
- you want human approval gates before commit, push, PR merge, or sync operations;
- you prefer a `review_pending` posture until evidence and verification have been inspected.

## When not to use it

Do not treat this document as universal doctrine. It may be unnecessary when:

- you only want ChatGPT to start a local Codex task and inspect bounded results;
- your team already has a review/acceptance workflow outside LCB;
- you do not want LCB to handle commit/push/PR/merge/sync steps;
- the configured project is exploratory and does not need strict acceptance gates.

In those cases, use the core bridge tools only.

## Review posture

Recommended strict-loop posture:

- Treat Codex output as an implementation summary, not proof.
- Inspect actual repository evidence through bridge tools.
- Prefer `get_review_package` first for a compact changed-file index.
- Use `get_changed_file_diff` and `get_changed_file_text` only for targeted follow-up.
- Run `run_verification` or `run_verification_bundle` with configured allowlisted commands.
- Use readiness tools before authority-changing operations.
- Keep explicit human approval gates before commit/push/merge/sync.
- Keep the result in `review_pending` until changed files, evidence, and verification are reviewed.

The optional `review_contract` field on `start_codex_task` can ask Codex for concise, review-oriented implementation summaries instead of full diffs or full file contents. That guidance improves review ergonomics, but it is not enforcement.

## Normal operating checklist

Before starting real implementation work:

```text
1. Start local-codex-bridge serve.
2. Start a secure HTTPS tunnel, for example ngrok http 8765.
3. Refresh the ChatGPT connector URL if the tunnel URL changed.
4. Select Local Codex Bridge in the chat.
5. Run list_projects.
6. Run get_project_status for the target project.
7. Run git_status verification if configured.
8. Confirm branch, HEAD, remote, and clean worktree.
9. Optionally create a clean local work branch with git_create_work_branch.
10. Start a bounded local Codex task, preferably with review_contract: true for concise review-oriented output.
11. Review stdout/stderr and get_review_package.
12. Use get_changed_file_diff / get_changed_file_text only for files that need targeted inspection.
13. Run verification with run_verification or run_verification_bundle.
14. If changes are not acceptable, ask Codex to revise or stop.
15. If changes are acceptable, check get_acceptance_readiness for the exact approved file set.
16. Explicitly approve the exact files and commit message.
17. Call git_commit_and_push only after human approval.
18. Confirm returned branch, remote, commit, push output, and final status.
19. If using GitHub PRs, create or inspect the PR and review GitHub evidence.
20. Use github_get_pr_status or get_pr_sync_readiness for conservative advisory merge/sync evidence.
21. After explicit human approval, call github_merge_pr only from the reviewed PR head branch when strict fresh gates pass.
22. After the PR is merged and local refs are already current, call git_sync_local_branch_to_origin only if you want the bridge to perform narrow local sync to origin/<target> using local refs only.
```

## Guidance versus security boundaries

This workflow is guidance, not a security boundary. Prompts, review contracts, checklist items, and `review_pending` posture help humans and ChatGPT work carefully, but they should not be trusted as enforcement.

Runtime safety gates on mutation tools are different: they protect concrete authority boundaries and are security-relevant. For example, if `github_merge_pr` is enabled and called, strict preflight, fixed `gh pr merge` argv, `--match-head-commit`, no `--admin`, no `--auto`, and partial-failure evidence belong in runtime behavior regardless of whether the operator follows this workflow.

The product rule is:

```text
Runtime code should enforce real authority boundaries.
Documentation can describe recommended workflows.
Personal or team-specific workflow preferences should not become mandatory runtime behavior unless they protect a concrete mutation boundary.
```
