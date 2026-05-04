# Product shape: core bridge and optional engineering control

Local Codex Bridge has two related jobs that should stay clearly separated.

The first job is the core bridge: give ChatGPT a narrow MCP surface for working with a configured local repository through local Codex and local repo evidence. This core should stay lightweight, project-agnostic, and useful even when the operator wants only task execution, inspection, and verification.

The second job is optional engineering control: provide conservative review, readiness, and controlled Git/GitHub workflow tools for operators who want ChatGPT and local Codex to participate in a stricter engineering loop. These controls are useful for high-trust local development workflows, but they are not required for every LCB user and should not be treated as universal engineering doctrine.

## Layers

### Core bridge

The core bridge includes the project-profile and inspection tools that make ChatGPT able to coordinate local Codex safely without receiving broad local machine authority:

- configured project profiles;
- local Codex task start, list, read, and abort operations;
- repository status and branch evidence;
- bounded changed-file review evidence;
- allowlisted verification commands.

This layer should remain small and general. It should not assume a downstream project, a preferred repository workflow, or a particular review methodology.

### Controlled actions

Controlled actions are optional bridge-owned mutations around explicit authority boundaries:

- creating a local work branch;
- committing and pushing approved files;
- creating a GitHub PR through installed `gh`;
- merging a GitHub PR through fixed `gh pr merge` argv;
- syncing a local target branch to local `origin/<target>` refs after merge.

These tools are heavier than the core bridge because they perform authority-changing work. Their runtime gates are part of the safety boundary, not merely style guidance. When LCB performs a mutation, it should fail closed on ambiguous state and return evidence rather than relying on prompt discipline alone.

### Engineering-control workflow

Engineering-control workflow guidance includes review contracts, readiness preflights, operating checklists, and documentation for a stricter ChatGPT ↔ Codex development loop.

This layer is optional. It reflects a conservative operating style for users who want structured review and acceptance, but it should not redefine LCB as a mandatory methodology framework. Operators may use LCB as a lightweight bridge without adopting every engineering-control practice.

## Design rule

Runtime code should enforce real authority boundaries. Documentation can describe recommended workflows. LCB should avoid turning personal or team-specific engineering preferences into mandatory runtime behavior unless they protect a concrete mutation boundary.

A useful test for future work:

```text
Does this protect an authority-changing operation?
  -> enforce it in code.

Is this a recommended way to review or organize work?
  -> document it as optional workflow guidance.

Is this only useful for one downstream project or one operator's habits?
  -> keep it out of core LCB, or present it as an example/profile.
```

## Product identity

A concise product framing:

```text
Local Codex Bridge is a lightweight local MCP bridge for ChatGPT ↔ Codex workflows.

By default, it exposes narrow project, task, repository-inspection, and verification primitives.

For operators who want a stricter engineering-control loop, it also provides optional controlled Git/GitHub actions and review/readiness tools that encode conservative safety gates around authority-changing steps.
```

This framing lets LCB remain useful both for users who only need a bridge and for users who want a more governed local engineering workflow.
