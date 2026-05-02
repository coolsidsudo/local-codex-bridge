from __future__ import annotations

from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from .auth import build_auth_provider
from .config import BridgeConfig
from .task_runner import TaskRunner


def build_mcp(config: str | Path | BridgeConfig) -> FastMCP:
    cfg = config if isinstance(config, BridgeConfig) else BridgeConfig.load(config)
    runner = TaskRunner(cfg)
    mcp = FastMCP("Local Codex Bridge", auth=build_auth_provider(cfg))

    @mcp.tool
    def list_projects() -> dict[str, Any]:
        """List configured project profiles available to the bridge."""
        return {
            project_id: {
                "name": project.name,
                "path": str(project.path),
                "default_model": project.default_model or cfg.server.default_model,
                "verification_commands": sorted(project.verification),
            }
            for project_id, project in cfg.projects.items()
        }

    @mcp.tool
    def get_project_status(project_id: str) -> dict[str, Any]:
        """Return git status, HEAD, and remotes for one configured project."""
        return runner.project_status(project_id)

    @mcp.tool
    def start_codex_task(
        project_id: str,
        prompt: str,
        model: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Start a local Codex CLI task inside a configured project.

        The prompt is written to a local task file and passed to `codex exec` stdin.
        """
        return runner.start_codex_task(
            project_id=project_id,
            prompt=prompt,
            model=model,
            dry_run=dry_run,
        )

    @mcp.tool
    def get_task(task_id: str, max_chars: int = 20000) -> dict[str, Any]:
        """Return task metadata and recent stdout/stderr."""
        return runner.get_task(task_id, max_chars=max_chars)

    @mcp.tool
    def list_tasks(limit: int = 20) -> list[dict[str, Any]]:
        """List recent bridge tasks."""
        return runner.list_tasks(limit=limit)

    @mcp.tool
    def abort_task(task_id: str) -> dict[str, Any]:
        """Terminate a running local Codex task."""
        return runner.abort_task(task_id)

    @mcp.tool
    def get_git_diff(project_id: str, max_chars: int = 30000) -> dict[str, Any]:
        """Return git status plus staged, unstaged, and bounded untracked review evidence."""
        return runner.git_diff(project_id, max_chars=max_chars)

    @mcp.tool
    def git_get_branch_status(project_id: str) -> dict[str, Any]:
        """Return current branch, dirty, upstream, ahead/behind, HEAD, and remote evidence."""
        return runner.git_get_branch_status(project_id)

    @mcp.tool
    def git_create_work_branch(
        project_id: str,
        branch_name: str,
        base_branch: str | None = None,
    ) -> dict[str, Any]:
        """Create and switch to a new local work branch from an existing local base branch."""
        return runner.git_create_work_branch(
            project_id=project_id,
            branch_name=branch_name,
            base_branch=base_branch,
        )

    @mcp.tool
    def run_verification(
        project_id: str,
        command_key: str,
        timeout: int = 600,
    ) -> dict[str, Any]:
        """Run an allowlisted verification command for a configured project."""
        return runner.run_verification(project_id, command_key, timeout=timeout)

    @mcp.tool
    def git_commit_and_push(
        project_id: str,
        files: list[str],
        message: str,
        remote: str = "origin",
        branch: str | None = None,
        timeout: int = 120,
    ) -> dict[str, Any]:
        """Stage selected files, create one local commit, and push it.

        This is a bridge-owned Git operation. It is intended for human-approved
        acceptance commits after Codex has edited files and ChatGPT has reviewed
        the patch.
        """
        return runner.git_commit_and_push(
            project_id=project_id,
            files=files,
            message=message,
            remote=remote,
            branch=branch,
            timeout=timeout,
        )

    return mcp
