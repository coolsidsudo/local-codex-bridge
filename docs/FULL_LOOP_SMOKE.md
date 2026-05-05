# Full controlled-loop smoke runbook

This runbook is for maintainers validating the complete Local Codex Bridge controlled work loop before a release. It is optional operational guidance, not a runtime profile and not a security boundary. Runtime profiles remain design-only; see [TOOL_PROFILES.md](TOOL_PROFILES.md).

Use this only in a disposable or intentionally prepared repository. Some steps can mutate local or remote state. Every mutating step below is marked **requires explicit human approval**.

## Scope

The smoke should prove that the current tagged or candidate build can support:

- core project/task/repository evidence;
- changed-file review tools;
- allowlisted verification;
- acceptance readiness before commit/push;
- controlled branch creation, commit/push, PR creation, PR merge, and local post-merge sync when explicitly approved;
- conservative PR and sync readiness evidence.

The smoke should not prove unrelated downstream project behavior, broad shell execution, generic Git/GitHub passthrough, tag creation, release creation, or package publishing.

## Preconditions

- Use a temporary generic Git repository or a dedicated smoke repository.
- Configure one Local Codex Bridge project profile for that repository.
- Configure only safe verification keys, such as `git_status` and a small test command for the smoke repository.
- Start the bridge locally and expose it through the intended connector path if testing ChatGPT integration.
- Confirm authentication is configured for any public/tunnel endpoint.
- Confirm the local `gh` CLI is installed and authenticated before GitHub PR tool checks.
- Confirm no real secrets, tunnel URLs, or downstream-project-specific assumptions will be copied into docs, prompts, PR bodies, or release notes.

## Read-only baseline

Run these through the MCP connector and record concise evidence:

1. `list_projects` shows only the expected configured smoke project(s).
2. `get_project_status` reports the intended path, branch, HEAD, and remotes.
3. `git_get_branch_status` reports a non-detached clean state and expected upstream evidence.
4. `run_verification` with a safe key such as `git_status` succeeds.
5. `run_verification_bundle` succeeds for the configured safe keys and returns bounded per-command evidence.

If these fail, stop before any mutation.

## Controlled work branch

**Requires explicit human approval.**

1. Confirm the repository is clean, non-detached, and on the intended local base branch.
2. Call `git_create_work_branch` only after approval of the new branch name and base branch.
3. Confirm the returned branch, base, HEAD, and final status before asking Codex to edit files.

This tool creates and switches to a local branch only. It must not be treated as push, PR, merge, branch deletion, tag, or release authority.

## Task and changed-file review

1. Start a bounded local Codex task with `start_codex_task` and `review_contract: true`. Ask for a tiny, reversible smoke change in the prepared repository.
2. Read task output with `get_task`; treat Codex output as a summary, not proof.
3. Inspect actual repository evidence with `get_review_package`.
4. Use `get_changed_file_diff` for each changed file that needs targeted review.
5. Use `get_changed_file_text` only when bounded file text is needed for review.
6. Run the configured verification bundle again.

Do not continue unless the human reviewer approves the exact changed files and commit message.

## Controlled commit and push

**Requires explicit human approval.**

1. Call `get_acceptance_readiness` with the exact approved file list.
2. Confirm readiness evidence: current branch, push branch, approved files, staged-state checks, origin constraints, and blocking reasons.
3. If ready, call `git_commit_and_push` with the exact approved files and approved commit message.
4. Record returned commit SHA, branch, remote, push evidence, and final status.

If any `blocked_*` or failed result reports possible staged-state risk, stop and inspect the repository before retrying.

## Controlled PR creation and status

**PR creation requires explicit human approval.**

1. Confirm the current branch is the intentionally pushed smoke branch and not the default/base branch.
2. Call `github_create_pr` only after approval of the PR title, body, base branch, and draft setting.
3. Call `github_get_pr_status` for the created PR.
4. Confirm advisory readiness evidence and blocking reasons are understandable. Do not treat advisory readiness as a guarantee that GitHub will merge.

## Controlled PR merge

**Requires explicit human approval.**

1. Confirm the PR has been reviewed in GitHub and is intended to merge.
2. Call `get_pr_sync_readiness` and review both PR readiness and local sync readiness evidence.
3. Confirm the local repository is clean, non-detached, on the reviewed PR head branch, and that local HEAD matches the fresh PR head SHA.
4. Call `github_merge_pr` only after human approval of the PR reference, merge method, expected head SHA, and branch deletion choice.
5. Confirm returned merge evidence and post-merge PR status.

This tool must not be used for auto-merge, admin bypass, tag/release work, broad branch cleanup, or generic `gh` execution.

## Local post-merge sync

**Requires explicit human approval.**

1. Ensure local refs are already current by whatever external process the maintainer chooses; Local Codex Bridge does not fetch or pull here.
2. Call `get_pr_sync_readiness` or inspect branch evidence to confirm the local target branch appears safe to sync to `origin/<target>` using local refs only.
3. Call `git_sync_local_branch_to_origin` only if the target branch, remote, cleanliness, and ahead/behind evidence are approved.
4. Confirm the result is either a safe no-op or a fixed `git switch <target>` plus `git reset --hard origin/<target>` sync after strict gates.

Stop on dirty state, detached HEAD, missing refs, ahead/diverged target branch, non-`origin` remote, or unclear evidence.

## Release-smoke evidence to keep

For the release issue or maintainer notes, keep concise evidence only:

- bridge version or candidate commit;
- configured smoke project ID, without private paths if notes are public;
- successful read-only baseline results;
- changed-file review evidence summary;
- verification command keys and pass/fail status;
- readiness results and any blocking reasons;
- controlled mutation results only when those steps were intentionally approved and performed.

Do not include full file contents, full diffs, secrets, tokens, private tunnel URLs, OIDC credentials, service credentials, or downstream-project-specific assumptions.
