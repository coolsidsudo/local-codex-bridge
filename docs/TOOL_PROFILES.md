# Tool profiles design

This document is design-only. Runtime tool profiles are **not implemented yet**.

Current Local Codex Bridge behavior is unchanged: the runtime still registers the existing tool surface according to the current code, with no profile configuration, no profile-based hiding, and no `blocked_disabled` profile behavior.

Any implementation of runtime profiles should be a separate future slice with its own code, tests, migration notes, and documentation updates.

## Product goal

Profiles are a possible future way to make the layered product model explicit at runtime:

- `core` for the lightweight bridge mental model;
- `controlled` for core bridge plus optional authority-changing operations;
- `workflow` for users who want stricter engineering-control defaults or guidance.

The mapping below is provisional design, not current behavior.

## Provisional future profile mapping

| Future profile | Purpose | Proposed tool surface |
| --- | --- | --- |
| `core` | Lightweight bridge only: project profiles, local Codex tasks, repo evidence, changed-file inspection, and allowlisted verification. | `list_projects`, `get_project_status`, `start_codex_task`, `get_task`, `list_tasks`, `abort_task`, `get_git_diff`, `get_review_package`, `get_changed_file_diff`, `get_changed_file_text`, `git_get_branch_status`, `run_verification`, `run_verification_bundle` |
| `controlled` | Core bridge plus optional bridge-owned authority-changing operations and their supporting evidence tools. | All `core` tools plus `git_create_work_branch`, `get_acceptance_readiness`, `git_commit_and_push`, `github_create_pr`, `github_get_pr_status`, `github_merge_pr`, `get_pr_sync_readiness`, `git_sync_local_branch_to_origin` |
| `workflow` | Design-only future profile for controlled actions plus stricter engineering-control defaults or guidance. | Likely all `controlled` tools. May encourage `review_contract` usage, readiness-first workflow guidance, or future stricter defaults. No runtime workflow behavior exists today. |

## Design questions for a future slice

- Should the default remain current behavior for backward compatibility?
- Should new installs eventually default to `core`, or should `local-codex-bridge init` ask which profile the operator wants?
- Should disabled tools be hidden/unregistered from MCP, or registered but return structured `blocked_disabled` responses?
- Should profile configuration be coarse-grained (`core` / `controlled` / `workflow`) or configurable by individual tool groups?
- How should profiles interact with security documentation and existing user expectations?
- What migration path avoids surprising existing users who already rely on the current full tool surface?

## Security expectations

Optional capability does not mean optional safety. If a future profile enables a mutation tool, that tool must still enforce its runtime gates because the gates protect concrete authority boundaries.

A future profile system should not move safety enforcement into prompts or documentation. Workflow preferences can be documented as optional guidance; authority-changing operations need runtime checks.

## Not implemented yet

- No runtime profile config currently exists.
- No tools are currently enabled, disabled, hidden, or exposed based on a profile.
- The mapping in this document is provisional design only.
- Current behavior remains unchanged.
- Future implementation must be handled as a separate slice.
