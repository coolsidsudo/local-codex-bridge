from __future__ import annotations

import json
import os
import posixpath
import re
import signal
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import BridgeConfig, ProjectConfig


UNTRACKED_LIST_MAX_FILES = 200
UNTRACKED_PREVIEW_MAX_FILES = 20
UNTRACKED_PREVIEW_MAX_BYTES_PER_FILE = 4096
UNTRACKED_PREVIEW_MAX_TOTAL_CHARS = 12000
REVIEW_PACKAGE_VERSION = 1
REVIEW_CONTRACT_VERSION = 1
REVIEW_CONTRACT_MARKER = "## Local Codex Bridge review contract (v1)"
REVIEW_CONTRACT_FOOTER = f"""---
{REVIEW_CONTRACT_MARKER}

Follow this contract for this local Codex task:
- Implement only the requested bounded slice; do not broaden scope.
- Do not commit, push, create a PR, merge, or touch tags/releases.
- Do not paste full diffs or full file contents in your final response.
- Return a concise implementation summary.
- List changed files.
- List exact verification commands run and their results.
- List risks, deviations, or follow-up needs.
- Confirm no commit, push, PR, merge, tag, or release work was performed.
- ChatGPT/human reviewers will inspect actual repository state, verification evidence, and readiness evidence through Local Codex Bridge tools rather than trusting Codex summaries.
"""
REVIEW_PACKAGE_UNTRACKED_EXCERPT_MAX_CHARS = 1000
BRANCH_NAME_MAX_CHARS = 200
BRANCH_NAME_ALLOWED_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
COMMAND_ARG_MAX_CHARS = 300
GITHUB_HTTPS_REMOTE_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
GITHUB_SSH_REMOTE_RE = re.compile(
    r"^git@github\.com:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?$"
)
GITHUB_PR_JSON_FIELDS = ",".join(
    [
        "number",
        "url",
        "title",
        "state",
        "isDraft",
        "baseRefName",
        "headRefName",
        "headRefOid",
        "mergeable",
        "mergeStateStatus",
        "reviewDecision",
        "statusCheckRollup",
        "updatedAt",
    ]
)
FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
GITHUB_MERGE_METHOD_FLAGS = {"squash": "--squash", "merge": "--merge", "rebase": "--rebase"}
GH_JSON_MAX_CHARS = 60000
CHECK_PASS_VALUES = {"SUCCESS", "SKIPPED", "NEUTRAL"}
CHECK_FAIL_VALUES = {
    "ACTION_REQUIRED",
    "CANCELLED",
    "ERROR",
    "FAILED",
    "FAILURE",
    "STARTUP_FAILURE",
    "STALE",
    "TIMED_OUT",
}
CHECK_PENDING_VALUES = {"EXPECTED", "IN_PROGRESS", "PENDING", "QUEUED", "REQUESTED", "WAITING"}
BLOCKING_REVIEW_DECISIONS = {"CHANGES_REQUESTED", "REVIEW_REQUIRED"}
CHANGED_FILE_TEXT_DEFAULT_MAX_CHARS = 60000
CHANGED_FILE_TEXT_MIN_READ_BYTES = 4096
CHANGED_FILE_TEXT_UTF8_BOUNDARY_BYTES = 4
CHANGED_FILE_TEXT_MAX_READ_BYTES = 1024 * 1024
VERIFICATION_OUTPUT_MAX_CHARS = 40000
CODEX_BIN_ENV = "LCB_CODEX_BIN"
CODEX_REMEDIATION_HINT = (
    "Install the Codex CLI, ensure it is on the bridge process PATH, or set "
    "server.codex_bin, projects.<id>.codex_bin, or LCB_CODEX_BIN to an executable path."
)


def _apply_review_contract(prompt: str) -> tuple[str, bool]:
    if REVIEW_CONTRACT_MARKER in prompt:
        return prompt, False
    base = prompt.rstrip()
    separator = "\n\n" if base else ""
    return f"{base}{separator}{REVIEW_CONTRACT_FOOTER}\n", True


@dataclass
class TaskRecord:
    task_id: str
    project_id: str
    project_path: Path
    task_path: Path
    prompt_path: Path
    stdout_path: Path
    stderr_path: Path
    meta_path: Path
    pid_path: Path


class TaskRunner:
    def __init__(self, config: BridgeConfig):
        self.config = config
        self._task_processes: dict[str, subprocess.Popen[Any]] = {}

    def _project(self, project_id: str) -> ProjectConfig:
        if project_id not in self.config.projects:
            raise ValueError(f"Unknown project_id: {project_id}")
        project = self.config.projects[project_id]
        if not project.path.exists() or not project.path.is_dir():
            raise ValueError(f"Configured project path does not exist: {project.path}")
        return project

    def _task_record(self, task_id: str, project_id: str | None = None) -> TaskRecord:
        task_path = self.config.server.task_dir / task_id
        meta_path = task_path / "meta.json"
        if project_id is None:
            if not meta_path.exists():
                raise ValueError(f"Unknown task_id: {task_id}")
            meta = json.loads(meta_path.read_text())
            project_id = meta["project_id"]
        project = self._project(project_id)
        return TaskRecord(
            task_id=task_id,
            project_id=project_id,
            project_path=project.path,
            task_path=task_path,
            prompt_path=task_path / "prompt.md",
            stdout_path=task_path / "stdout.jsonl",
            stderr_path=task_path / "stderr.log",
            meta_path=meta_path,
            pid_path=task_path / "pid",
        )

    def project_status(self, project_id: str) -> dict[str, Any]:
        project = self._project(project_id)
        return {
            "project_id": project_id,
            "name": project.name,
            "path": str(project.path),
            "codex": self.codex_preflight(project_id),
            "git": self._run(project.path, ["git", "status", "--short", "--branch"], timeout=20),
            "head": self._run(project.path, ["git", "rev-parse", "HEAD"], timeout=20),
            "remotes": self._run(project.path, ["git", "remote", "-v"], timeout=20),
        }

    def codex_preflight(self, project_id: str) -> dict[str, Any]:
        project = self._project(project_id)
        info = self._codex_executable_info(project)
        result: dict[str, Any] = {
            "status": "ok" if info["resolved_path"] else "missing_executable",
            "project_id": project_id,
            "cwd": str(project.path),
            "bridge_process_path": info["path_env"],
            "path_env": info["path_env"],
            "configured_codex_bin": info["configured_codex_bin"],
            "project_codex_bin": info["project_codex_bin"],
            "global_codex_bin": info["global_codex_bin"],
            "env_var": CODEX_BIN_ENV,
            "env_codex_bin": info["env_codex_bin"],
            "selected_source": info["selected_source"],
            "selected_executable": info["selected_executable"],
            "resolved_path": info["resolved_path"],
            "resolution_candidates": info["resolution_candidates"],
        }
        if info["resolved_path"]:
            version = self._run(
                project.path,
                [info["resolved_path"], "--version"],
                timeout=20,
                max_chars=4000,
            )
            result["version"] = version
            if version["returncode"] != 0:
                result["status"] = "version_failed"
                result["failure"] = "codex --version exited nonzero"
        else:
            result["failure"] = "Codex executable is not available to the bridge process"
            result["remediation_hint"] = CODEX_REMEDIATION_HINT
        return result

    def _codex_executable_info(self, project: ProjectConfig) -> dict[str, Any]:
        path_env = os.environ.get("PATH", "")
        env_codex_bin = os.environ.get(CODEX_BIN_ENV, "").strip() or None
        global_codex_bin = self.config.server.codex_bin.strip()
        candidates: list[dict[str, str]] = []

        if project.codex_bin:
            candidates.append({"source": "project", "executable": project.codex_bin})
        if global_codex_bin and global_codex_bin != "codex":
            candidates.append({"source": "global", "executable": global_codex_bin})
        if env_codex_bin:
            candidates.append({"source": "environment", "executable": env_codex_bin})
        if global_codex_bin == "codex":
            candidates.append({"source": "global_default", "executable": global_codex_bin})
        if not candidates:
            candidates.append({"source": "fallback_display", "executable": "codex"})

        selected = candidates[0]
        selected_executable = selected["executable"]
        resolved_path = shutil.which(selected_executable)
        return {
            "configured_codex_bin": project.codex_bin or global_codex_bin,
            "project_codex_bin": project.codex_bin,
            "global_codex_bin": global_codex_bin,
            "env_codex_bin": env_codex_bin,
            "selected_source": selected["source"],
            "selected_executable": selected_executable,
            "resolved_path": resolved_path,
            "path_env": path_env,
            "resolution_candidates": candidates,
        }

    def start_codex_task(
        self,
        project_id: str,
        prompt: str,
        model: str | None = None,
        extra_codex_args: list[str] | None = None,
        dry_run: bool = False,
        review_contract: bool = False,
    ) -> dict[str, Any]:
        project = self._project(project_id)
        task_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        rec = self._task_record(task_id, project_id)
        rec.task_path.mkdir(parents=True, exist_ok=False)
        effective_prompt = prompt
        review_contract_footer_appended = False
        if review_contract:
            effective_prompt, review_contract_footer_appended = _apply_review_contract(prompt)
        rec.prompt_path.write_text(effective_prompt, encoding="utf-8")

        effective_model = model or project.default_model or self.config.server.default_model
        codex_info = self._codex_executable_info(project)
        cmd = [codex_info["selected_executable"], "exec"]
        if effective_model:
            cmd += ["-m", effective_model]
        cmd += list(self.config.server.default_codex_args)
        if extra_codex_args:
            cmd += extra_codex_args

        meta = {
            "task_id": task_id,
            "project_id": project_id,
            "project_path": str(project.path),
            "model": effective_model,
            "cmd": cmd,
            "attempted_executable": codex_info["selected_executable"],
            "cwd": str(project.path),
            "path_env": codex_info["path_env"],
            "codex_executable": {
                "configured_codex_bin": codex_info["configured_codex_bin"],
                "project_codex_bin": codex_info["project_codex_bin"],
                "global_codex_bin": codex_info["global_codex_bin"],
                "env_var": CODEX_BIN_ENV,
                "env_codex_bin": codex_info["env_codex_bin"],
                "selected_source": codex_info["selected_source"],
                "selected_executable": codex_info["selected_executable"],
                "resolved_path": codex_info["resolved_path"],
                "resolution_candidates": codex_info["resolution_candidates"],
            },
            "dry_run": dry_run,
            "review_contract_requested": review_contract,
            "review_contract_version": REVIEW_CONTRACT_VERSION if review_contract else None,
            "review_contract_footer_appended": review_contract_footer_appended,
            "created_at": time.time(),
            "status": "dry_run" if dry_run else "running",
        }
        rec.meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        if dry_run:
            return {"task_id": task_id, "status": "dry_run", "cmd": cmd}

        stdout = rec.stdout_path.open("wb")
        stderr = rec.stderr_path.open("wb")
        prompt_stdin = rec.prompt_path.open("rb")

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(project.path),
                stdin=prompt_stdin,
                stdout=stdout,
                stderr=stderr,
                shell=False,
                start_new_session=True,
            )
        except OSError as exc:
            stdout.close()
            stderr.close()
            prompt_stdin.close()
            ended_at = time.time()
            meta.update(
                {
                    "status": "failed_to_start",
                    "returncode": None,
                    "spawn_error": str(exc),
                    "ended_at": ended_at,
                    "remediation_hint": CODEX_REMEDIATION_HINT,
                }
            )
            rec.meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            return {
                "task_id": task_id,
                "status": "failed_to_start",
                "returncode": None,
                "spawn_error": str(exc),
                "cmd": cmd,
                "attempted_executable": codex_info["selected_executable"],
                "cwd": str(project.path),
                "path_env": codex_info["path_env"],
                "created_at": meta["created_at"],
                "ended_at": ended_at,
                "remediation_hint": CODEX_REMEDIATION_HINT,
            }
        stdout.close()
        stderr.close()
        prompt_stdin.close()
        self._task_processes[task_id] = proc
        rec.pid_path.write_text(str(proc.pid), encoding="utf-8")
        return {"task_id": task_id, "status": "running", "pid": proc.pid, "cmd": cmd}

    def get_task(self, task_id: str, max_chars: int = 20000) -> dict[str, Any]:
        rec = self._task_record(task_id)
        meta = json.loads(rec.meta_path.read_text(encoding="utf-8"))
        meta = self._refresh_running_task_meta(rec, meta)
        status = meta["status"]

        stdout = self._tail_text(rec.stdout_path, max_chars)
        stderr = self._tail_text(rec.stderr_path, max_chars)
        return {
            "task_id": task_id,
            "status": status,
            "meta": meta,
            "stdout_tail": stdout,
            "stderr_tail": stderr,
        }

    def abort_task(self, task_id: str) -> dict[str, Any]:
        rec = self._task_record(task_id)
        if not rec.pid_path.exists():
            return {"task_id": task_id, "status": "no_pid"}
        pid = int(rec.pid_path.read_text().strip())
        try:
            os.killpg(pid, signal.SIGTERM)
            return {"task_id": task_id, "status": "terminated", "pid": pid}
        except ProcessLookupError:
            return {"task_id": task_id, "status": "already_exited", "pid": pid}

    def git_diff(self, project_id: str, max_chars: int = 30000) -> dict[str, Any]:
        project = self._project(project_id)
        status = self._run(project.path, ["git", "status", "--short", "--branch"], timeout=20)
        unstaged_stat = self._run(project.path, ["git", "diff", "--stat"], timeout=20)
        unstaged_diff = self._run(project.path, ["git", "diff"], timeout=20, max_chars=max_chars)
        staged_stat = self._run(project.path, ["git", "diff", "--cached", "--stat"], timeout=20)
        staged_diff = self._run(
            project.path,
            ["git", "diff", "--cached"],
            timeout=20,
            max_chars=max_chars,
        )
        untracked_files = self._untracked_files(project.path)
        untracked_previews = self._untracked_previews(
            project.path,
            untracked_files["files"],
            max_chars=max_chars,
            enabled=untracked_files["returncode"] == 0,
        )
        return {
            "status": status,
            "stat": unstaged_stat,
            "diff": unstaged_diff,
            "unstaged_stat": unstaged_stat,
            "unstaged_diff": unstaged_diff,
            "staged_stat": staged_stat,
            "staged_diff": staged_diff,
            "untracked_files": untracked_files,
            "untracked_previews": untracked_previews,
        }

    def git_get_branch_status(self, project_id: str) -> dict[str, Any]:
        project = self._project(project_id)
        repo = project.path

        short_status = self._run(repo, ["git", "status", "--short", "--branch"], timeout=20)
        porcelain_status = self._porcelain_status(repo)
        head = self._run(repo, ["git", "rev-parse", "--verify", "HEAD"], timeout=20)
        remotes = self._run(repo, ["git", "remote", "-v"], timeout=20)
        current_branch_result = self._current_branch(repo)
        current_branch = current_branch_result.get("stdout", "").strip()
        upstream_info = self._upstream_info(repo)

        evidence = {
            "project_id": project_id,
            "path": str(repo),
            "current_branch": current_branch or None,
            "detached": current_branch_result["returncode"] != 0 or not current_branch,
            "dirty": porcelain_status["returncode"] == 0 and bool(porcelain_status["stdout"]),
            "head": head,
            "short_status": short_status,
            "porcelain_status": porcelain_status,
            "remotes": remotes,
            "current_branch_result": current_branch_result,
            **upstream_info,
        }

        blocking_failures = [
            ("short_status", short_status),
            ("porcelain_status", porcelain_status),
            ("head", head),
            ("remotes", remotes),
            ("current_branch_result", current_branch_result),
        ]
        for key, result in blocking_failures:
            if result["returncode"] != 0:
                return {
                    "status": "blocked_git",
                    "error": f"git command failed while collecting {key}",
                    **evidence,
                }

        if upstream_info.get("upstream") and upstream_info["ahead_behind_result"]["returncode"] != 0:
            return {
                "status": "blocked_git",
                "error": "git command failed while collecting upstream ahead/behind",
                **evidence,
            }

        return {"status": "ok", **evidence}

    def get_acceptance_readiness(
        self,
        project_id: str,
        approved_files: list[str],
        remote: str = "origin",
        branch: str | None = None,
    ) -> dict[str, Any]:
        try:
            project = self._project(project_id)
        except ValueError as exc:
            return {
                "status": "blocked_input",
                "error": str(exc),
                "project_id": project_id,
                "ready": False,
                "blocking_reasons": [str(exc)],
                "warnings": [],
                "limits": {"read_only": True, "mutation_performed": False},
            }

        repo = project.path
        base: dict[str, Any] = {
            "project_id": project_id,
            "name": project.name,
            "path": str(repo),
            "ready": False,
            "blocking_reasons": [],
            "warnings": [],
            "approved_files_requested": approved_files if isinstance(approved_files, list) else [],
            "approved_files_normalized": [],
            "remote": remote,
            "branch_requested": branch,
            "push_branch": None,
            "current_branch": None,
            "detached": False,
            "dirty": False,
            "head": None,
            "changed_files": {"staged": [], "unstaged": [], "untracked": [], "all": []},
            "coverage": {
                "changed_but_not_approved": [],
                "approved_but_not_changed": [],
                "unapproved_staged": [],
                "staged_not_approved": [],
                "staged_within_approved": False,
                "approved_changed_files": [],
            },
            "git": {},
            "limits": {"read_only": True, "mutation_performed": False},
        }

        if not isinstance(approved_files, list):
            return {
                **base,
                "status": "blocked_input",
                "error": "approved_files must be a list",
                "blocking_reasons": ["approved_files must be a list"],
            }
        if remote != "origin":
            return {
                **base,
                "status": "blocked_input",
                "error": "Only remote='origin' is supported",
                "blocking_reasons": ["Only remote='origin' is supported"],
            }
        if branch is not None and not isinstance(branch, str):
            return {
                **base,
                "status": "blocked_input",
                "error": "branch must be a string when provided",
                "blocking_reasons": ["branch must be a string when provided"],
            }
        if branch is not None and not branch.strip():
            return {
                **base,
                "status": "blocked_input",
                "error": "branch must not be blank when provided",
                "blocking_reasons": ["branch must not be blank when provided"],
            }
        if any(not isinstance(item, str) for item in approved_files):
            return {
                **base,
                "status": "blocked_input",
                "error": "approved_files entries must be strings",
                "blocking_reasons": ["approved_files entries must be strings"],
            }

        if approved_files:
            approved_files_result = self._normalize_approved_files(repo, approved_files)
            if approved_files_result["status"] != "ok":
                return {
                    **base,
                    "status": "blocked_input",
                    "error": approved_files_result["error"],
                    "blocking_reasons": [approved_files_result["error"]],
                }
            normalized_approved_files = approved_files_result["files"]
        else:
            normalized_approved_files = []

        short_status = self._run(repo, ["git", "status", "--short", "--branch"], timeout=20)
        porcelain_status_raw = self._git_z_full(
            repo,
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=normal"],
            timeout=20,
        )
        current_branch_result = self._current_branch(repo)
        current_branch = current_branch_result.get("stdout", "").strip()
        head = self._run(repo, ["git", "rev-parse", "--verify", "HEAD"], timeout=20)
        remotes = self._run(repo, ["git", "remote", "-v"], timeout=20)
        origin_url = self._run(repo, ["git", "remote", "get-url", "origin"], timeout=20)
        staged_files = self._staged_files(repo)
        untracked_files = self._untracked_files(repo)
        upstream_info = self._upstream_info(repo)

        git_evidence = {
            "status_short_branch": short_status,
            "porcelain_status": porcelain_status_raw["evidence"],
            "current_branch_result": current_branch_result,
            "head": head,
            "staged_files": staged_files,
            "untracked_files": untracked_files,
            "remotes": remotes,
            "origin_url": origin_url,
            "upstream_result": upstream_info["upstream_result"],
            "ahead_behind_result": upstream_info["ahead_behind_result"],
            "upstream": upstream_info["upstream"],
            "ahead_behind": upstream_info["ahead_behind"],
        }
        base.update(
            {
                "approved_files_normalized": normalized_approved_files,
                "current_branch": current_branch or None,
                "push_branch": branch or current_branch or None,
                "detached": current_branch_result["returncode"] != 0 or not current_branch,
                "head": head["stdout"].strip() if head["returncode"] == 0 else None,
                "git": git_evidence,
            }
        )

        blocking_git_results = [
            ("status_short_branch", short_status),
            ("porcelain_status", porcelain_status_raw["evidence"]),
            ("current_branch_result", current_branch_result),
            ("head", head),
            ("staged_files", staged_files),
            ("untracked_files", untracked_files),
            ("remotes", remotes),
        ]
        for key, result in blocking_git_results:
            if result["returncode"] != 0:
                return {
                    **base,
                    "status": "blocked_git",
                    "error": f"git command failed while collecting {key}",
                    "blocking_reasons": [f"git command failed while collecting {key}"],
                }
        if upstream_info.get("upstream") and upstream_info["ahead_behind_result"]["returncode"] != 0:
            return {
                **base,
                "status": "blocked_git",
                "error": "git command failed while collecting upstream ahead/behind",
                "blocking_reasons": ["git command failed while collecting upstream ahead/behind"],
            }

        porcelain_records = self._parse_porcelain_status_z(porcelain_status_raw["stdout"])
        staged = sorted(
            {
                record["path"]
                for record in porcelain_records
                if record["index_status"] not in {" ", "?"}
            }
        )
        unstaged = sorted(
            {
                record["path"]
                for record in porcelain_records
                if record["worktree_status"] not in {" ", "?"}
            }
        )
        untracked = sorted(
            {
                record["path"]
                for record in porcelain_records
                if record["index_status"] == "?" and record["worktree_status"] == "?"
            }
        )
        all_changed = sorted(set(staged) | set(unstaged) | set(untracked))
        approved_set = set(normalized_approved_files)
        changed_set = set(all_changed)
        staged_set = set(staged_files["files"])
        changed_but_not_approved = sorted(changed_set - approved_set)
        approved_but_not_changed = sorted(approved_set - changed_set)
        unapproved_staged = sorted(staged_set - approved_set)
        approved_changed_files = sorted(approved_set & changed_set)
        ahead_behind = upstream_info["ahead_behind"]

        blocking_reasons: list[str] = []
        warnings: list[str] = []
        if not normalized_approved_files:
            blocking_reasons.append("approved_files must not be empty")
        if not current_branch:
            blocking_reasons.append(
                "Current branch could not be determined; detached or invalid HEAD is not ready"
            )
        elif branch is not None and branch != current_branch:
            blocking_reasons.append("Requested branch does not match current checked-out branch")
        if origin_url["returncode"] != 0:
            blocking_reasons.append("origin remote is not configured")
        if changed_but_not_approved:
            blocking_reasons.append("Changed files are not included in approved_files")
        if approved_but_not_changed:
            blocking_reasons.append("Approved files are not currently changed")
        if unapproved_staged:
            blocking_reasons.append("Unapproved files are already staged")
        if ahead_behind:
            if ahead_behind["behind"] > 0:
                blocking_reasons.append("Current branch is behind upstream")
            if ahead_behind["ahead"] > 0:
                blocking_reasons.append("Current branch is already ahead of upstream")
        else:
            warnings.append("No upstream ahead/behind evidence is available")
        if untracked_files.get("truncated"):
            warnings.append("Untracked file list was truncated")

        base.update(
            {
                "status": "ok",
                "ready": not blocking_reasons,
                "blocking_reasons": blocking_reasons,
                "warnings": warnings,
                "remote": remote,
                "branch_requested": branch,
                "push_branch": branch or current_branch or None,
                "current_branch": current_branch or None,
                "detached": not current_branch,
                "dirty": bool(porcelain_records),
                "changed_files": {
                    "staged": staged,
                    "unstaged": unstaged,
                    "untracked": untracked,
                    "all": all_changed,
                },
                "coverage": {
                    "changed_but_not_approved": changed_but_not_approved,
                    "approved_but_not_changed": approved_but_not_changed,
                    "unapproved_staged": unapproved_staged,
                    "staged_not_approved": unapproved_staged,
                    "staged_within_approved": staged_set <= approved_set,
                    "approved_changed_files": approved_changed_files,
                },
            }
        )
        return base

    def get_review_package(self, project_id: str, max_chars: int = 20000) -> dict[str, Any]:
        if not isinstance(max_chars, int) or max_chars < 0:
            return {"status": "blocked_input", "error": "max_chars must be a non-negative integer"}

        project = self._project(project_id)
        repo = project.path

        short_status = self._run(repo, ["git", "status", "--short", "--branch"], timeout=20)
        porcelain_status_raw = self._git_z_full(
            repo,
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=normal"],
            timeout=20,
        )
        current_branch_result = self._current_branch(repo)
        current_branch = current_branch_result.get("stdout", "").strip()
        head = self._run(repo, ["git", "rev-parse", "--verify", "HEAD"], timeout=20)
        remotes = self._sanitize_command_result(
            self._run(repo, ["git", "remote", "-v"], timeout=20)
        )
        upstream_info = self._upstream_info(repo)

        unstaged_stat = self._run(repo, ["git", "diff", "--stat"], timeout=20)
        staged_stat = self._run(repo, ["git", "diff", "--cached", "--stat"], timeout=20)
        unstaged_name_status_raw = self._git_z_full(
            repo,
            ["git", "diff", "--name-status", "-z", "--diff-filter=ACDMRTUXB"],
            timeout=20,
        )
        staged_name_status_raw = self._git_z_full(
            repo,
            ["git", "diff", "--cached", "--name-status", "-z", "--diff-filter=ACDMRTUXB"],
            timeout=20,
        )
        unstaged_numstat_raw = self._git_z_full(
            repo,
            ["git", "diff", "--numstat", "-z"],
            timeout=20,
        )
        staged_numstat_raw = self._git_z_full(
            repo,
            ["git", "diff", "--cached", "--numstat", "-z"],
            timeout=20,
        )
        staged_files = self._staged_files(repo)
        untracked_files = self._untracked_files(repo)

        evidence = {
            "short_status": short_status,
            "porcelain_status_z": porcelain_status_raw["evidence"],
            "current_branch_result": current_branch_result,
            "head": head,
            "remotes": remotes,
            "upstream_result": upstream_info["upstream_result"],
            "ahead_behind_result": upstream_info["ahead_behind_result"],
            "unstaged_name_status": unstaged_name_status_raw["evidence"],
            "staged_name_status": staged_name_status_raw["evidence"],
            "unstaged_numstat": unstaged_numstat_raw["evidence"],
            "staged_numstat": staged_numstat_raw["evidence"],
        }

        for key in [
            "short_status",
            "porcelain_status_z",
            "current_branch_result",
            "head",
            "unstaged_stat",
            "staged_stat",
            "unstaged_name_status",
            "staged_name_status",
            "unstaged_numstat",
            "staged_numstat",
            "untracked_files",
        ]:
            result = {
                "unstaged_stat": unstaged_stat,
                "staged_stat": staged_stat,
                "untracked_files": untracked_files,
                **evidence,
            }[key]
            if result["returncode"] != 0:
                return {
                    "status": "blocked_git",
                    "error": f"git command failed while collecting {key}",
                    "project_id": project_id,
                    "path": str(repo),
                    "evidence": evidence,
                }

        if upstream_info.get("upstream") and upstream_info["ahead_behind_result"]["returncode"] != 0:
            return {
                "status": "blocked_git",
                "error": "git command failed while collecting upstream ahead/behind",
                "project_id": project_id,
                "path": str(repo),
                "evidence": evidence,
                }

        staged_records = self._parse_name_status_z(staged_name_status_raw["stdout"])
        unstaged_records = self._parse_name_status_z(unstaged_name_status_raw["stdout"])
        staged_numstats = self._parse_numstat_z(staged_numstat_raw["stdout"])
        unstaged_numstats = self._parse_numstat_z(unstaged_numstat_raw["stdout"])
        porcelain_records = self._parse_porcelain_status_z(porcelain_status_raw["stdout"])
        files, untracked_previews = self._changed_file_table(
            repo=repo,
            staged_records=staged_records,
            unstaged_records=unstaged_records,
            staged_numstats=staged_numstats,
            unstaged_numstats=unstaged_numstats,
            porcelain_records=porcelain_records,
            untracked_files=untracked_files["files"],
        )

        summary = {
            "changed_file_count": len(files),
            "staged_count": sum(1 for item in files if item["staged"]),
            "unstaged_count": sum(1 for item in files if item["unstaged"]),
            "untracked_count": sum(1 for item in files if item["untracked"]),
            "binary_count": sum(1 for item in files if item["binary_text_status"] == "binary"),
            "likely_needs_targeted_review_count": sum(
                1 for item in files if item["likely_needs_targeted_review"]
            ),
        }
        staged_file_list = staged_files.get("files", [])
        unstaged_file_list = sorted({record["path"] for record in unstaged_records})

        package = {
            "status": "ok",
            "project_id": project_id,
            "name": project.name,
            "path": str(repo),
            "package_version": REVIEW_PACKAGE_VERSION,
            "limits": {
                "max_chars": max_chars,
                "full_diffs_included": False,
                "full_file_contents_included": False,
            },
            "repo": {
                "current_branch": current_branch or None,
                "detached": current_branch_result["returncode"] != 0 or not current_branch,
                "dirty": bool(porcelain_records),
                "head": head["stdout"].strip() if head["returncode"] == 0 else None,
                "remotes": remotes,
                "upstream": upstream_info["upstream"],
                "ahead_behind": upstream_info["ahead_behind"],
            },
            "evidence": evidence,
            "summary": summary,
            "files": files,
            "staged_files": staged_file_list,
            "unstaged_files": unstaged_file_list,
            "untracked_files": untracked_files,
            "diff_stats": {
                "unstaged": unstaged_stat,
                "staged": staged_stat,
            },
            "untracked_previews": untracked_previews,
            "truncation": {
                "truncated": False,
                "omitted_file_count": 0,
                "omitted_preview_count": 0,
                "omitted_sections": [],
            },
            "suggested_next_inspection_calls": self._suggested_next_inspection_calls(files),
        }
        return self._truncate_review_package(package, max_chars=max_chars)

    def get_changed_file_diff(
        self,
        project_id: str,
        path: str,
        source: str = "auto",
        max_chars: int = 30000,
    ) -> dict[str, Any]:
        if not isinstance(max_chars, int) or max_chars < 0:
            return {"status": "blocked_input", "error": "max_chars must be a non-negative integer"}

        source_result = self._validate_changed_file_diff_source(source)
        if source_result["status"] != "ok":
            return source_result
        source_requested = source_result["source"]

        project = self._project(project_id)
        repo = project.path
        path_result = self._normalize_changed_file_diff_path(repo, path)
        if path_result["status"] != "ok":
            return path_result
        normalized_path = path_result["path"]

        state = self._target_changed_file_state(repo, normalized_path)
        if state["status"] != "ok":
            return {
                "status": "blocked_git",
                "error": state["error"],
                "project_id": project_id,
                "name": project.name,
                "path": str(repo),
                "requested_path": path,
                "normalized_path": normalized_path,
                "source_requested": source_requested,
                "evidence": state["evidence"],
            }

        safety = self._targeted_path_safety(repo, normalized_path, state)
        if safety["status"] != "ok":
            return {
                "status": "blocked_unsafe",
                "reason": safety["reason"],
                "project_id": project_id,
                "name": project.name,
                "path": str(repo),
                "requested_path": path,
                "normalized_path": normalized_path,
                "source_requested": source_requested,
                "evidence": state["evidence"],
            }

        resolution = self._resolve_changed_file_diff_source(state, source_requested)
        if resolution["status"] != "ok":
            return {
                **resolution,
                "project_id": project_id,
                "name": project.name,
                "path": str(repo),
                "requested_path": path,
                "normalized_path": normalized_path,
                "source_requested": source_requested,
                "file": state["file"],
                "evidence": state["evidence"],
            }
        source_resolved = resolution["source"]

        diff_safety = self._targeted_diff_safety_check(repo, normalized_path, source_resolved, state)
        if diff_safety["status"] != "ok":
            return {
                "status": "blocked_unsafe",
                "reason": diff_safety["reason"],
                "project_id": project_id,
                "name": project.name,
                "path": str(repo),
                "requested_path": path,
                "normalized_path": normalized_path,
                "source_requested": source_requested,
                "source_resolved": source_resolved,
                "file": state["file"],
                "evidence": state["evidence"],
                "summary": diff_safety.get("summary"),
            }

        diff = self._run_targeted_changed_file_diff(
            repo,
            normalized_path,
            source_resolved,
            max_chars=max_chars,
        )
        if source_resolved == "untracked" and diff["returncode"] in {0, 1}:
            diff_status = "ok"
        elif source_resolved != "untracked" and diff["returncode"] == 0:
            diff_status = "ok"
        else:
            diff_status = "blocked_git"

        if diff_status != "ok":
            return {
                "status": "blocked_git",
                "error": "targeted git diff command failed",
                "project_id": project_id,
                "name": project.name,
                "path": str(repo),
                "requested_path": path,
                "normalized_path": normalized_path,
                "source_requested": source_requested,
                "source_resolved": source_resolved,
                "file": state["file"],
                "diff": diff,
                "evidence": state["evidence"],
            }

        return {
            "status": "ok",
            "project_id": project_id,
            "name": project.name,
            "path": str(repo),
            "requested_path": path,
            "normalized_path": normalized_path,
            "source_requested": source_requested,
            "source_resolved": source_resolved,
            "file": state["file"],
            "diff": diff,
            "evidence": state["evidence"],
            "limits": {
                "max_chars": max_chars,
                "targeted_path_only": True,
                "full_repo_diff_included": False,
                "verification_executed": False,
            },
            "truncation": {
                "truncated": diff["stdout_truncated"],
                "stdout_chars": len(diff["stdout"]),
                "max_chars": max_chars,
                "omitted_chars": diff["stdout_omitted_chars"],
            },
        }

    def get_changed_file_text(
        self,
        project_id: str,
        path: str,
        source: str = "auto",
        max_chars: int = CHANGED_FILE_TEXT_DEFAULT_MAX_CHARS,
    ) -> dict[str, Any]:
        if not isinstance(max_chars, int) or max_chars < 0:
            return {"status": "blocked_input", "error": "max_chars must be a non-negative integer"}

        source_result = self._validate_changed_file_text_source(source)
        if source_result["status"] != "ok":
            return source_result
        source_requested = source_result["source"]

        project = self._project(project_id)
        repo = project.path
        path_result = self._normalize_changed_file_diff_path(repo, path)
        if path_result["status"] != "ok":
            return path_result
        normalized_path = path_result["path"]

        state = self._target_changed_file_state(repo, normalized_path)
        if state["status"] != "ok":
            return {
                "status": "blocked_git",
                "error": state["error"],
                "project_id": project_id,
                "name": project.name,
                "path": str(repo),
                "requested_path": path,
                "normalized_path": normalized_path,
                "source_requested": source_requested,
                "evidence": state["evidence"],
            }

        initial_safety = self._changed_file_text_initial_path_safety(repo, normalized_path)
        if initial_safety["status"] != "ok":
            return {
                "status": "blocked_unsafe",
                "reason": initial_safety["reason"],
                "project_id": project_id,
                "name": project.name,
                "path": str(repo),
                "requested_path": path,
                "normalized_path": normalized_path,
                "source_requested": source_requested,
                "file": state["file"],
                "evidence": state["evidence"],
            }

        resolution = self._resolve_changed_file_text_source(state, source_requested)
        if resolution["status"] != "ok":
            return {
                **resolution,
                "project_id": project_id,
                "name": project.name,
                "path": str(repo),
                "requested_path": path,
                "normalized_path": normalized_path,
                "source_requested": source_requested,
                "file": state["file"],
                "evidence": state["evidence"],
            }
        source_resolved = resolution["source"]

        text_safety = self._targeted_text_safety_check(repo, normalized_path, source_resolved, state)
        if text_safety["status"] != "ok":
            blocked = {
                "status": text_safety["status"],
                "reason": text_safety["reason"],
                "project_id": project_id,
                "name": project.name,
                "path": str(repo),
                "requested_path": path,
                "normalized_path": normalized_path,
                "source_requested": source_requested,
                "source_resolved": source_resolved,
                "file": state["file"],
                "evidence": {**state["evidence"], **text_safety.get("evidence", {})},
            }
            if text_safety["status"] in {"blocked_deleted", "blocked_no_content"}:
                blocked["suggested_next_tool"] = "get_changed_file_diff"
            return blocked

        if source_resolved == "staged":
            read_result = self._read_changed_staged_text(repo, normalized_path, max_chars=max_chars)
        elif source_resolved == "untracked":
            read_result = self._read_changed_untracked_text(repo, normalized_path, max_chars=max_chars)
        else:
            read_result = self._read_changed_worktree_text(repo, normalized_path, max_chars=max_chars)

        if read_result["status"] != "ok":
            blocked = {
                "status": read_result["status"],
                "reason": read_result["reason"],
                "project_id": project_id,
                "name": project.name,
                "path": str(repo),
                "requested_path": path,
                "normalized_path": normalized_path,
                "source_requested": source_requested,
                "source_resolved": source_resolved,
                "file": state["file"],
                "evidence": {**state["evidence"], **read_result.get("evidence", {})},
            }
            if read_result["status"] in {"blocked_deleted", "blocked_no_content"}:
                blocked["suggested_next_tool"] = "get_changed_file_diff"
            return blocked

        content = read_result["content"]
        truncation = read_result["truncation"]
        return {
            "status": "ok",
            "project_id": project_id,
            "name": project.name,
            "path": str(repo),
            "requested_path": path,
            "normalized_path": normalized_path,
            "source_requested": source_requested,
            "source_resolved": source_resolved,
            "file": state["file"],
            "content": content,
            "evidence": {**state["evidence"], **read_result.get("evidence", {})},
            "limits": {
                "max_chars": max_chars,
                "targeted_path_only": True,
                "changed_file_only": True,
                "binary_content_included": False,
                "verification_executed": False,
            },
            "truncation": truncation,
        }

    def git_create_work_branch(
        self,
        project_id: str,
        branch_name: str,
        base_branch: str | None = None,
    ) -> dict[str, Any]:
        project = self._project(project_id)
        repo = project.path

        branch_validation = self._validate_branch_name(repo, branch_name, "branch_name")
        if branch_validation["status"] != "ok":
            return branch_validation
        new_branch = branch_validation["name"]

        target_remote_style_result = self._is_remote_style_branch(repo, new_branch)
        if target_remote_style_result["status"] != "ok":
            return {
                "status": "blocked_git",
                "error": "Could not inspect git remotes while validating branch name",
                "project_id": project_id,
                "path": str(repo),
                "requested_branch": branch_name,
                "created_branch": new_branch,
                "branch_name_check_ref_format": branch_validation["check_ref_format"],
                "target_remote_style_check": target_remote_style_result,
            }
        if target_remote_style_result["remote_style"]:
            return {
                "status": "blocked_input",
                "error": "branch_name must not be a remote-style branch",
                "project_id": project_id,
                "path": str(repo),
                "requested_branch": branch_name,
                "created_branch": new_branch,
                "branch_name_check_ref_format": branch_validation["check_ref_format"],
                "target_remote_style_check": target_remote_style_result,
            }

        status_before = self._run(repo, ["git", "status", "--short", "--branch"], timeout=20)
        porcelain_status_before = self._porcelain_status(repo)
        head_before = self._run(repo, ["git", "rev-parse", "--verify", "HEAD"], timeout=20)
        remotes = self._run(repo, ["git", "remote", "-v"], timeout=20)
        current_branch_result = self._current_branch(repo)
        current_branch_before = current_branch_result.get("stdout", "").strip()
        upstream_before = self._upstream_info(repo, suffix="_before")

        evidence: dict[str, Any] = {
            "project_id": project_id,
            "path": str(repo),
            "requested_branch": branch_name,
            "created_branch": new_branch,
            "requested_base_branch": base_branch,
            "current_branch_before": current_branch_before or None,
            "current_branch_result": current_branch_result,
            "head_before": head_before,
            "status_before": status_before,
            "porcelain_status_before": porcelain_status_before,
            "remotes": remotes,
            "branch_name_check_ref_format": branch_validation["check_ref_format"],
            "target_remote_style_check": target_remote_style_result,
            **upstream_before,
        }

        for key, result in [
            ("status_before", status_before),
            ("porcelain_status_before", porcelain_status_before),
            ("head_before", head_before),
            ("remotes", remotes),
            ("current_branch_result", current_branch_result),
        ]:
            if result["returncode"] != 0:
                return {
                    "status": "blocked_git",
                    "error": f"git command failed while collecting {key}",
                    **evidence,
                }

        if upstream_before.get("upstream_before") and upstream_before[
            "ahead_behind_result_before"
        ]["returncode"] != 0:
            return {
                "status": "blocked_git",
                "error": "git command failed while collecting upstream ahead/behind",
                **evidence,
            }

        if porcelain_status_before["stdout"]:
            return {
                "status": "blocked_dirty",
                "error": "Worktree is dirty; refusing to create or switch branches",
                **evidence,
            }

        if not current_branch_before:
            return {
                "status": "blocked_detached_head",
                "error": "Current branch could not be determined; refusing from detached or invalid HEAD",
                **evidence,
            }

        effective_base = base_branch if base_branch is not None else current_branch_before
        base_validation = self._validate_branch_name(repo, effective_base, "base_branch")
        if base_validation["status"] != "ok":
            return {**base_validation, **evidence, "base_branch": effective_base}
        base_name = base_validation["name"]
        evidence["base_branch"] = base_name
        evidence["base_branch_check_ref_format"] = base_validation["check_ref_format"]

        remote_style_result = self._is_remote_style_branch(repo, base_name)
        evidence["base_remote_style_check"] = remote_style_result
        if remote_style_result["status"] != "ok":
            return {
                "status": "blocked_git",
                "error": "Could not inspect git remotes while validating base branch",
                **evidence,
            }
        if remote_style_result["remote_style"]:
            return {
                "status": "blocked_input",
                "error": "base_branch must be an existing local branch name, not a remote-style branch",
                **evidence,
            }

        existing_branch = self._local_branch_exists(repo, new_branch)
        evidence["existing_branch"] = existing_branch
        if existing_branch["status"] == "blocked_git":
            return {
                "status": "blocked_git",
                "error": "Could not determine whether branch already exists",
                **evidence,
            }
        if existing_branch["exists"]:
            return {
                "status": "blocked_branch_exists",
                "error": "Target branch already exists; switching existing branches is deferred",
                **evidence,
            }

        base_exists = self._local_branch_exists(repo, base_name)
        evidence["base_branch_exists"] = base_exists
        if base_exists["status"] == "blocked_git":
            return {
                "status": "blocked_git",
                "error": "Could not determine whether base branch exists",
                **evidence,
            }
        if not base_exists["exists"]:
            return {
                "status": "blocked_base_branch",
                "error": "base_branch must be an existing local branch name",
                **evidence,
            }

        base_head = self._run(
            repo,
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{base_name}^{{commit}}"],
            timeout=20,
        )
        evidence["base_ref"] = f"refs/heads/{base_name}"
        evidence["base_head"] = base_head
        if base_head["returncode"] != 0:
            return {
                "status": "blocked_base_branch",
                "error": "Could not resolve base branch to a commit",
                **evidence,
            }

        create = self._run(
            repo,
            ["git", "switch", "--no-track", "-c", new_branch, base_name],
            timeout=120,
            max_chars=40000,
        )
        if create["returncode"] != 0:
            return {
                "status": "blocked_git",
                "error": "git switch failed; branch was not created",
                **evidence,
                "create": create,
                "status_after": self._run(repo, ["git", "status", "--short", "--branch"], timeout=20),
                "porcelain_status_after": self._porcelain_status(repo),
                "current_branch_after_result": self._current_branch(repo),
                "head_after": self._run(repo, ["git", "rev-parse", "--verify", "HEAD"], timeout=20),
            }

        current_branch_after_result = self._current_branch(repo)
        current_branch_after = current_branch_after_result.get("stdout", "").strip()
        status_after = self._run(repo, ["git", "status", "--short", "--branch"], timeout=20)
        porcelain_status_after = self._porcelain_status(repo)
        head_after = self._run(repo, ["git", "rev-parse", "--verify", "HEAD"], timeout=20)
        upstream_after = self._upstream_info(repo, suffix="_after")
        after_evidence = {
            "create": create,
            "current_branch_after": current_branch_after or None,
            "current_branch_after_result": current_branch_after_result,
            "head_after": head_after,
            "status_after": status_after,
            "porcelain_status_after": porcelain_status_after,
            **upstream_after,
        }

        final_state_verified = (
            current_branch_after_result["returncode"] == 0
            and current_branch_after == new_branch
            and head_after["returncode"] == 0
            and status_after["returncode"] == 0
            and porcelain_status_after["returncode"] == 0
        )
        if not final_state_verified:
            return {
                "status": "blocked_git",
                "error": "git switch succeeded but final branch state could not be verified",
                **evidence,
                **after_evidence,
            }

        return {
            "status": "created",
            **evidence,
            **after_evidence,
        }

    def run_verification(
        self,
        project_id: str,
        command_key: str,
        timeout: int = 600,
    ) -> dict[str, Any]:
        project = self._project(project_id)
        cmd = self._resolve_verification_command(project, command_key)
        result = self._run(
            project.path,
            cmd,
            timeout=timeout,
            max_chars=VERIFICATION_OUTPUT_MAX_CHARS,
        )
        result["command_key"] = command_key
        result["status"] = "passed" if result["returncode"] == 0 else "failed"
        if result["returncode"] == 127:
            result["reason"] = "executable not found"
        elif result["returncode"] != 0:
            result["reason"] = "nonzero exit status"
            if not result.get("stderr"):
                result["hint"] = (
                    "Command exited nonzero without stderr; run directly or make the script "
                    "print failing checks."
                )
        else:
            result["reason"] = None
        return result

    def run_verification_bundle(
        self,
        project_id: str,
        command_keys: list[str],
        timeout_per_command: int = 600,
        stop_on_fail: bool = False,
    ) -> dict[str, Any]:
        started_at = time.monotonic()
        try:
            project = self._project(project_id)
        except ValueError as exc:
            return {"status": "blocked_input", "error": str(exc)}

        if not isinstance(command_keys, list):
            return {"status": "blocked_input", "error": "command_keys must be a non-empty list"}
        if not command_keys:
            return {"status": "blocked_input", "error": "command_keys must not be empty"}
        if (
            not isinstance(timeout_per_command, int)
            or isinstance(timeout_per_command, bool)
            or timeout_per_command <= 0
        ):
            return {
                "status": "blocked_input",
                "error": "timeout_per_command must be a positive integer",
            }
        if not isinstance(stop_on_fail, bool):
            return {"status": "blocked_input", "error": "stop_on_fail must be a boolean"}

        commands: list[tuple[str, list[str]]] = []
        for command_key in command_keys:
            if not isinstance(command_key, str):
                return {
                    "status": "blocked_input",
                    "error": "command_keys must contain only strings",
                }
            if not command_key.strip():
                return {
                    "status": "blocked_input",
                    "error": "command_keys must not contain blank strings",
                }
            try:
                commands.append(
                    (command_key, self._resolve_verification_command(project, command_key))
                )
            except ValueError as exc:
                return {"status": "blocked_input", "error": str(exc)}

        results: list[dict[str, Any]] = []
        summary = {"passed": 0, "failed": 0, "not_run": 0}
        try:
            stop_remaining = False
            for command_key, cmd in commands:
                if stop_remaining:
                    summary["not_run"] += 1
                    results.append(
                        {
                            "command_key": command_key,
                            "cmd": cmd,
                            "returncode": None,
                            "stdout": "",
                            "stderr": "",
                            "stdout_truncated": False,
                            "stderr_truncated": False,
                            "stdout_omitted_chars": 0,
                            "stderr_omitted_chars": 0,
                            "elapsed_seconds": 0.0,
                            "status": "not_run",
                            "reason": "not run because stop_on_fail halted after a failed command",
                        }
                    )
                    continue

                result = self._run_verification_bundle_command(
                    project.path,
                    command_key,
                    cmd,
                    timeout=timeout_per_command,
                    max_chars=VERIFICATION_OUTPUT_MAX_CHARS,
                )
                results.append(result)
                if result["status"] == "passed":
                    summary["passed"] += 1
                else:
                    summary["failed"] += 1
                    if stop_on_fail:
                        stop_remaining = True
        except Exception as exc:
            # Defensive guard for unexpected orchestration errors.
            return {
                "status": "blocked_verification",
                "error": str(exc),
                "project_id": project_id,
                "name": project.name,
                "path": str(project.path),
                "requested_command_keys": command_keys,
                "timeout_per_command": timeout_per_command,
                "stop_on_fail": stop_on_fail,
                "elapsed_seconds": round(time.monotonic() - started_at, 3),
                "summary": summary,
                "results": results,
            }

        return {
            "status": "failed_verification" if summary["failed"] else "ok",
            "project_id": project_id,
            "name": project.name,
            "path": str(project.path),
            "requested_command_keys": command_keys,
            "timeout_per_command": timeout_per_command,
            "stop_on_fail": stop_on_fail,
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
            "summary": summary,
            "results": results,
        }

    def git_commit_and_push(
        self,
        project_id: str,
        files: list[str],
        message: str,
        remote: str = "origin",
        branch: str | None = None,
        timeout: int = 120,
    ) -> dict[str, Any]:
        project = self._project(project_id)
        repo = project.path

        if not files:
            return {"status": "blocked_input", "error": "files must not be empty"}
        if not message.strip():
            return {"status": "blocked_input", "error": "message must not be empty"}
        if remote != "origin":
            return {"status": "blocked_input", "error": "Only remote='origin' is supported"}
        if branch is not None and not branch.strip():
            return {"status": "blocked_input", "error": "branch must not be blank when provided"}

        approved_files_result = self._normalize_approved_files(repo, files)
        if approved_files_result["status"] != "ok":
            return approved_files_result
        approved_files = approved_files_result["files"]

        before_status = self._run(repo, ["git", "status", "--short", "--branch"], timeout=20)
        head_before = self._run(repo, ["git", "rev-parse", "HEAD"], timeout=20)
        remotes = self._run(repo, ["git", "remote", "-v"], timeout=20)
        current_branch_result = self._current_branch(repo)
        current_branch_name = current_branch_result.get("stdout", "").strip()

        evidence = {
            "before_status": before_status,
            "head_before": head_before,
            "remotes": remotes,
            "current_branch": current_branch_name,
            "current_branch_result": current_branch_result,
            "push_remote": remote,
            "push_branch": branch or current_branch_name,
            "approved_files": approved_files,
        }

        if current_branch_result["returncode"] != 0 or not current_branch_name:
            return {
                "status": "blocked_branch",
                "error": "Current branch could not be determined; refusing to push from detached or invalid HEAD",
                **evidence,
            }

        push_branch = branch or current_branch_name
        evidence["push_branch"] = push_branch
        if push_branch != current_branch_name:
            return {
                "status": "blocked_branch",
                "error": "Requested branch does not match current checked-out branch",
                **evidence,
            }

        pre_staged_files = self._staged_files(repo)
        evidence["pre_staged_files"] = pre_staged_files
        if pre_staged_files["returncode"] != 0:
            return {
                "status": "blocked_staged_files",
                "error": "Could not inspect staged files before git add",
                **evidence,
            }
        approved_set = set(approved_files)
        pre_staged_set = set(pre_staged_files["files"])
        unapproved_pre_staged = sorted(pre_staged_set - approved_set)
        if unapproved_pre_staged:
            return {
                "status": "blocked_staged_files",
                "error": "Unapproved files are already staged; refusing to commit",
                "unapproved_staged_files": unapproved_pre_staged,
                "staged_files": pre_staged_files,
                "staged_state_risk": "Unapproved files are already staged. Nothing was committed or pushed.",
                **evidence,
            }

        add = self._run(
            repo,
            ["git", "--literal-pathspecs", "add", "--", *approved_files],
            timeout=timeout,
            max_chars=40000,
        )
        if add["returncode"] != 0:
            return {
                "status": "blocked_add",
                "error": "git add failed; no commit or push was attempted",
                **evidence,
                "add": add,
                "final_status": self._run(repo, ["git", "status", "--short", "--branch"], timeout=20),
                "staged_state_risk": "git add failed. Review staged state before retrying.",
            }

        staged_files = self._staged_files(repo)
        if staged_files["returncode"] != 0:
            return {
                "status": "blocked_staged_files",
                "error": "Could not inspect staged files after git add",
                **evidence,
                "add": add,
                "staged_files": staged_files,
                "final_status": self._run(repo, ["git", "status", "--short", "--branch"], timeout=20),
                "staged_state_risk": "Approved changes may now be staged. Review staged state before retrying.",
            }
        if set(staged_files["files"]) != approved_set:
            return {
                "status": "blocked_staged_files",
                "error": "Staged files do not exactly match approved files; refusing to commit",
                **evidence,
                "add": add,
                "staged_files": staged_files,
                "unexpected_staged_files": sorted(set(staged_files["files"]) - approved_set),
                "missing_approved_files": sorted(approved_set - set(staged_files["files"])),
                "final_status": self._run(repo, ["git", "status", "--short", "--branch"], timeout=20),
                "staged_state_risk": "git add may have left approved changes staged. Review staged state before retrying.",
            }

        commit = self._run(repo, ["git", "commit", "-m", message], timeout=timeout, max_chars=40000)
        if commit["returncode"] != 0:
            return {
                "status": "blocked_commit",
                "error": "git commit failed; no push was attempted",
                **evidence,
                "add": add,
                "staged_files": staged_files,
                "commit": commit,
                "final_status": self._run(repo, ["git", "status", "--short", "--branch"], timeout=20),
                "staged_state_risk": "git commit failed and may have left approved changes staged.",
            }

        head_after = self._run(repo, ["git", "rev-parse", "HEAD"], timeout=20)
        push = self._run(repo, ["git", "push", remote, push_branch], timeout=timeout, max_chars=40000)
        final_status = self._run(repo, ["git", "status", "--short", "--branch"], timeout=20)
        log = self._run(repo, ["git", "log", "-1", "--oneline", "--decorate"], timeout=20)

        if push["returncode"] != 0:
            return {
                "status": "blocked_push",
                "error": "git push failed; commit was created locally but was not pushed",
                **evidence,
                "add": add,
                "staged_files": staged_files,
                "commit": commit,
                "head_after": head_after,
                "push": push,
                "final_status": final_status,
                "log": log,
                "staged_state_risk": "Push failed after a local commit. Review branch state before retrying.",
            }

        return {
            "status": "pushed",
            **evidence,
            "add": add,
            "staged_files": staged_files,
            "commit": commit,
            "head_after": head_after,
            "push": push,
            "final_status": final_status,
            "log": log,
        }

    def github_create_pr(
        self,
        project_id: str,
        title: str,
        body: str,
        base_branch: str | None = None,
        draft: bool = True,
    ) -> dict[str, Any]:
        project = self._project(project_id)
        repo = project.path

        if not isinstance(title, str) or not title.strip():
            return {"status": "blocked_input", "error": "title must not be empty"}
        if not isinstance(body, str):
            return {"status": "blocked_input", "error": "body must be a string"}
        if base_branch is not None and not isinstance(base_branch, str):
            return {"status": "blocked_input", "error": "base_branch must be a string when provided"}

        local = self._github_pr_local_preflight(repo, require_clean=True)
        local_evidence = {key: value for key, value in local.items() if key != "status"}
        evidence: dict[str, Any] = {
            "project_id": project_id,
            "path": str(repo),
            **local_evidence,
            "requested_base_branch": base_branch,
            "draft": bool(draft),
        }
        if local["status"] != "ok":
            return {**local, "project_id": project_id, "path": str(repo)}

        current_branch = local["current_branch"]
        current_remote_style = self._is_remote_style_branch(repo, current_branch)
        evidence["current_remote_style_check"] = current_remote_style
        if current_remote_style["status"] != "ok":
            return {
                "status": "blocked_git",
                "error": "Could not inspect git remotes while validating current branch",
                **evidence,
            }
        if current_remote_style["remote_style"]:
            return {
                "status": "blocked_input",
                "error": "current branch must not be remote-style",
                **evidence,
            }

        origin = self._github_origin_info(repo)
        evidence["origin"] = origin
        if origin["status"] != "ok":
            return {"status": "blocked_remote", "error": "origin is not a supported GitHub remote", **evidence}

        gh_ready = self._github_cli_ready(repo)
        evidence["gh"] = gh_ready
        if gh_ready["status"] != "ok":
            return {**gh_ready, **evidence}

        repo_view = self._github_repo_view(repo, origin["repo_arg"])
        evidence["repo_view"] = repo_view
        if repo_view["status"] != "ok":
            return {**repo_view, **evidence}

        head_sha = local["head"]["stdout"].strip()
        default_branch = repo_view["default_branch"]
        selected_base = base_branch if base_branch is not None else default_branch
        evidence["base_branch"] = selected_base
        evidence["github_default_branch"] = default_branch

        base_validation = self._validate_branch_name(repo, selected_base, "base_branch")
        evidence["base_branch_validation"] = base_validation
        if base_validation["status"] != "ok":
            return {"status": "blocked_input", "error": "base_branch is invalid", **evidence}

        if base_branch is not None:
            base_remote_style = self._is_remote_style_branch(repo, selected_base)
            evidence["base_remote_style_check"] = base_remote_style
            if base_remote_style["status"] != "ok":
                return {
                    "status": "blocked_git",
                    "error": "Could not inspect git remotes while validating base_branch",
                    **evidence,
                }
            if base_remote_style["remote_style"]:
                return {
                    "status": "blocked_input",
                    "error": "base_branch must not be remote-style",
                    **evidence,
                }

        if current_branch == default_branch:
            return {
                "status": "blocked_default_branch",
                "error": "Refusing to create a PR from the GitHub default branch",
                **evidence,
            }
        if current_branch == selected_base:
            return {
                "status": "blocked_branch",
                "error": "Current branch must differ from the selected base_branch",
                **evidence,
            }

        base_remote = self._origin_branch_sha(repo, selected_base)
        evidence["base_remote"] = base_remote
        if base_remote["status"] != "ok":
            return {"status": "blocked_git", "error": "Could not inspect origin base branch", **evidence}
        if not base_remote["sha"]:
            return {
                "status": "blocked_base_branch",
                "error": "base_branch does not exist on origin",
                **evidence,
            }

        current_remote = self._origin_branch_sha(repo, current_branch)
        evidence["current_remote"] = current_remote
        if current_remote["status"] != "ok":
            return {"status": "blocked_git", "error": "Could not inspect origin current branch", **evidence}
        if not current_remote["sha"]:
            return {
                "status": "blocked_unpublished",
                "error": "Current branch is not published to origin; C2 does not push upstream",
                **evidence,
            }
        if current_remote["sha"] != head_sha:
            return {
                "status": "blocked_remote_mismatch",
                "error": "origin branch SHA differs from local HEAD",
                **evidence,
            }

        existing = self._github_prs_for_branch(repo, origin["repo_arg"], current_branch)
        evidence["existing_prs"] = existing
        if existing["status"] != "ok":
            return {**existing, **evidence}
        if len(existing["prs"]) == 1:
            return {
                "status": "existing_pr",
                "error": "An open PR already exists for the current branch",
                "pr": existing["prs"][0],
                **evidence,
            }
        if len(existing["prs"]) > 1:
            return {
                "status": "blocked_ambiguous_pr",
                "error": "Multiple open PRs matched the current branch",
                **evidence,
            }

        create_cmd = [
            "gh",
            "pr",
            "create",
            "-R",
            origin["repo_arg"],
            "--title",
            title,
            "--body",
            body,
            "--base",
            selected_base,
            "--head",
            current_branch,
        ]
        if draft:
            create_cmd.append("--draft")

        create = self._run(repo, create_cmd, timeout=120, max_chars=40000)
        evidence["create"] = self._sanitize_command_result(create)
        if create["returncode"] != 0:
            return {"status": "blocked_gh", "error": "gh pr create failed", **evidence}

        parsed_url = self._parse_github_pr_create_url(
            create["stdout"],
            origin["owner"],
            origin["repo"],
        )
        evidence["created_pr_url_parse"] = parsed_url
        if parsed_url["status"] != "ok":
            return {
                "status": "blocked_gh_output",
                "error": "gh pr create did not return the expected GitHub PR URL",
                **evidence,
            }

        canonical = self._github_pr_view(repo, origin["repo_arg"], str(parsed_url["number"]))
        evidence["canonical_pr"] = canonical
        if canonical["status"] != "ok":
            return {**canonical, **evidence}

        return {
            "status": "created",
            "pr": canonical["pr"],
            **evidence,
        }

    def github_get_pr_status(
        self,
        project_id: str,
        pr_url_or_number: str | int | None = None,
    ) -> dict[str, Any]:
        project = self._project(project_id)
        repo = project.path
        local = self._collect_pr_sync_local_evidence(repo)
        pr_readiness = self._initial_pr_readiness_from_local(local)

        origin = self._github_origin_info(repo)
        evidence: dict[str, Any] = {
            "project_id": project_id,
            "path": str(repo),
            "origin": origin,
            "requested_pr": pr_url_or_number,
            "pr_readiness": pr_readiness,
        }
        if origin["status"] != "ok":
            pr_readiness["status"] = origin["status"]
            pr_readiness["blocking_reasons"].append("origin is not a supported GitHub remote")
            return {"status": "blocked_remote", "error": "origin is not a supported GitHub remote", **evidence}

        gh_ready = self._github_cli_ready(repo)
        evidence["gh"] = gh_ready
        if gh_ready["status"] != "ok":
            pr_readiness["status"] = gh_ready["status"]
            pr_readiness["blocking_reasons"].append(gh_ready["error"])
            return {**gh_ready, **evidence}

        if pr_url_or_number is None:
            branch = self._current_branch(repo)
            evidence["current_branch_result"] = branch
            current_branch = branch["stdout"].strip()
            evidence["current_branch"] = current_branch or None
            if branch["returncode"] != 0 or not current_branch:
                pr_readiness["status"] = "blocked_detached_head"
                pr_readiness["blocking_reasons"].append(
                    "Current branch could not be determined for branch PR lookup"
                )
                return {
                    "status": "blocked_detached_head",
                    "error": "Current branch could not be determined for branch PR lookup",
                    **evidence,
                }
            prs = self._github_prs_for_branch(repo, origin["repo_arg"], current_branch)
            evidence["prs_for_branch"] = prs
            if prs["status"] != "ok":
                pr_readiness["status"] = prs["status"]
                pr_readiness["blocking_reasons"].append(prs["error"])
                return {**prs, **evidence}
            if not prs["prs"]:
                pr_readiness["status"] = "no_pr"
                pr_readiness["blocking_reasons"].append("No open PR found for current branch")
                return {"status": "no_pr", "error": "No open PR found for current branch", **evidence}
            if len(prs["prs"]) > 1:
                pr_readiness["status"] = "blocked_ambiguous_pr"
                pr_readiness["blocking_reasons"].append("Multiple open PRs matched the current branch")
                return {
                    "status": "blocked_ambiguous_pr",
                    "error": "Multiple open PRs matched the current branch",
                    **evidence,
                }
            pr_readiness = self._finalize_pr_readiness_from_pr(
                readiness=pr_readiness,
                pr=prs["prs"][0],
                local=local,
                target_branch=None,
                suppress_merged_open_pr_blockers=True,
            )
            evidence["pr_readiness"] = pr_readiness
            return {"status": "ok", "pr": prs["prs"][0], **evidence}

        parsed = self._parse_pr_reference(pr_url_or_number, origin["owner"], origin["repo"])
        evidence["parsed_pr_reference"] = parsed
        if parsed["status"] != "ok":
            pr_readiness["status"] = "blocked_input"
            pr_readiness["blocking_reasons"].append("Invalid PR number or URL")
            return {"status": "blocked_input", "error": "Invalid PR number or URL", **evidence}

        canonical = self._github_pr_view(repo, origin["repo_arg"], parsed["reference"])
        evidence["canonical_pr"] = canonical
        if canonical["status"] != "ok":
            pr_readiness["status"] = canonical["status"]
            pr_readiness["blocking_reasons"].append(canonical["error"])
            return {**canonical, **evidence}
        pr_readiness = self._finalize_pr_readiness_from_pr(
            readiness=pr_readiness,
            pr=canonical["pr"],
            local=local,
            target_branch=None,
            suppress_merged_open_pr_blockers=True,
        )
        evidence["pr_readiness"] = pr_readiness
        return {"status": "ok", "pr": canonical["pr"], **evidence}

    def github_merge_pr(
        self,
        project_id: str,
        pr_url_or_number: str | int,
        merge_method: str = "squash",
        delete_branch: bool = False,
        expected_head_sha: str | None = None,
    ) -> dict[str, Any]:
        try:
            project = self._project(project_id)
        except ValueError as exc:
            error = str(exc)
            return {
                "status": "blocked_input",
                "error": error,
                "project_id": project_id,
                "name": None,
                "path": None,
                "requested_pr": pr_url_or_number,
                "merge_method": merge_method,
                "delete_branch": delete_branch,
                "expected_head_sha": expected_head_sha,
                "matched_head_sha": None,
                "ready": False,
                "merge_command_executed": False,
                "mutation_performed": False,
                "blocking_reasons": [error],
                "warnings": [],
                "limits": self._github_merge_limits(mutation_performed=False),
            }

        repo = project.path
        base: dict[str, Any] = {
            "project_id": project_id,
            "name": project.name,
            "path": str(repo),
            "requested_pr": pr_url_or_number,
            "merge_method": merge_method,
            "delete_branch": delete_branch,
            "expected_head_sha": expected_head_sha,
            "matched_head_sha": None,
            "ready": False,
            "merge_command_executed": False,
            "mutation_performed": False,
            "blocking_reasons": [],
            "warnings": [],
            "limits": self._github_merge_limits(mutation_performed=False),
        }

        validation = self._validate_github_merge_inputs(
            pr_url_or_number=pr_url_or_number,
            merge_method=merge_method,
            delete_branch=delete_branch,
            expected_head_sha=expected_head_sha,
        )
        if validation["status"] != "ok":
            error = validation["error"]
            return {
                **base,
                "status": "blocked_input",
                "error": error,
                "blocking_reasons": [error],
                "input_validation": validation,
            }

        merge_method = validation["merge_method"]
        expected_sha = validation["expected_head_sha"]
        base.update(
            {
                "merge_method": merge_method,
                "delete_branch": delete_branch,
                "expected_head_sha": expected_sha,
            }
        )

        before = self._collect_github_merge_preflight(
            repo=repo,
            pr_url_or_number=pr_url_or_number,
            expected_head_sha=expected_sha,
        )
        readiness = before["readiness"]
        pr = before.get("pr")
        pr_summary = self._github_pr_summary(pr)
        matched_head_sha = pr_summary.get("headRefOid")
        base.update(
            {
                "pr": pr_summary,
                "matched_head_sha": matched_head_sha,
                "ready": readiness["ready_to_consider_merge"],
                "blocking_reasons": readiness["blocking_reasons"],
                "warnings": readiness["warnings"],
                "before_evidence": before["before_evidence"],
                "readiness_evidence": readiness,
            }
        )

        if before["status"] == "blocked_git":
            return {
                **base,
                "status": "blocked_git",
                "error": "Could not collect local git evidence for PR merge preflight",
            }
        if before["status"] in {"blocked_input", "blocked_github"}:
            return {
                **base,
                "status": before["status"],
                "error": before["error"],
            }
        if readiness["blocking_reasons"]:
            return {
                **base,
                "status": "blocked_readiness",
                "error": "PR is not ready for controlled merge execution",
            }

        reference = before["merge_reference"]
        method_flag = GITHUB_MERGE_METHOD_FLAGS[merge_method]
        merge_cmd = [
            "gh",
            "pr",
            "merge",
            reference,
            method_flag,
            "--match-head-commit",
            matched_head_sha,
        ]
        if delete_branch:
            merge_cmd.append("--delete-branch")

        started_at = time.monotonic()
        merge_result = self._sanitize_command_result(
            self._run(repo, merge_cmd, timeout=120, max_chars=40000)
        )
        merge_result["elapsed_seconds"] = round(time.monotonic() - started_at, 3)

        command_base = {
            **base,
            "ready": True,
            "blocking_reasons": [],
            "merge_command_executed": True,
            "merge_command_evidence": merge_result,
        }
        if merge_result["returncode"] != 0:
            warnings = list(base["warnings"])
            warnings.append(
                "gh pr merge failed after preflight; check PR status in GitHub before retrying"
            )
            return {
                **command_base,
                "status": "failed_merge",
                "error": "gh pr merge failed",
                "warnings": warnings,
                "mutation_performed": False,
                "mutation_state": "unknown_after_failed_merge_command",
                "limits": self._github_merge_limits(
                    mutation_performed=False,
                    mutation_state="unknown_after_failed_merge_command",
                ),
            }

        after = self._github_pr_view(repo, before["origin"]["repo_arg"], str(pr_summary["number"]))
        after_evidence: dict[str, Any] = {"pr_status": after}
        warnings = list(base["warnings"])
        after_evidence_error = None
        if after["status"] != "ok":
            after_evidence_error = after.get("error", "Could not collect PR status after merge")
            warnings.append(
                "gh pr merge succeeded but after-status evidence could not be collected"
            )

        return {
            **command_base,
            "status": "ok_merged",
            "warnings": warnings,
            "mutation_performed": True,
            "limits": self._github_merge_limits(mutation_performed=True),
            "after_evidence": after_evidence,
            "after_evidence_error": after_evidence_error,
        }

    def get_pr_sync_readiness(
        self,
        project_id: str,
        pr_url_or_number: str | int | None = None,
        target_branch: str = "main",
        remote: str = "origin",
    ) -> dict[str, Any]:
        try:
            project = self._project(project_id)
        except ValueError as exc:
            error = str(exc)
            return {
                "status": "blocked_input",
                "error": error,
                "project_id": project_id,
                "requested_pr": pr_url_or_number,
                "target_branch": target_branch,
                "remote": remote,
                "ready_to_consider_merge": False,
                "ready_to_sync_local_target": False,
                "pr_readiness": self._empty_pr_readiness(
                    status="blocked_input",
                    blocking_reasons=[error],
                ),
                "local_sync_readiness": self._empty_sync_readiness(
                    target_branch=target_branch,
                    remote=remote,
                    status="blocked_input",
                    blocking_reasons=[error],
                ),
                "limits": self._pr_sync_limits(),
            }

        repo = project.path
        base: dict[str, Any] = {
            "project_id": project_id,
            "name": project.name,
            "path": str(repo),
            "requested_pr": pr_url_or_number,
            "target_branch": target_branch,
            "remote": remote,
            "ready_to_consider_merge": False,
            "ready_to_sync_local_target": False,
            "limits": self._pr_sync_limits(),
        }

        if remote != "origin":
            error = "Only remote='origin' is supported"
            return {
                **base,
                "status": "blocked_input",
                "error": error,
                "pr_readiness": self._empty_pr_readiness(
                    status="blocked_input",
                    blocking_reasons=[error],
                ),
                "local_sync_readiness": self._empty_sync_readiness(
                    target_branch=target_branch,
                    remote=remote,
                    status="blocked_input",
                    blocking_reasons=[error],
                ),
            }
        if not isinstance(target_branch, str):
            error = "target_branch must be a string"
            return {
                **base,
                "status": "blocked_input",
                "error": error,
                "pr_readiness": self._empty_pr_readiness(
                    status="blocked_input",
                    blocking_reasons=[error],
                ),
                "local_sync_readiness": self._empty_sync_readiness(
                    target_branch=target_branch,
                    remote=remote,
                    status="blocked_input",
                    blocking_reasons=[error],
                ),
            }

        target_validation = self._validate_branch_name(repo, target_branch, "target_branch")
        if target_validation["status"] != "ok":
            error = target_validation["error"]
            return {
                **base,
                "status": "blocked_input",
                "error": error,
                "target_branch_validation": target_validation,
                "pr_readiness": self._empty_pr_readiness(
                    status="blocked_input",
                    blocking_reasons=[error],
                ),
                "local_sync_readiness": self._empty_sync_readiness(
                    target_branch=target_branch,
                    remote=remote,
                    status="blocked_input",
                    blocking_reasons=[error],
                ),
            }

        remote_style = self._is_remote_style_branch(repo, target_validation["name"])
        if remote_style["status"] != "ok":
            error = "Could not inspect git remotes while validating target_branch"
            local = self._collect_pr_sync_local_evidence(repo)
            pr_readiness = self._empty_pr_readiness(
                status="blocked_git",
                blocking_reasons=[error],
            )
            sync_readiness = self._empty_sync_readiness(
                target_branch=target_validation["name"],
                remote=remote,
                status="blocked_git",
                blocking_reasons=[error],
            )
            return {
                **base,
                "status": "ok",
                "target_branch": target_validation["name"],
                "target_branch_validation": target_validation,
                "target_remote_style_check": remote_style,
                "local": local,
                "pr_readiness": pr_readiness,
                "local_sync_readiness": sync_readiness,
            }
        if remote_style["remote_style"]:
            error = "target_branch must not be a remote-style branch"
            return {
                **base,
                "status": "blocked_input",
                "error": error,
                "target_branch_validation": target_validation,
                "target_remote_style_check": remote_style,
                "pr_readiness": self._empty_pr_readiness(
                    status="blocked_input",
                    blocking_reasons=[error],
                ),
                "local_sync_readiness": self._empty_sync_readiness(
                    target_branch=target_validation["name"],
                    remote=remote,
                    status="blocked_input",
                    blocking_reasons=[error],
                ),
            }

        target = target_validation["name"]
        local = self._collect_pr_sync_local_evidence(repo)
        pr_readiness = self._build_pr_merge_readiness(
            repo=repo,
            pr_url_or_number=pr_url_or_number,
            target_branch=target,
            local=local,
            suppress_merged_open_pr_blockers=True,
        )
        sync_readiness = self._build_local_sync_readiness(
            repo=repo,
            target_branch=target,
            remote=remote,
            local=local,
        )

        return {
            **base,
            "status": "ok",
            "target_branch": target,
            "target_branch_validation": target_validation,
            "target_remote_style_check": remote_style,
            "local": local,
            "ready_to_consider_merge": pr_readiness["ready_to_consider_merge"],
            "ready_to_sync_local_target": sync_readiness["ready_to_sync_local_target"],
            "pr_readiness": pr_readiness,
            "local_sync_readiness": sync_readiness,
        }

    def git_sync_local_branch_to_origin(
        self,
        project_id: str,
        target_branch: str = "main",
        remote: str = "origin",
    ) -> dict[str, Any]:
        try:
            project = self._project(project_id)
        except ValueError as exc:
            error = str(exc)
            return {
                "status": "blocked_input",
                "action": "blocked",
                "error": error,
                "project_id": project_id,
                "target_branch": target_branch,
                "remote": remote,
                "warnings": [],
                "blocking_reasons": [error],
                "executed_commands": [],
                "limits": self._local_sync_execution_limits(),
            }

        repo = project.path
        base: dict[str, Any] = {
            "project_id": project_id,
            "name": project.name,
            "path": str(repo),
            "target_branch": target_branch,
            "remote": remote,
            "warnings": [],
            "blocking_reasons": [],
            "executed_commands": [],
            "limits": self._local_sync_execution_limits(),
        }

        if remote != "origin":
            error = "Only remote='origin' is supported"
            return {
                **base,
                "status": "blocked_input",
                "action": "blocked",
                "error": error,
                "blocking_reasons": [error],
            }
        if not isinstance(target_branch, str):
            error = "target_branch must be a string"
            return {
                **base,
                "status": "blocked_input",
                "action": "blocked",
                "error": error,
                "blocking_reasons": [error],
            }

        target_validation = self._validate_branch_name(repo, target_branch, "target_branch")
        if target_validation["status"] != "ok":
            error = target_validation["error"]
            return {
                **base,
                "status": "blocked_input",
                "action": "blocked",
                "error": error,
                "target_branch_validation": target_validation,
                "blocking_reasons": [error],
            }

        remote_style = self._is_remote_style_branch(repo, target_validation["name"])
        if remote_style["status"] != "ok":
            error = "Could not inspect git remotes while validating target_branch"
            return {
                **base,
                "status": "blocked_git",
                "action": "blocked",
                "error": error,
                "target_branch": target_validation["name"],
                "target_branch_validation": target_validation,
                "target_remote_style_check": remote_style,
                "blocking_reasons": [error],
            }
        if remote_style["remote_style"]:
            error = "target_branch must not be a remote-style branch"
            return {
                **base,
                "status": "blocked_input",
                "action": "blocked",
                "error": error,
                "target_branch": target_validation["name"],
                "target_branch_validation": target_validation,
                "target_remote_style_check": remote_style,
                "blocking_reasons": [error],
            }

        target = target_validation["name"]
        base.update(
            {
                "target_branch": target,
                "target_branch_validation": target_validation,
                "target_remote_style_check": remote_style,
            }
        )

        initial = self._collect_local_sync_execution_readiness(repo, target, remote)
        if initial["status"] == "blocked_git":
            return {
                **base,
                "status": "blocked_git",
                "action": "blocked",
                "error": "Could not collect local sync evidence",
                "before_evidence": initial["evidence"],
                "warnings": initial["warnings"],
                "blocking_reasons": initial["blocking_reasons"],
                "local_sync_readiness": initial["readiness"],
            }
        if initial["blocking_reasons"]:
            return {
                **base,
                "status": "blocked_sync",
                "action": "blocked",
                "error": "Local target branch is not safe to sync",
                "before_evidence": initial["evidence"],
                "warnings": initial["warnings"],
                "blocking_reasons": initial["blocking_reasons"],
                "local_sync_readiness": initial["readiness"],
            }

        relation = initial["evidence"]["relation"]
        if relation == "equal":
            return {
                **base,
                "status": "ok_noop",
                "action": "no_op",
                "before_evidence": initial["evidence"],
                "after_evidence": initial["evidence"],
                "warnings": initial["warnings"],
                "blocking_reasons": [],
                "local_sync_readiness": initial["readiness"],
            }
        if relation != "behind":
            error = "Local target branch relation is not safe to sync"
            return {
                **base,
                "status": "blocked_sync",
                "action": "blocked",
                "error": error,
                "before_evidence": initial["evidence"],
                "warnings": initial["warnings"],
                "blocking_reasons": [error],
                "local_sync_readiness": initial["readiness"],
            }

        before = self._collect_local_sync_execution_readiness(repo, target, remote)
        if not self._local_sync_execution_ready(before, expected_relation="behind"):
            return {
                **base,
                "status": "blocked_sync",
                "action": "blocked",
                "error": "Local sync safety re-check failed before mutation",
                "before_evidence": before["evidence"],
                "initial_evidence": initial["evidence"],
                "warnings": before["warnings"],
                "blocking_reasons": before["blocking_reasons"] or ["Local sync safety re-check failed"],
                "local_sync_readiness": before["readiness"],
            }

        executed_commands: list[list[str]] = []
        switch_cmd = ["git", "switch", target]
        switch_result = self._run(repo, switch_cmd, timeout=60)
        executed_commands.append(switch_cmd)
        if switch_result["returncode"] != 0:
            after = self._collect_local_sync_execution_readiness(repo, target, remote)
            return {
                **base,
                "status": "blocked_sync",
                "action": "blocked",
                "error": "git switch failed",
                "before_evidence": before["evidence"],
                "after_evidence": after["evidence"],
                "warnings": before["warnings"] + after["warnings"],
                "blocking_reasons": ["git switch failed"],
                "executed_commands": executed_commands,
                "mutation_results": {"switch": switch_result},
                "local_sync_readiness": after["readiness"],
            }

        intermediate = self._collect_local_sync_execution_readiness(repo, target, remote)
        if not self._local_sync_execution_ready(
            intermediate,
            expected_relation="behind",
            expected_current_branch=target,
        ):
            return {
                **base,
                "status": "blocked_sync",
                "action": "blocked",
                "error": "Local sync safety check failed after switch",
                "before_evidence": before["evidence"],
                "intermediate_evidence": intermediate["evidence"],
                "warnings": before["warnings"] + intermediate["warnings"],
                "blocking_reasons": intermediate["blocking_reasons"]
                or ["Local sync safety check failed after switch"],
                "executed_commands": executed_commands,
                "mutation_results": {"switch": switch_result},
                "local_sync_readiness": intermediate["readiness"],
            }

        reset_cmd = ["git", "reset", "--hard", f"{remote}/{target}"]
        reset_result = self._run(repo, reset_cmd, timeout=60)
        executed_commands.append(reset_cmd)
        after = self._collect_local_sync_execution_readiness(repo, target, remote)
        if reset_result["returncode"] != 0:
            return {
                **base,
                "status": "blocked_sync",
                "action": "blocked",
                "error": "git reset failed",
                "before_evidence": before["evidence"],
                "intermediate_evidence": intermediate["evidence"],
                "after_evidence": after["evidence"],
                "warnings": before["warnings"] + intermediate["warnings"] + after["warnings"],
                "blocking_reasons": ["git reset failed"],
                "executed_commands": executed_commands,
                "mutation_results": {"switch": switch_result, "reset": reset_result},
                "local_sync_readiness": after["readiness"],
            }

        if not self._local_sync_execution_ready(
            after,
            expected_relation="equal",
            expected_current_branch=target,
        ):
            return {
                **base,
                "status": "blocked_sync",
                "action": "blocked",
                "error": "Post-reset local sync evidence is not clean and equal",
                "before_evidence": before["evidence"],
                "intermediate_evidence": intermediate["evidence"],
                "after_evidence": after["evidence"],
                "warnings": before["warnings"] + intermediate["warnings"] + after["warnings"],
                "blocking_reasons": after["blocking_reasons"]
                or ["Post-reset local sync evidence is not clean and equal"],
                "executed_commands": executed_commands,
                "mutation_results": {"switch": switch_result, "reset": reset_result},
                "local_sync_readiness": after["readiness"],
            }

        return {
            **base,
            "status": "ok_synced",
            "action": "synced",
            "before_evidence": before["evidence"],
            "intermediate_evidence": intermediate["evidence"],
            "after_evidence": after["evidence"],
            "warnings": before["warnings"] + intermediate["warnings"] + after["warnings"],
            "blocking_reasons": [],
            "executed_commands": executed_commands,
            "mutation_results": {"switch": switch_result, "reset": reset_result},
            "local_sync_readiness": after["readiness"],
        }

    def _pr_sync_limits(self) -> dict[str, Any]:
        return {
            "read_only": True,
            "mutation_performed": False,
            "no_merge_authority": True,
            "sync_uses_local_refs_only": True,
        }

    def _local_sync_execution_limits(self) -> dict[str, Any]:
        return {
            "read_only": False,
            "mutation_scope": ["git switch <target_branch>", "git reset --hard origin/<target_branch>"],
            "sync_uses_local_refs_only": True,
            "no_fetch": True,
            "no_pull": True,
            "no_push": True,
            "no_merge": True,
            "no_pr_mutation": True,
            "no_branch_deletion": True,
            "no_tag_work": True,
            "no_release_work": True,
        }

    def _github_merge_limits(
        self,
        mutation_performed: bool = False,
        mutation_state: str | None = None,
    ) -> dict[str, Any]:
        limits = {
            "read_only": False,
            "mutation_performed": mutation_performed,
            "mutation_scope": ["gh pr merge <pr> <method_flag> --match-head-commit <sha>"],
            "no_fetch": True,
            "no_pull": True,
            "no_push": True,
            "no_local_sync": True,
            "no_arbitrary_branch_cleanup": True,
            "no_tag_or_release": True,
            "no_admin_bypass": True,
            "no_auto_merge": True,
            "no_broad_gh_passthrough": True,
        }
        if mutation_state is not None:
            limits["mutation_state"] = mutation_state
        return limits

    def _validate_github_merge_inputs(
        self,
        pr_url_or_number: Any,
        merge_method: Any,
        delete_branch: Any,
        expected_head_sha: Any,
    ) -> dict[str, Any]:
        if isinstance(pr_url_or_number, bool) or not isinstance(pr_url_or_number, (str, int)):
            return {"status": "blocked_input", "error": "pr_url_or_number must be a string or int"}
        if isinstance(pr_url_or_number, str) and not pr_url_or_number.strip():
            return {"status": "blocked_input", "error": "pr_url_or_number must not be empty"}
        if isinstance(pr_url_or_number, int) and pr_url_or_number <= 0:
            return {"status": "blocked_input", "error": "pr_url_or_number must be a positive integer"}
        if not isinstance(merge_method, str) or merge_method not in GITHUB_MERGE_METHOD_FLAGS:
            return {
                "status": "blocked_input",
                "error": "merge_method must be one of: squash, merge, rebase",
            }
        if not isinstance(delete_branch, bool):
            return {"status": "blocked_input", "error": "delete_branch must be a boolean"}

        normalized_sha = None
        if expected_head_sha is not None:
            if not isinstance(expected_head_sha, str):
                return {
                    "status": "blocked_input",
                    "error": "expected_head_sha must be a string when provided",
                }
            normalized_sha = expected_head_sha.strip()
            if not FULL_SHA_RE.fullmatch(normalized_sha):
                return {
                    "status": "blocked_input",
                    "error": "expected_head_sha must be a full 40-character hex commit SHA",
                }

        return {
            "status": "ok",
            "merge_method": merge_method,
            "expected_head_sha": normalized_sha,
        }

    def _collect_github_merge_preflight(
        self,
        repo: Path,
        pr_url_or_number: str | int,
        expected_head_sha: str | None,
    ) -> dict[str, Any]:
        local = self._collect_pr_sync_local_evidence(repo)
        readiness = self._build_pr_merge_readiness(
            repo=repo,
            pr_url_or_number=pr_url_or_number,
            target_branch="main",
            local=local,
        )
        pr = readiness.get("pr")
        origin = readiness.get("evidence", {}).get("origin")
        parsed_reference = readiness.get("evidence", {}).get("parsed_pr_reference")
        canonical_pr = readiness.get("evidence", {}).get("canonical_pr")

        before_evidence = {
            "local": self._github_merge_local_evidence(local),
            "pr_status": canonical_pr,
            "readiness": readiness,
        }

        if readiness["status"] == "blocked_git":
            return {
                "status": "blocked_git",
                "error": "Could not collect local git evidence",
                "readiness": readiness,
                "pr": pr,
                "before_evidence": before_evidence,
            }
        if parsed_reference and parsed_reference.get("status") != "ok":
            return {
                "status": "blocked_input",
                "error": "Invalid PR number or URL",
                "readiness": readiness,
                "pr": pr,
                "before_evidence": before_evidence,
            }
        if readiness["status"] != "ok" and not pr:
            return {
                "status": "blocked_github",
                "error": readiness["blocking_reasons"][0] if readiness["blocking_reasons"] else readiness["status"],
                "readiness": readiness,
                "pr": pr,
                "before_evidence": before_evidence,
            }

        head_sha = pr.get("headRefOid") if isinstance(pr, dict) else None
        if not isinstance(head_sha, str) or not FULL_SHA_RE.fullmatch(head_sha):
            readiness["blocking_reasons"].append("PR head SHA is missing or not a full commit SHA")
        elif expected_head_sha is not None and head_sha != expected_head_sha:
            readiness["blocking_reasons"].append("expected_head_sha does not match fresh PR head SHA")

        readiness["ready_to_consider_merge"] = not readiness["blocking_reasons"]
        before_evidence["readiness"] = readiness

        if not origin or not isinstance(origin, dict) or origin.get("status") != "ok":
            return {
                "status": "blocked_github",
                "error": "origin is not a supported GitHub remote",
                "readiness": readiness,
                "pr": pr,
                "before_evidence": before_evidence,
            }
        if not parsed_reference or parsed_reference.get("status") != "ok":
            return {
                "status": "blocked_input",
                "error": "Invalid PR number or URL",
                "readiness": readiness,
                "pr": pr,
                "before_evidence": before_evidence,
            }

        return {
            "status": "ok",
            "readiness": readiness,
            "pr": pr,
            "origin": origin,
            "merge_reference": parsed_reference["reference"],
            "before_evidence": before_evidence,
        }

    def _github_merge_local_evidence(self, local: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": local.get("status"),
            "git_errors": local.get("git_errors"),
            "current_branch": local.get("current_branch"),
            "detached": local.get("detached"),
            "dirty": local.get("dirty"),
            "head": local.get("head"),
            "status_short_branch": local.get("status_short_branch"),
            "porcelain_status": local.get("porcelain_status"),
            "current_branch_result": local.get("current_branch_result"),
            "head_result": local.get("head_result"),
        }

    def _github_pr_summary(self, pr: Any) -> dict[str, Any]:
        if not isinstance(pr, dict):
            return {
                "number": None,
                "url": None,
                "title": None,
                "baseRefName": None,
                "headRefName": None,
                "headRefOid": None,
            }
        return {
            "number": pr.get("number"),
            "url": pr.get("url"),
            "title": pr.get("title"),
            "baseRefName": pr.get("baseRefName"),
            "headRefName": pr.get("headRefName"),
            "headRefOid": pr.get("headRefOid"),
        }

    def _empty_pr_readiness(
        self,
        status: str = "ok",
        blocking_reasons: list[str] | None = None,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "ready_to_consider_merge": False,
            "number": None,
            "url": None,
            "state": None,
            "is_draft": None,
            "base_branch": None,
            "head_branch": None,
            "head_sha": None,
            "mergeable": None,
            "merge_state_status": None,
            "review_decision": None,
            "check_summary": {"status": "unknown", "total": 0},
            "merge_readiness_applicable": None,
            "pr_lifecycle_state": None,
            "post_merge_note": None,
            "local_current_branch": None,
            "local_head": None,
            "detached": None,
            "dirty": None,
            "local_branch_matches_pr_head": None,
            "local_head_matches_pr_head_sha": None,
            "blocking_reasons": blocking_reasons or [],
            "warnings": warnings or [],
            "evidence": {},
        }

    def _empty_sync_readiness(
        self,
        target_branch: Any,
        remote: str,
        status: str = "ok",
        blocking_reasons: list[str] | None = None,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "target_branch": target_branch,
            "remote": remote,
            "remote_target": f"{remote}/{target_branch}" if isinstance(target_branch, str) else None,
            "current_branch": None,
            "detached": None,
            "dirty": None,
            "local_target_sha": None,
            "remote_target_ref": None,
            "remote_target_sha": None,
            "divergence": None,
            "relation": "unknown",
            "local_target_has_unique_commits": None,
            "remote_target_has_unique_commits": None,
            "ready_to_sync_local_target": False,
            "blocking_reasons": blocking_reasons or [],
            "warnings": warnings or [],
            "suggested_operator_commands": [],
            "evidence": {},
        }

    def _collect_pr_sync_local_evidence(self, repo: Path) -> dict[str, Any]:
        short_status = self._run(repo, ["git", "status", "--short", "--branch"], timeout=20)
        porcelain_status = self._porcelain_status(repo)
        current_branch_result = self._current_branch(repo)
        current_branch = current_branch_result["stdout"].strip()
        head = self._run(repo, ["git", "rev-parse", "--verify", "HEAD"], timeout=20)
        origin_url = self._run(repo, ["git", "remote", "get-url", "origin"], timeout=20)
        remotes = self._run(repo, ["git", "remote", "-v"], timeout=20)

        git_errors: list[str] = []
        for key, result in [
            ("status_short_branch", short_status),
            ("porcelain_status", porcelain_status),
            ("current_branch_result", current_branch_result),
            ("head", head),
            ("remotes", remotes),
        ]:
            if result["returncode"] != 0:
                git_errors.append(f"git command failed while collecting {key}")

        return {
            "status": "blocked_git" if git_errors else "ok",
            "git_errors": git_errors,
            "current_branch": current_branch or None,
            "detached": current_branch_result["returncode"] != 0 or not current_branch,
            "dirty": porcelain_status["returncode"] == 0 and bool(porcelain_status["stdout"]),
            "head": head["stdout"].strip() if head["returncode"] == 0 else None,
            "origin_exists": origin_url["returncode"] == 0,
            "status_short_branch": short_status,
            "porcelain_status": porcelain_status,
            "current_branch_result": current_branch_result,
            "head_result": head,
            "origin_url": origin_url,
            "remotes": remotes,
        }

    def _initial_pr_readiness_from_local(self, local: dict[str, Any]) -> dict[str, Any]:
        blocking_reasons = list(local.get("git_errors", []))
        warnings = ["Readiness is conservative advisory evidence only."]
        readiness = self._empty_pr_readiness(
            status="blocked_git" if blocking_reasons else "ok",
            blocking_reasons=blocking_reasons,
            warnings=warnings,
        )
        readiness.update(
            {
                "local_current_branch": local.get("current_branch"),
                "local_head": local.get("head"),
                "detached": local.get("detached"),
                "dirty": local.get("dirty"),
                "evidence": {
                    "status_short_branch": local.get("status_short_branch"),
                    "porcelain_status": local.get("porcelain_status"),
                    "current_branch_result": local.get("current_branch_result"),
                    "head": local.get("head_result"),
                },
            }
        )
        return readiness

    def _finalize_pr_readiness_from_pr(
        self,
        readiness: dict[str, Any],
        pr: dict[str, Any],
        local: dict[str, Any],
        target_branch: str | None,
        suppress_merged_open_pr_blockers: bool = False,
    ) -> dict[str, Any]:
        check_summary = self._summarize_status_check_rollup(pr.get("statusCheckRollup"))
        state = pr.get("state")
        is_draft = pr.get("isDraft")
        base_branch = pr.get("baseRefName")
        head_branch = pr.get("headRefName")
        head_sha = pr.get("headRefOid")
        mergeable = pr.get("mergeable")
        merge_state_status = pr.get("mergeStateStatus")
        review_decision = pr.get("reviewDecision")
        local_branch_matches = (
            local.get("current_branch") == head_branch
            if local.get("current_branch") is not None and isinstance(head_branch, str)
            else None
        )
        local_head_matches = (
            local.get("head") == head_sha
            if local.get("head") is not None and isinstance(head_sha, str)
            else None
        )

        readiness.update(
            {
                "number": pr.get("number"),
                "url": pr.get("url"),
                "state": state,
                "is_draft": is_draft,
                "base_branch": base_branch,
                "head_branch": head_branch,
                "head_sha": head_sha,
                "mergeable": mergeable,
                "merge_state_status": merge_state_status,
                "review_decision": review_decision,
                "check_summary": check_summary,
                "merge_readiness_applicable": True,
                "pr_lifecycle_state": state.lower() if isinstance(state, str) else None,
                "post_merge_note": None,
                "local_branch_matches_pr_head": local_branch_matches,
                "local_head_matches_pr_head_sha": local_head_matches,
                "pr": pr,
            }
        )

        if state == "MERGED" and suppress_merged_open_pr_blockers:
            readiness.update(
                {
                    "ready_to_consider_merge": False,
                    "merge_readiness_applicable": False,
                    "pr_lifecycle_state": "merged",
                    "post_merge_note": (
                        "PR is already merged; merge readiness checks are not applicable. "
                        "Use local target sync readiness instead."
                    ),
                }
            )
            readiness["warnings"].append(readiness["post_merge_note"])
            return readiness

        if state != "OPEN":
            readiness["blocking_reasons"].append("PR is not open")
        if is_draft is not False:
            readiness["blocking_reasons"].append("PR is draft or draft status is unknown")
        if target_branch is not None and base_branch != target_branch:
            readiness["blocking_reasons"].append("PR base branch does not match target_branch")
        if mergeable != "MERGEABLE":
            readiness["blocking_reasons"].append("PR mergeability is not confirmed mergeable")
        if merge_state_status != "CLEAN":
            readiness["blocking_reasons"].append("PR merge state is not clean")
        if check_summary["status"] != "passing":
            readiness["blocking_reasons"].append("PR checks are not confirmed passing")
            if check_summary["status"] in {"missing", "unknown"}:
                readiness["warnings"].append("Missing or unknown check evidence is treated as not ready")
        if review_decision != "APPROVED":
            if review_decision in BLOCKING_REVIEW_DECISIONS:
                readiness["blocking_reasons"].append("PR review decision is blocking")
            else:
                readiness["blocking_reasons"].append("PR review decision is missing or unknown")
                readiness["warnings"].append("Missing review-decision evidence is treated as not ready")
        if local.get("dirty"):
            readiness["blocking_reasons"].append("Local worktree is dirty")
        if local.get("detached"):
            readiness["blocking_reasons"].append("Local repository is detached or current branch is unknown")
        if local_branch_matches is not True:
            readiness["blocking_reasons"].append("Local current branch does not match PR head branch")
        if local_head_matches is not True:
            readiness["blocking_reasons"].append("Local HEAD does not match PR head SHA")

        readiness["ready_to_consider_merge"] = not readiness["blocking_reasons"]
        return readiness

    def _build_pr_merge_readiness(
        self,
        repo: Path,
        pr_url_or_number: str | int | None,
        target_branch: str,
        local: dict[str, Any],
        suppress_merged_open_pr_blockers: bool = False,
    ) -> dict[str, Any]:
        readiness = self._initial_pr_readiness_from_local(local)
        if readiness["blocking_reasons"]:
            return readiness

        origin = self._github_origin_info(repo)
        readiness["evidence"]["origin"] = origin
        if origin["status"] != "ok":
            readiness["status"] = origin["status"]
            readiness["blocking_reasons"].append("origin is not a supported GitHub remote")
            return readiness

        gh_ready = self._github_cli_ready(repo)
        readiness["evidence"]["gh"] = gh_ready
        if gh_ready["status"] != "ok":
            readiness["status"] = gh_ready["status"]
            readiness["blocking_reasons"].append(gh_ready["error"])
            return readiness

        pr_result: dict[str, Any]
        if pr_url_or_number is None:
            current_branch = local.get("current_branch")
            if not current_branch:
                readiness["status"] = "blocked_detached_head"
                readiness["blocking_reasons"].append(
                    "Current branch could not be determined for branch PR lookup"
                )
                return readiness
            prs = self._github_prs_for_branch(repo, origin["repo_arg"], current_branch)
            readiness["evidence"]["prs_for_branch"] = prs
            if prs["status"] != "ok":
                readiness["status"] = prs["status"]
                readiness["blocking_reasons"].append(prs["error"])
                return readiness
            if not prs["prs"]:
                readiness["status"] = "no_pr"
                readiness["blocking_reasons"].append("No open PR found for current branch")
                return readiness
            if len(prs["prs"]) > 1:
                readiness["status"] = "blocked_ambiguous_pr"
                readiness["blocking_reasons"].append("Multiple open PRs matched the current branch")
                return readiness
            pr_result = {"status": "ok", "pr": prs["prs"][0]}
        else:
            parsed = self._parse_pr_reference(pr_url_or_number, origin["owner"], origin["repo"])
            readiness["evidence"]["parsed_pr_reference"] = parsed
            if parsed["status"] != "ok":
                readiness["status"] = "blocked_input"
                readiness["blocking_reasons"].append("Invalid PR number or URL")
                return readiness
            pr_result = self._github_pr_view(repo, origin["repo_arg"], parsed["reference"])
            readiness["evidence"]["canonical_pr"] = pr_result
            if pr_result["status"] != "ok":
                readiness["status"] = pr_result["status"]
                readiness["blocking_reasons"].append(pr_result["error"])
                return readiness

        return self._finalize_pr_readiness_from_pr(
            readiness=readiness,
            pr=pr_result["pr"],
            local=local,
            target_branch=target_branch,
            suppress_merged_open_pr_blockers=suppress_merged_open_pr_blockers,
        )

    def _summarize_status_check_rollup(self, rollup: Any) -> dict[str, Any]:
        if not isinstance(rollup, list):
            return {"status": "unknown", "total": 0, "passing": 0, "failing": 0, "pending": 0, "unknown": 1}
        if not rollup:
            return {"status": "missing", "total": 0, "passing": 0, "failing": 0, "pending": 0, "unknown": 0}

        counts = {"passing": 0, "failing": 0, "pending": 0, "unknown": 0}
        items: list[dict[str, Any]] = []
        for item in rollup:
            status = "unknown"
            name = None
            if isinstance(item, dict):
                name = item.get("name") or item.get("context") or item.get("workflowName")
                conclusion = self._upper_or_none(item.get("conclusion"))
                state = self._upper_or_none(item.get("state"))
                check_status = self._upper_or_none(item.get("status"))
                values = {value for value in [conclusion, state, check_status] if value}
                if values & CHECK_FAIL_VALUES:
                    status = "failing"
                elif values & CHECK_PENDING_VALUES:
                    status = "pending"
                elif conclusion in CHECK_PASS_VALUES or state == "SUCCESS":
                    status = "passing"
                elif check_status == "COMPLETED" and conclusion in CHECK_PASS_VALUES:
                    status = "passing"
            counts[status] += 1
            items.append({"name": name, "status": status})

        overall = "passing"
        if counts["failing"]:
            overall = "failing"
        elif counts["pending"]:
            overall = "pending"
        elif counts["unknown"]:
            overall = "unknown"
        return {"status": overall, "total": len(rollup), **counts, "items": items}

    def _upper_or_none(self, value: Any) -> str | None:
        return value.upper() if isinstance(value, str) and value else None

    def _build_local_sync_readiness(
        self,
        repo: Path,
        target_branch: str,
        remote: str,
        local: dict[str, Any],
    ) -> dict[str, Any]:
        blocking_reasons = list(local.get("git_errors", []))
        warnings = ["Sync readiness uses local refs only; no fetch was performed."]
        sync = self._empty_sync_readiness(
            target_branch=target_branch,
            remote=remote,
            status="ok",
            blocking_reasons=blocking_reasons,
            warnings=warnings,
        )
        sync.update(
            {
                "current_branch": local.get("current_branch"),
                "detached": local.get("detached"),
                "dirty": local.get("dirty"),
                "evidence": {
                    "status_short_branch": local.get("status_short_branch"),
                    "porcelain_status": local.get("porcelain_status"),
                    "current_branch_result": local.get("current_branch_result"),
                    "head": local.get("head_result"),
                    "origin_url": local.get("origin_url"),
                    "remotes": local.get("remotes"),
                },
            }
        )
        if blocking_reasons:
            sync["status"] = "blocked_git"
            return sync
        if local.get("dirty"):
            sync["blocking_reasons"].append("Local worktree is dirty")
        if local.get("detached"):
            sync["blocking_reasons"].append("Local repository is detached or current branch is unknown")
        if not local.get("origin_exists"):
            sync["blocking_reasons"].append("origin remote is not configured")

        local_target_ref = f"refs/heads/{target_branch}"
        remote_target_ref = f"refs/remotes/{remote}/{target_branch}"
        local_target = self._run(
            repo,
            ["git", "rev-parse", "--verify", f"{local_target_ref}^{{commit}}"],
            timeout=20,
        )
        remote_target = self._run(
            repo,
            ["git", "rev-parse", "--verify", f"{remote_target_ref}^{{commit}}"],
            timeout=20,
        )
        sync["evidence"]["local_target"] = local_target
        sync["evidence"]["remote_target"] = remote_target
        if local_target["returncode"] == 0:
            sync["local_target_sha"] = local_target["stdout"].strip()
        else:
            sync["blocking_reasons"].append("Local target branch does not exist")
        if remote_target["returncode"] == 0:
            sync["remote_target_ref"] = remote_target_ref
            sync["remote_target_sha"] = remote_target["stdout"].strip()
        else:
            sync["blocking_reasons"].append("Local origin target ref does not exist")

        if local_target["returncode"] == 0 and remote_target["returncode"] == 0:
            divergence = self._run(
                repo,
                ["git", "rev-list", "--left-right", "--count", f"{remote_target_ref}...{local_target_ref}"],
                timeout=20,
            )
            sync["evidence"]["divergence"] = divergence
            if divergence["returncode"] != 0:
                sync["blocking_reasons"].append("Could not inspect local target divergence")
            else:
                parts = divergence["stdout"].strip().split()
                if len(parts) != 2:
                    sync["blocking_reasons"].append("Could not parse local target divergence")
                else:
                    try:
                        remote_unique = int(parts[0])
                        local_unique = int(parts[1])
                    except ValueError:
                        sync["blocking_reasons"].append("Could not parse local target divergence")
                    else:
                        relation = "unknown"
                        if remote_unique == 0 and local_unique == 0:
                            relation = "equal"
                            sync["warnings"].append("No sync appears necessary; local target equals local origin target ref")
                        elif remote_unique > 0 and local_unique == 0:
                            relation = "behind"
                        elif remote_unique == 0 and local_unique > 0:
                            relation = "ahead"
                        elif remote_unique > 0 and local_unique > 0:
                            relation = "diverged"
                        sync.update(
                            {
                                "divergence": {
                                    "remote_unique_commits": remote_unique,
                                    "local_unique_commits": local_unique,
                                },
                                "relation": relation,
                                "local_target_has_unique_commits": local_unique > 0,
                                "remote_target_has_unique_commits": remote_unique > 0,
                            }
                        )
                        if local_unique > 0:
                            sync["blocking_reasons"].append(
                                "Local target branch has commits not on local origin target ref"
                            )

        sync["ready_to_sync_local_target"] = not sync["blocking_reasons"]
        if sync["ready_to_sync_local_target"]:
            sync["suggested_operator_commands"] = self._suggest_sync_commands(
                target_branch=target_branch,
                remote=remote,
                relation=sync["relation"],
            )
        return sync

    def _collect_local_sync_execution_readiness(
        self,
        repo: Path,
        target_branch: str,
        remote: str,
    ) -> dict[str, Any]:
        local = self._collect_pr_sync_local_evidence(repo)
        readiness = self._build_local_sync_readiness(
            repo=repo,
            target_branch=target_branch,
            remote=remote,
            local=local,
        )
        evidence = self._local_sync_execution_evidence(local, readiness)
        return {
            "status": readiness["status"],
            "warnings": readiness["warnings"],
            "blocking_reasons": readiness["blocking_reasons"],
            "evidence": evidence,
            "readiness": readiness,
        }

    def _local_sync_execution_evidence(
        self,
        local: dict[str, Any],
        readiness: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "current_branch": readiness.get("current_branch"),
            "head": local.get("head"),
            "dirty": readiness.get("dirty"),
            "detached": readiness.get("detached"),
            "local_target_sha": readiness.get("local_target_sha"),
            "origin_target_ref": readiness.get("remote_target_ref"),
            "origin_target_sha": readiness.get("remote_target_sha"),
            "divergence": readiness.get("divergence"),
            "relation": readiness.get("relation"),
            "local_target_has_unique_commits": readiness.get("local_target_has_unique_commits"),
            "origin_target_has_unique_commits": readiness.get("remote_target_has_unique_commits"),
            "ready_to_sync_local_target": readiness.get("ready_to_sync_local_target"),
        }

    def _local_sync_execution_ready(
        self,
        check: dict[str, Any],
        expected_relation: str,
        expected_current_branch: str | None = None,
    ) -> bool:
        evidence = check["evidence"]
        if check["status"] != "ok" or check["blocking_reasons"]:
            return False
        if evidence.get("relation") != expected_relation:
            return False
        if evidence.get("dirty") is not False or evidence.get("detached") is not False:
            return False
        if evidence.get("local_target_has_unique_commits") is not False:
            return False
        if expected_current_branch is not None and evidence.get("current_branch") != expected_current_branch:
            return False
        if not evidence.get("local_target_sha") or not evidence.get("origin_target_sha"):
            return False
        return True

    def _suggest_sync_commands(
        self,
        target_branch: str,
        remote: str,
        relation: str,
    ) -> list[str]:
        if relation == "equal":
            return [
                "git status --short --branch",
                f"git rev-list --left-right --count {remote}/{target_branch}...{target_branch}",
            ]
        return [
            f"git switch {target_branch}",
            f"git fetch {remote} --prune",
            f"git rev-list --left-right --count {remote}/{target_branch}...{target_branch}",
            f"git reset --hard {remote}/{target_branch}",
            "git status --short --branch",
        ]

    def list_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        task_dirs = sorted(self.config.server.task_dir.glob("*"), key=lambda p: p.name, reverse=True)
        items = []
        for task_dir in task_dirs[:limit]:
            meta_path = task_dir / "meta.json"
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    if meta.get("status") == "running":
                        rec = TaskRecord(
                            task_id=str(meta.get("task_id", task_dir.name)),
                            project_id=str(meta.get("project_id", "")),
                            project_path=Path(str(meta.get("project_path", ""))),
                            task_path=task_dir,
                            prompt_path=task_dir / "prompt.md",
                            stdout_path=task_dir / "stdout.jsonl",
                            stderr_path=task_dir / "stderr.log",
                            meta_path=meta_path,
                            pid_path=task_dir / "pid",
                        )
                        meta = self._refresh_running_task_meta(rec, meta)
                    items.append(meta)
                except Exception as exc:  # noqa: BLE001
                    items.append({"task_id": task_dir.name, "error": str(exc)})
        return items

    def _github_pr_local_preflight(self, repo: Path, require_clean: bool) -> dict[str, Any]:
        short_status = self._run(repo, ["git", "status", "--short", "--branch"], timeout=20)
        porcelain_status = self._porcelain_status(repo)
        head = self._run(repo, ["git", "rev-parse", "--verify", "HEAD"], timeout=20)
        current_branch_result = self._current_branch(repo)
        current_branch = current_branch_result["stdout"].strip()

        evidence = {
            "short_status": short_status,
            "porcelain_status": porcelain_status,
            "head": head,
            "current_branch_result": current_branch_result,
            "current_branch": current_branch or None,
        }
        for key in ["short_status", "porcelain_status", "head", "current_branch_result"]:
            if evidence[key]["returncode"] != 0:
                return {
                    "status": "blocked_git",
                    "error": f"git command failed while collecting {key}",
                    **evidence,
                }
        if not current_branch:
            return {
                "status": "blocked_detached_head",
                "error": "Current branch could not be determined; refusing from detached or invalid HEAD",
                **evidence,
            }
        if require_clean and porcelain_status["stdout"]:
            return {
                "status": "blocked_dirty",
                "error": "Worktree is dirty; refusing to create a GitHub PR",
                **evidence,
            }
        return {"status": "ok", **evidence}

    def _github_origin_info(self, repo: Path) -> dict[str, Any]:
        origin_url = self._run(repo, ["git", "remote", "get-url", "origin"], timeout=20)
        sanitized_result = self._sanitize_command_result(origin_url)
        if origin_url["returncode"] != 0:
            return {
                "status": "blocked_remote",
                "error": "Could not read origin remote URL",
                "origin_url": sanitized_result,
            }

        raw_url = origin_url["stdout"].strip()
        sanitized_url = self._sanitize_text(raw_url)
        match = GITHUB_HTTPS_REMOTE_RE.fullmatch(raw_url) or GITHUB_SSH_REMOTE_RE.fullmatch(raw_url)
        if not match:
            return {
                "status": "blocked_remote",
                "error": "origin is not a supported github.com remote URL",
                "origin_url": sanitized_result,
                "sanitized_origin_url": sanitized_url,
            }

        owner, repo_name = match.groups()
        repo_arg = f"{owner}/{repo_name}"
        return {
            "status": "ok",
            "owner": owner,
            "repo": repo_name,
            "repo_arg": repo_arg,
            "origin_url": sanitized_result,
            "sanitized_origin_url": sanitized_url,
        }

    def _github_cli_ready(self, repo: Path) -> dict[str, Any]:
        version = self._sanitize_command_result(self._run(repo, ["gh", "--version"], timeout=20))
        if version["returncode"] == 127:
            return {"status": "blocked_gh", "error": "gh CLI is unavailable", "version": version}
        if version["returncode"] != 0:
            return {"status": "blocked_gh", "error": "gh --version failed", "version": version}

        auth = self._sanitize_command_result(
            self._run(repo, ["gh", "auth", "status", "-h", "github.com"], timeout=20)
        )
        if auth["returncode"] != 0:
            return {
                "status": "blocked_gh_auth",
                "error": "gh auth status failed for github.com",
                "version": version,
                "auth": auth,
            }
        return {"status": "ok", "version": version, "auth": auth}

    def _github_repo_view(self, repo: Path, repo_arg: str) -> dict[str, Any]:
        result = self._gh_json(
            repo,
            ["gh", "repo", "view", repo_arg, "--json", "nameWithOwner,defaultBranchRef"],
            expected_type=dict,
        )
        if result["status"] != "ok":
            return result
        data = result["json"]
        default_ref = data.get("defaultBranchRef")
        default_branch = default_ref.get("name") if isinstance(default_ref, dict) else None
        if not isinstance(data.get("nameWithOwner"), str) or not isinstance(default_branch, str):
            return {
                "status": "blocked_gh_output",
                "error": "gh repo view returned unexpected JSON shape",
                **result,
            }
        return {**result, "name_with_owner": data["nameWithOwner"], "default_branch": default_branch}

    def _github_prs_for_branch(
        self,
        repo: Path,
        repo_arg: str,
        branch: str,
    ) -> dict[str, Any]:
        return self._gh_json(
            repo,
            [
                "gh",
                "pr",
                "list",
                "-R",
                repo_arg,
                "--head",
                branch,
                "--state",
                "open",
                "--json",
                GITHUB_PR_JSON_FIELDS,
                "--limit",
                "10",
            ],
            expected_type=list,
            list_key="prs",
        )

    def _github_pr_view(self, repo: Path, repo_arg: str, reference: str) -> dict[str, Any]:
        return self._gh_json(
            repo,
            [
                "gh",
                "pr",
                "view",
                reference,
                "-R",
                repo_arg,
                "--json",
                GITHUB_PR_JSON_FIELDS,
            ],
            expected_type=dict,
            dict_key="pr",
        )

    def _gh_json(
        self,
        repo: Path,
        cmd: list[str],
        expected_type: type,
        list_key: str | None = None,
        dict_key: str | None = None,
    ) -> dict[str, Any]:
        result = self._run(repo, cmd, timeout=60, max_chars=GH_JSON_MAX_CHARS)
        sanitized = self._sanitize_command_result(result)
        if result["returncode"] != 0:
            return {"status": "blocked_gh", "error": "gh command failed", "command": sanitized}
        try:
            parsed = json.loads(result["stdout"])
        except json.JSONDecodeError:
            return {
                "status": "blocked_gh_output",
                "error": "gh command returned invalid JSON",
                "command": sanitized,
            }
        if not isinstance(parsed, expected_type):
            return {
                "status": "blocked_gh_output",
                "error": "gh command returned unexpected JSON type",
                "command": sanitized,
                "json": parsed,
            }
        payload: dict[str, Any] = {"status": "ok", "command": sanitized, "json": parsed}
        if list_key:
            payload[list_key] = parsed
        if dict_key:
            payload[dict_key] = parsed
        return payload

    def _origin_branch_sha(self, repo: Path, branch: str) -> dict[str, Any]:
        result = self._sanitize_command_result(
            self._run(repo, ["git", "ls-remote", "--heads", "origin", branch], timeout=30)
        )
        if result["returncode"] != 0:
            return {"status": "blocked_git", "sha": None, "ls_remote": result}

        lines = [line for line in result["stdout"].splitlines() if line.strip()]
        if not lines:
            return {"status": "ok", "sha": None, "ls_remote": result}
        if len(lines) != 1:
            return {"status": "blocked_git", "sha": None, "ls_remote": result}

        parts = lines[0].split()
        expected_ref = f"refs/heads/{branch}"
        if len(parts) != 2 or parts[1] != expected_ref:
            return {"status": "blocked_git", "sha": None, "ls_remote": result}
        return {"status": "ok", "sha": parts[0], "ref": parts[1], "ls_remote": result}

    def _parse_github_pr_create_url(self, output: str, owner: str, repo: str) -> dict[str, Any]:
        line = output.strip()
        pattern = rf"^https://github\.com/{re.escape(owner)}/{re.escape(repo)}/pull/([1-9][0-9]*)$"
        match = re.fullmatch(pattern, line)
        if not match:
            return {"status": "blocked_gh_output", "url": self._sanitize_text(line), "number": None}
        return {"status": "ok", "url": line, "number": int(match.group(1))}

    def _parse_pr_reference(self, value: str | int, owner: str, repo: str) -> dict[str, Any]:
        if isinstance(value, int):
            if value <= 0:
                return {"status": "blocked_input"}
            return {"status": "ok", "reference": str(value)}

        if not isinstance(value, str):
            return {"status": "blocked_input"}
        stripped = value.strip()
        if not stripped or stripped != value:
            return {"status": "blocked_input"}
        if re.fullmatch(r"[1-9][0-9]*", stripped):
            return {"status": "ok", "reference": stripped}

        parsed = self._parse_github_pr_create_url(stripped, owner, repo)
        if parsed["status"] == "ok":
            return {"status": "ok", "reference": stripped}
        return {"status": "blocked_input"}

    def _sanitize_command_result(self, result: dict[str, Any]) -> dict[str, Any]:
        sanitized = dict(result)
        sanitized["cmd"] = self._sanitize_command_args(result.get("cmd", []))
        sanitized["stdout"] = self._sanitize_text(str(result.get("stdout", "")))
        sanitized["stderr"] = self._sanitize_text(str(result.get("stderr", "")))
        return sanitized

    def _sanitize_command_args(self, cmd: Any) -> list[str]:
        if not isinstance(cmd, list):
            return []

        args = [self._bound_command_arg(self._sanitize_text(str(part))) for part in cmd]
        if args[:3] != ["gh", "pr", "create"]:
            return args

        redacted = list(args)
        for flag, replacement in [
            ("--title", "<redacted-pr-title>"),
            ("--body", "<redacted-pr-body>"),
        ]:
            try:
                index = redacted.index(flag)
            except ValueError:
                continue
            if index + 1 < len(redacted):
                redacted[index + 1] = replacement
        return redacted

    def _bound_command_arg(self, arg: str) -> str:
        if len(arg) <= COMMAND_ARG_MAX_CHARS:
            return arg
        return f"{arg[:COMMAND_ARG_MAX_CHARS]}…<truncated>"

    def _sanitize_text(self, text: str) -> str:
        text = re.sub(
            r"https://[^/:\s@]+:[^/\s@]+@github\.com/",
            "https://<redacted>@github.com/",
            text,
        )
        text = re.sub(
            r"https://[^/\s@]+@github\.com/",
            "https://<redacted>@github.com/",
            text,
        )
        return text

    def _normalize_approved_files(self, repo: Path, files: list[str]) -> dict[str, Any]:
        approved_files: list[str] = []
        for raw in files:
            if not raw or not raw.strip():
                return {"status": "blocked_input", "error": "file paths must not be blank"}
            raw_path = Path(raw).expanduser()
            candidate = raw_path if raw_path.is_absolute() else repo / raw_path
            resolved = candidate.resolve(strict=False)
            try:
                relative = resolved.relative_to(repo)
            except ValueError:
                return {"status": "blocked_input", "error": f"File path escapes project root: {raw}"}
            if relative == Path("."):
                return {"status": "blocked_input", "error": "project root cannot be an approved file"}
            if resolved.exists() and resolved.is_dir():
                return {"status": "blocked_input", "error": f"Approved path is a directory, not a file: {raw}"}
            approved = relative.as_posix()
            if approved not in approved_files:
                approved_files.append(approved)
        if not approved_files:
            return {"status": "blocked_input", "error": "files must not be empty"}
        return {"status": "ok", "files": approved_files}

    def _current_branch(self, repo: Path) -> dict[str, Any]:
        return self._run(repo, ["git", "branch", "--show-current"], timeout=20)

    def _porcelain_status(self, repo: Path) -> dict[str, Any]:
        return self._run(
            repo,
            ["git", "status", "--porcelain=v1", "--untracked-files=normal"],
            timeout=20,
        )

    def _upstream_info(self, repo: Path, suffix: str = "") -> dict[str, Any]:
        upstream_result = self._run(
            repo,
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
            timeout=20,
        )
        upstream = upstream_result["stdout"].strip() if upstream_result["returncode"] == 0 else None
        ahead_behind = None
        ahead_behind_result: dict[str, Any] | None = None

        if upstream:
            ahead_behind_result = self._run(
                repo,
                ["git", "rev-list", "--left-right", "--count", "@{upstream}...HEAD"],
                timeout=20,
            )
            if ahead_behind_result["returncode"] == 0:
                parts = ahead_behind_result["stdout"].strip().split()
                if len(parts) == 2:
                    try:
                        behind, ahead = (int(parts[0]), int(parts[1]))
                    except ValueError:
                        ahead_behind = None
                    else:
                        ahead_behind = {"ahead": ahead, "behind": behind}
        else:
            ahead_behind_result = {
                "cmd": ["git", "rev-list", "--left-right", "--count", "@{upstream}...HEAD"],
                "returncode": None,
                "stdout": "",
                "stderr": "skipped because no upstream is configured",
            }

        return {
            f"upstream{suffix}": upstream,
            f"upstream_result{suffix}": upstream_result,
            f"ahead_behind{suffix}": ahead_behind,
            f"ahead_behind_result{suffix}": ahead_behind_result,
        }

    def _validate_branch_name(self, repo: Path, name: str, field: str) -> dict[str, Any]:
        if not isinstance(name, str):
            return {"status": "blocked_input", "error": f"{field} must be a string"}
        if name != name.strip():
            return {"status": "blocked_input", "error": f"{field} must not have surrounding whitespace"}
        if not name:
            return {"status": "blocked_input", "error": f"{field} must not be empty"}
        if len(name) > BRANCH_NAME_MAX_CHARS:
            return {"status": "blocked_input", "error": f"{field} is too long"}
        if not name.isascii():
            return {"status": "blocked_input", "error": f"{field} must be ASCII"}
        if any(char.isspace() for char in name):
            return {"status": "blocked_input", "error": f"{field} must not contain whitespace"}
        if name == "HEAD":
            return {"status": "blocked_input", "error": f"{field} must not be HEAD"}
        if name.startswith("refs/"):
            return {"status": "blocked_input", "error": f"{field} must not start with refs/"}
        if name[0] in {".", "/", "-"}:
            return {"status": "blocked_input", "error": f"{field} has an unsafe prefix"}
        if name[-1] in {".", "/"} or name.endswith(".lock"):
            return {"status": "blocked_input", "error": f"{field} has an unsafe suffix"}
        if not BRANCH_NAME_ALLOWED_RE.fullmatch(name):
            return {"status": "blocked_input", "error": f"{field} contains unsafe punctuation"}
        unsafe_tokens = ["..", "//", "@{", "\\", ":", "~", "^", "?", "*", "["]
        if any(token in name for token in unsafe_tokens):
            return {"status": "blocked_input", "error": f"{field} contains unsafe ref syntax"}

        check_ref_format = self._run(repo, ["git", "check-ref-format", "--branch", name], timeout=20)
        if check_ref_format["returncode"] != 0:
            return {
                "status": "blocked_input",
                "error": f"{field} is not a valid git branch name",
                "check_ref_format": check_ref_format,
            }
        return {"status": "ok", "name": name, "check_ref_format": check_ref_format}

    def _local_branch_exists(self, repo: Path, branch_name: str) -> dict[str, Any]:
        result = self._run(
            repo,
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
            timeout=20,
        )
        if result["returncode"] == 0:
            return {"status": "ok", "exists": True, "show_ref": result}
        if result["returncode"] == 1:
            return {"status": "ok", "exists": False, "show_ref": result}
        return {"status": "blocked_git", "exists": None, "show_ref": result}

    def _is_remote_style_branch(self, repo: Path, branch_name: str) -> dict[str, Any]:
        remotes = self._run(repo, ["git", "remote"], timeout=20)
        if remotes["returncode"] != 0:
            return {"status": "blocked_git", "remote_style": None, "remotes": remotes}
        remote_names = {line.strip() for line in remotes["stdout"].splitlines() if line.strip()}
        remote_names.update({"origin", "upstream"})
        first_segment = branch_name.split("/", 1)[0]
        return {
            "status": "ok",
            "remote_style": "/" in branch_name and first_segment in remote_names,
            "remotes": remotes,
        }

    def _staged_files(self, repo: Path) -> dict[str, Any]:
        result = self._run(
            repo,
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACDMRTUXB"],
            timeout=20,
        )
        if result["returncode"] == 0:
            result["files"] = sorted(line for line in result["stdout"].splitlines() if line)
        else:
            result["files"] = []
        return result

    def _parse_name_status_z(self, output: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        tokens = [token for token in output.split("\0") if token]
        index = 0
        while index < len(tokens):
            status_token = tokens[index]
            index += 1
            tab_parts = status_token.split("\t")
            status = tab_parts[0]
            code = status[:1] if status else "?"

            old_path = None
            if code in {"R", "C"}:
                if len(tab_parts) >= 3:
                    old_path = tab_parts[1]
                    path = tab_parts[2]
                elif index + 1 < len(tokens):
                    old_path = tokens[index]
                    path = tokens[index + 1]
                    index += 2
                else:
                    path = ""
            elif len(tab_parts) >= 2:
                path = tab_parts[1]
            elif index < len(tokens):
                path = tokens[index]
                index += 1
            else:
                path = ""

            if path:
                records.append(
                    {
                        "status": status,
                        "code": code,
                        "path": path,
                        "old_path": old_path,
                    }
                )
        return records

    def _parse_numstat_z(self, output: str) -> dict[str, dict[str, Any]]:
        stats: dict[str, dict[str, Any]] = {}
        tokens = [token for token in output.split("\0") if token]
        index = 0
        while index < len(tokens):
            token = tokens[index]
            index += 1
            parts = token.split("\t")
            if len(parts) < 3:
                continue

            added_raw, deleted_raw, path = parts[0], parts[1], parts[2]
            old_path = None
            if path == "" and index + 1 < len(tokens):
                old_path = tokens[index]
                path = tokens[index + 1]
                index += 2
            if not path:
                continue

            binary = added_raw == "-" and deleted_raw == "-"
            added: int | None = None
            deleted: int | None = None
            if not binary:
                try:
                    added = int(added_raw)
                    deleted = int(deleted_raw)
                except ValueError:
                    added = None
                    deleted = None

            stats[path] = {
                "path": path,
                "old_path": old_path,
                "binary": binary,
                "added_lines": added,
                "deleted_lines": deleted,
            }
        return stats

    def _parse_porcelain_status_z(self, output: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        tokens = [token for token in output.split("\0") if token]
        index = 0
        while index < len(tokens):
            token = tokens[index]
            index += 1
            if len(token) < 3:
                continue
            x_status = token[0]
            y_status = token[1]
            path = token[3:] if token[2] == " " else token[2:]
            old_path = None
            if x_status in {"R", "C"} or y_status in {"R", "C"}:
                if index < len(tokens):
                    old_path = tokens[index]
                    index += 1
            if path:
                records.append(
                    {
                        "path": path,
                        "old_path": old_path,
                        "index_status": x_status,
                        "worktree_status": y_status,
                    }
                )
        return records

    def _validate_changed_file_diff_source(self, source: str) -> dict[str, Any]:
        if not isinstance(source, str):
            return {"status": "blocked_input", "error": "source must be a string"}
        allowed = {"auto", "unstaged", "staged", "untracked"}
        if source not in allowed:
            return {
                "status": "blocked_input",
                "error": "source must be one of auto, unstaged, staged, untracked",
                "allowed_sources": sorted(allowed),
            }
        return {"status": "ok", "source": source}

    def _validate_changed_file_text_source(self, source: str) -> dict[str, Any]:
        if not isinstance(source, str):
            return {"status": "blocked_input", "error": "source must be a string"}
        allowed = {"auto", "worktree", "unstaged", "staged", "untracked"}
        if source not in allowed:
            return {
                "status": "blocked_input",
                "error": "source must be one of auto, worktree, unstaged, staged, untracked",
                "allowed_sources": sorted(allowed),
            }
        return {"status": "ok", "source": source}

    def _normalize_changed_file_diff_path(self, repo: Path, path: str) -> dict[str, Any]:
        if not isinstance(path, str):
            return {"status": "blocked_input", "error": "path must be a string"}
        if not path.strip():
            return {"status": "blocked_input", "error": "path must not be empty"}
        if path != path.strip():
            return {"status": "blocked_input", "error": "path must not have surrounding whitespace"}

        raw_path = Path(path)
        if raw_path.is_absolute():
            return {"status": "blocked_input", "error": "path must be repo-relative"}
        normalized = posixpath.normpath(path.replace("\\", "/"))
        if normalized in {"", "."}:
            return {"status": "blocked_input", "error": "path must target one file, not the repo root"}
        if normalized == ".." or normalized.startswith("../"):
            return {"status": "blocked_input", "error": "path must stay inside the repo"}

        repo_resolved = repo.resolve()
        candidate = repo / normalized
        resolved_parent = candidate.parent.resolve(strict=False)
        try:
            resolved_parent.relative_to(repo_resolved)
        except ValueError:
            return {"status": "blocked_input", "error": "path must stay inside the repo"}
        return {"status": "ok", "path": normalized}

    def _target_changed_file_state(self, repo: Path, path: str) -> dict[str, Any]:
        porcelain_status_raw = self._git_z_full(
            repo,
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=normal", "--", path],
            timeout=20,
        )
        unstaged_name_status_raw = self._git_z_full(
            repo,
            ["git", "diff", "--name-status", "-z", "--diff-filter=ACDMRTUXB", "--", path],
            timeout=20,
        )
        staged_name_status_raw = self._git_z_full(
            repo,
            ["git", "diff", "--cached", "--name-status", "-z", "--diff-filter=ACDMRTUXB", "--", path],
            timeout=20,
        )
        unstaged_numstat_raw = self._git_z_full(
            repo,
            ["git", "diff", "--numstat", "-z", "--", path],
            timeout=20,
        )
        staged_numstat_raw = self._git_z_full(
            repo,
            ["git", "diff", "--cached", "--numstat", "-z", "--", path],
            timeout=20,
        )
        unstaged_raw = self._git_z_full(
            repo,
            ["git", "diff", "--raw", "-z", "--", path],
            timeout=20,
        )
        staged_raw = self._git_z_full(
            repo,
            ["git", "diff", "--cached", "--raw", "-z", "--", path],
            timeout=20,
        )
        untracked_raw = self._git_z_full(
            repo,
            ["git", "ls-files", "--others", "--exclude-standard", "-z", "--", path],
            timeout=20,
        )

        evidence = {
            "porcelain_status_z": porcelain_status_raw["evidence"],
            "unstaged_name_status": unstaged_name_status_raw["evidence"],
            "staged_name_status": staged_name_status_raw["evidence"],
            "unstaged_numstat": unstaged_numstat_raw["evidence"],
            "staged_numstat": staged_numstat_raw["evidence"],
            "unstaged_raw": unstaged_raw["evidence"],
            "staged_raw": staged_raw["evidence"],
            "untracked_files": untracked_raw["evidence"],
        }
        raw_results = {
            "porcelain_status_z": porcelain_status_raw,
            "unstaged_name_status": unstaged_name_status_raw,
            "staged_name_status": staged_name_status_raw,
            "unstaged_numstat": unstaged_numstat_raw,
            "staged_numstat": staged_numstat_raw,
            "unstaged_raw": unstaged_raw,
            "staged_raw": staged_raw,
            "untracked_files": untracked_raw,
        }
        for key, result in raw_results.items():
            if result["returncode"] != 0:
                return {
                    "status": "blocked_git",
                    "error": f"git command failed while collecting {key}",
                    "evidence": evidence,
                }

        staged_records = [
            record
            for record in self._parse_name_status_z(staged_name_status_raw["stdout"])
            if record["path"] == path
        ]
        unstaged_records = [
            record
            for record in self._parse_name_status_z(unstaged_name_status_raw["stdout"])
            if record["path"] == path
        ]
        untracked_files = [
            item for item in untracked_raw["stdout"].split("\0") if item and item == path
        ]
        staged_numstats = self._parse_numstat_z(staged_numstat_raw["stdout"])
        unstaged_numstats = self._parse_numstat_z(unstaged_numstat_raw["stdout"])
        staged_numstat = staged_numstats.get(path)
        unstaged_numstat = unstaged_numstats.get(path)
        numstats = [item for item in [staged_numstat, unstaged_numstat] if item]

        staged_codes = [record["code"] for record in staged_records]
        unstaged_codes = [record["code"] for record in unstaged_records]
        untracked = bool(untracked_files)
        old_path = next((record.get("old_path") for record in staged_records if record.get("old_path")), None)
        if not old_path:
            old_path = next(
                (record.get("old_path") for record in unstaged_records if record.get("old_path")),
                None,
            )

        binary_text_status = self._binary_text_status(numstats)
        if untracked:
            preview = self._safe_untracked_preview_summary(repo, path)
            binary_text_status = preview["binary_text_status"]
        staged = bool(staged_records)
        unstaged = bool(unstaged_records)
        change_type = self._review_change_type(
            staged_codes=staged_codes,
            unstaged_codes=unstaged_codes,
            untracked=untracked,
        )
        file_item = {
            "path": path,
            "change_type": change_type,
            "staged": staged,
            "unstaged": unstaged,
            "untracked": untracked,
            "binary_text_status": binary_text_status,
            "diff_available": (
                (untracked and binary_text_status == "text")
                or (not untracked and binary_text_status == "text" and (staged or unstaged))
            ),
        }
        if old_path:
            file_item["old_path"] = old_path

        return {
            "status": "ok",
            "file": file_item,
            "staged_records": staged_records,
            "unstaged_records": unstaged_records,
            "staged_numstat": staged_numstat,
            "unstaged_numstat": unstaged_numstat,
            "untracked_files": untracked_files,
            "evidence": evidence,
        }

    def _targeted_path_safety(self, repo: Path, path: str, state: dict[str, Any]) -> dict[str, Any]:
        candidate = repo / path
        missing = not candidate.exists()
        if missing:
            if not (state["file"]["staged"] or state["file"]["unstaged"] or state["file"]["untracked"]):
                return {"status": "ok"}
            deletion = state["file"]["change_type"] == "deleted" and (
                state["file"]["staged"] or state["file"]["unstaged"]
            )
            if deletion:
                return {"status": "ok"}
            return {"status": "blocked_unsafe", "reason": "missing"}
        if candidate.is_symlink():
            return {"status": "blocked_unsafe", "reason": "symlink"}
        if candidate.is_dir():
            return {"status": "blocked_unsafe", "reason": "directory"}
        if not candidate.is_file():
            return {"status": "blocked_unsafe", "reason": "non_regular_file"}
        return {"status": "ok"}

    def _resolve_changed_file_diff_source(
        self,
        state: dict[str, Any],
        source: str,
    ) -> dict[str, Any]:
        file_item = state["file"]
        available_sources = []
        if file_item["staged"]:
            available_sources.append("staged")
        if file_item["unstaged"]:
            available_sources.append("unstaged")
        if file_item["untracked"]:
            available_sources.append("untracked")

        if not available_sources:
            return {
                "status": "blocked_unchanged",
                "error": "path is not currently changed, staged, or untracked",
                "available_sources": [],
            }

        if source != "auto":
            if source not in available_sources:
                return {
                    "status": "blocked_unchanged",
                    "error": f"path has no {source} change",
                    "available_sources": available_sources,
                }
            return {"status": "ok", "source": source, "available_sources": available_sources}

        tracked_sources = [item for item in available_sources if item in {"staged", "unstaged"}]
        if len(tracked_sources) > 1:
            return {
                "status": "blocked_ambiguous_source",
                "error": "path has both staged and unstaged changes",
                "available_sources": ["staged", "unstaged"],
            }
        return {"status": "ok", "source": available_sources[0], "available_sources": available_sources}

    def _targeted_diff_safety_check(
        self,
        repo: Path,
        path: str,
        source: str,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        if source == "untracked":
            candidate_result = self._untracked_preview_candidate(repo, path)
            if candidate_result["status"] != "ok":
                return {"status": "blocked_unsafe", "reason": candidate_result["reason"]}
            preview = self._read_untracked_preview(candidate_result["path"])
            if preview["status"] != "included":
                return {"status": "blocked_unsafe", "reason": preview["reason"]}
            return {"status": "ok"}

        raw_key = "staged_raw" if source == "staged" else "unstaged_raw"
        raw_stdout = state["evidence"][raw_key]["stdout"]
        if "120000" in raw_stdout:
            return {"status": "blocked_unsafe", "reason": "symlink"}
        numstat = state["staged_numstat"] if source == "staged" else state["unstaged_numstat"]
        if numstat and numstat["binary"]:
            return {
                "status": "blocked_unsafe",
                "reason": "binary",
                "summary": {"path": path, "source": source, "binary_text_status": "binary"},
            }
        return {"status": "ok"}

    def _run_targeted_changed_file_diff(
        self,
        repo: Path,
        path: str,
        source: str,
        max_chars: int,
    ) -> dict[str, Any]:
        if source == "staged":
            cmd = ["git", "diff", "--cached", "--", path]
        elif source == "unstaged":
            cmd = ["git", "diff", "--", path]
        else:
            cmd = ["git", "diff", "--no-index", "--", os.devnull, path]
        return self._run_prefix(repo, cmd, timeout=20, max_chars=max_chars)

    def _changed_file_text_initial_path_safety(self, repo: Path, path: str) -> dict[str, Any]:
        candidate = repo / path
        if not candidate.exists():
            return {"status": "ok"}
        if candidate.is_symlink():
            return {"status": "blocked_unsafe", "reason": "symlink"}
        if candidate.is_dir():
            return {"status": "blocked_unsafe", "reason": "directory"}
        if not candidate.is_file():
            return {"status": "blocked_unsafe", "reason": "non_regular_file"}
        return {"status": "ok"}

    def _resolve_changed_file_text_source(
        self,
        state: dict[str, Any],
        source: str,
    ) -> dict[str, Any]:
        file_item = state["file"]
        if file_item["untracked"]:
            available_sources = ["untracked"]
        else:
            available_sources = []
            if file_item["staged"]:
                available_sources.append("staged")
            if file_item["staged"] or file_item["unstaged"]:
                available_sources.append("worktree")

        if not available_sources:
            return {
                "status": "blocked_unchanged",
                "error": "path is not currently changed, staged, or untracked",
                "available_sources": [],
            }

        if source != "auto":
            source_resolved = "worktree" if source == "unstaged" else source
            if source_resolved not in available_sources:
                return {
                    "status": "blocked_unchanged",
                    "error": f"path has no {source} content source",
                    "available_sources": available_sources,
                }
            return {"status": "ok", "source": source_resolved, "available_sources": available_sources}

        if file_item["untracked"]:
            return {"status": "ok", "source": "untracked", "available_sources": available_sources}
        if file_item["staged"] and file_item["unstaged"]:
            return {
                "status": "blocked_ambiguous_source",
                "error": "path has both staged and worktree changes",
                "available_sources": ["staged", "worktree"],
            }
        if file_item["staged"]:
            return {"status": "ok", "source": "staged", "available_sources": available_sources}
        return {"status": "ok", "source": "worktree", "available_sources": available_sources}

    def _targeted_text_safety_check(
        self,
        repo: Path,
        path: str,
        source: str,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        file_item = state["file"]
        if file_item["change_type"] == "deleted":
            return {"status": "blocked_deleted", "reason": "deleted"}

        if source == "untracked":
            candidate_result = self._untracked_preview_candidate(repo, path)
            if candidate_result["status"] != "ok":
                return {"status": "blocked_unsafe", "reason": candidate_result["reason"]}
            return {"status": "ok"}

        if source == "worktree":
            safety = self._targeted_path_safety(repo, path, state)
            if safety["status"] != "ok":
                return {"status": "blocked_unsafe", "reason": safety["reason"]}
            if file_item["binary_text_status"] == "binary":
                return {"status": "blocked_unsafe", "reason": "binary"}
            return {"status": "ok"}

        raw_stdout = state["evidence"]["staged_raw"]["stdout"]
        if "120000" in raw_stdout:
            return {"status": "blocked_unsafe", "reason": "symlink"}
        if "160000" in raw_stdout:
            return {"status": "blocked_unsafe", "reason": "gitlink"}
        numstat = state["staged_numstat"]
        if numstat and numstat["binary"]:
            return {"status": "blocked_unsafe", "reason": "binary"}
        return {"status": "ok"}

    def _changed_file_text_read_budget(self, max_chars: int, file_size: int | None) -> int:
        budget = max(
            CHANGED_FILE_TEXT_MIN_READ_BYTES,
            (max_chars * 4) + CHANGED_FILE_TEXT_UTF8_BOUNDARY_BYTES,
        )
        budget = min(budget, CHANGED_FILE_TEXT_MAX_READ_BYTES)
        if file_size is not None:
            budget = min(budget, file_size)
        return max(0, budget)

    def _read_changed_worktree_text(
        self,
        repo: Path,
        path: str,
        max_chars: int,
    ) -> dict[str, Any]:
        candidate = repo / path
        read_result = self._read_changed_filesystem_text(candidate, max_chars=max_chars)
        if "evidence" not in read_result:
            read_result["evidence"] = {}
        read_result["evidence"]["worktree_read"] = {
            "path": path,
            "source": "worktree",
            "bytes_read": read_result.get("bytes_read", 0),
            "file_size_bytes": read_result.get("file_size_bytes"),
        }
        return read_result

    def _read_changed_untracked_text(
        self,
        repo: Path,
        path: str,
        max_chars: int,
    ) -> dict[str, Any]:
        candidate_result = self._untracked_preview_candidate(repo, path)
        if candidate_result["status"] != "ok":
            return {"status": "blocked_unsafe", "reason": candidate_result["reason"]}
        read_result = self._read_changed_filesystem_text(
            candidate_result["path"],
            max_chars=max_chars,
        )
        if "evidence" not in read_result:
            read_result["evidence"] = {}
        read_result["evidence"]["untracked_read"] = {
            "path": path,
            "source": "untracked",
            "bytes_read": read_result.get("bytes_read", 0),
            "file_size_bytes": read_result.get("file_size_bytes"),
        }
        return read_result

    def _read_changed_filesystem_text(self, path: Path, max_chars: int) -> dict[str, Any]:
        try:
            file_size = path.stat().st_size
            read_size = self._changed_file_text_read_budget(max_chars, file_size)
            with path.open("rb") as handle:
                sample = handle.read(read_size)
        except OSError:
            return {"status": "blocked_unsafe", "reason": "unreadable"}

        decoded = self._decode_changed_file_text_sample(
            sample,
            max_chars=max_chars,
            bytes_read=len(sample),
            file_size_bytes=file_size,
        )
        decoded["bytes_read"] = len(sample)
        decoded["file_size_bytes"] = file_size
        return decoded

    def _read_changed_staged_text(
        self,
        repo: Path,
        path: str,
        max_chars: int,
    ) -> dict[str, Any]:
        entry_result = self._staged_index_blob_entry(repo, path)
        if entry_result["status"] != "ok":
            return entry_result

        oid = entry_result["entry"]["oid"]
        size_result = self._run(repo, ["git", "cat-file", "-s", oid], timeout=20, max_chars=1000)
        evidence = {
            "staged_ls_files": entry_result["evidence"]["staged_ls_files"],
            "staged_cat_file_size": size_result,
        }
        if size_result["returncode"] != 0:
            return {
                "status": "blocked_git",
                "reason": "cat_file_size_failed",
                "evidence": evidence,
            }
        try:
            blob_size = int(size_result["stdout"].strip())
        except ValueError:
            return {"status": "blocked_git", "reason": "invalid_blob_size", "evidence": evidence}

        read_size = self._changed_file_text_read_budget(max_chars, blob_size)
        blob_result = self._read_git_blob_prefix(repo, oid, max_bytes=read_size, blob_size=blob_size)
        evidence["staged_cat_file_blob"] = blob_result["evidence"]
        if blob_result["status"] != "ok":
            return {
                "status": "blocked_git",
                "reason": blob_result["reason"],
                "evidence": evidence,
            }

        decoded = self._decode_changed_file_text_sample(
            blob_result["stdout_bytes"],
            max_chars=max_chars,
            bytes_read=blob_result["bytes_read"],
            file_size_bytes=blob_size,
        )
        decoded["bytes_read"] = blob_result["bytes_read"]
        decoded["file_size_bytes"] = blob_size
        decoded["evidence"] = evidence
        return decoded

    def _staged_index_blob_entry(self, repo: Path, path: str) -> dict[str, Any]:
        result = self._git_z_full(
            repo,
            ["git", "ls-files", "-s", "-z", "--", path],
            timeout=20,
        )
        evidence = {"staged_ls_files": result["evidence"]}
        if result["returncode"] != 0:
            return {"status": "blocked_git", "reason": "ls_files_failed", "evidence": evidence}

        entries = []
        for token in [item for item in result["stdout"].split("\0") if item]:
            metadata, separator, entry_path = token.partition("\t")
            if not separator or entry_path != path:
                continue
            parts = metadata.split()
            if len(parts) != 3:
                return {
                    "status": "blocked_unsafe",
                    "reason": "invalid_index_entry",
                    "evidence": evidence,
                }
            entries.append(
                {
                    "mode": parts[0],
                    "oid": parts[1],
                    "stage": parts[2],
                    "path": entry_path,
                }
            )

        if not entries:
            return {"status": "blocked_no_content", "reason": "no_index_blob", "evidence": evidence}
        if any(entry["stage"] != "0" for entry in entries):
            return {"status": "blocked_conflict", "reason": "multi_stage_index", "evidence": evidence}
        if len(entries) != 1:
            return {
                "status": "blocked_unsafe",
                "reason": "ambiguous_index_entries",
                "evidence": evidence,
            }

        entry = entries[0]
        if entry["mode"] == "120000":
            return {"status": "blocked_unsafe", "reason": "symlink", "evidence": evidence}
        if entry["mode"] == "160000":
            return {"status": "blocked_unsafe", "reason": "gitlink", "evidence": evidence}
        if not entry["mode"].startswith("100"):
            return {"status": "blocked_unsafe", "reason": "non_regular_file", "evidence": evidence}
        return {"status": "ok", "entry": entry, "evidence": evidence}

    def _read_git_blob_prefix(
        self,
        repo: Path,
        oid: str,
        *,
        max_bytes: int,
        blob_size: int,
    ) -> dict[str, Any]:
        cmd = ["git", "cat-file", "blob", oid]
        proc: subprocess.Popen[bytes] | None = None
        stdout_bytes = b""
        stderr_bytes = b""
        terminated_after_prefix = False
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(repo),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
            if proc.stdout is not None:
                stdout_bytes = proc.stdout.read(max_bytes)
            if blob_size > len(stdout_bytes) and proc.poll() is None:
                terminated_after_prefix = True
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                terminated_after_prefix = True
                proc.kill()
                proc.wait(timeout=5)
            if proc.stderr is not None:
                stderr_bytes = proc.stderr.read(4096)
        except FileNotFoundError as exc:
            return {
                "status": "blocked_git",
                "reason": "git_not_found",
                "stdout_bytes": b"",
                "bytes_read": 0,
                "evidence": {
                    "cmd": cmd,
                    "returncode": 127,
                    "stderr": str(exc),
                    "bytes_read": 0,
                    "terminated_after_prefix": False,
                },
            }
        finally:
            if proc is not None:
                if proc.stdout is not None:
                    proc.stdout.close()
                if proc.stderr is not None:
                    proc.stderr.close()

        returncode = proc.returncode if proc is not None and proc.returncode is not None else 0
        evidence = {
            "cmd": cmd,
            "returncode": returncode,
            "stderr": stderr_bytes.decode("utf-8", errors="replace"),
            "bytes_read": len(stdout_bytes),
            "blob_size_bytes": blob_size,
            "terminated_after_prefix": terminated_after_prefix,
        }
        if returncode != 0 and not terminated_after_prefix:
            return {
                "status": "blocked_git",
                "reason": "cat_file_blob_failed",
                "stdout_bytes": b"",
                "bytes_read": 0,
                "evidence": evidence,
            }
        return {
            "status": "ok",
            "stdout_bytes": stdout_bytes,
            "bytes_read": len(stdout_bytes),
            "evidence": evidence,
        }

    def _decode_changed_file_text_sample(
        self,
        sample: bytes,
        *,
        max_chars: int,
        bytes_read: int,
        file_size_bytes: int | None,
    ) -> dict[str, Any]:
        if b"\0" in sample:
            return {"status": "blocked_unsafe", "reason": "binary"}

        byte_truncated = file_size_bytes is None or file_size_bytes > bytes_read
        boundary_trimmed = False
        try:
            decoded = sample.decode("utf-8")
        except UnicodeDecodeError as exc:
            if byte_truncated and exc.reason == "unexpected end of data":
                try:
                    decoded = sample[: exc.start].decode("utf-8")
                except UnicodeDecodeError:
                    return {"status": "blocked_unsafe", "reason": "invalid_utf8"}
                boundary_trimmed = True
            else:
                return {"status": "blocked_unsafe", "reason": "invalid_utf8"}

        text = decoded[:max_chars] if max_chars else ""
        char_truncated = len(decoded) > len(text)
        truncated = byte_truncated or boundary_trimmed or char_truncated
        returned_bytes = len(text.encode("utf-8"))
        omitted_bytes = (
            max(0, file_size_bytes - returned_bytes) if file_size_bytes is not None else None
        )
        omitted_chars = None
        if not byte_truncated and not boundary_trimmed:
            omitted_chars = max(0, len(decoded) - len(text))

        return {
            "status": "ok",
            "content": {
                "text": text,
                "encoding": "utf-8",
                "chars": len(text),
                "bytes_read": bytes_read,
                "file_size_bytes": file_size_bytes,
            },
            "truncation": {
                "truncated": truncated,
                "chars": len(text),
                "max_chars": max_chars,
                "omitted_chars": omitted_chars,
                "omitted_bytes": omitted_bytes,
            },
        }

    def _changed_file_table(
        self,
        *,
        repo: Path,
        staged_records: list[dict[str, Any]],
        unstaged_records: list[dict[str, Any]],
        staged_numstats: dict[str, dict[str, Any]],
        unstaged_numstats: dict[str, dict[str, Any]],
        porcelain_records: list[dict[str, Any]],
        untracked_files: list[str],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        files_by_path: dict[str, dict[str, Any]] = {}

        def item_for(path: str) -> dict[str, Any]:
            if path not in files_by_path:
                files_by_path[path] = {
                    "path": path,
                    "old_path": None,
                    "staged_codes": [],
                    "unstaged_codes": [],
                    "untracked": False,
                    "numstats": [],
                    "reason_codes": [],
                }
            return files_by_path[path]

        for record in staged_records:
            item = item_for(record["path"])
            item["staged_codes"].append(record["code"])
            item["old_path"] = item["old_path"] or record.get("old_path")

        for record in unstaged_records:
            item = item_for(record["path"])
            item["unstaged_codes"].append(record["code"])
            item["old_path"] = item["old_path"] or record.get("old_path")

        for path in untracked_files:
            item = item_for(path)
            item["untracked"] = True

        for record in porcelain_records:
            item = item_for(record["path"])
            item["old_path"] = item["old_path"] or record.get("old_path")
            if record["index_status"] not in {" ", "?"}:
                code = record["index_status"]
                if code not in item["staged_codes"]:
                    item["staged_codes"].append(code)
            if record["worktree_status"] not in {" ", "?"}:
                code = record["worktree_status"]
                if code not in item["unstaged_codes"]:
                    item["unstaged_codes"].append(code)
            if record["index_status"] == "?" and record["worktree_status"] == "?":
                item["untracked"] = True

        for stats in [staged_numstats, unstaged_numstats]:
            for path, numstat in stats.items():
                item_for(path)["numstats"].append(numstat)

        preview_items: list[dict[str, Any]] = []
        omitted_preview_count = 0
        untracked_preview_count = 0
        changed_files: list[dict[str, Any]] = []

        for path in sorted(files_by_path):
            raw = files_by_path[path]
            staged = bool(raw["staged_codes"])
            unstaged = bool(raw["unstaged_codes"])
            untracked = bool(raw["untracked"])
            reason_codes: list[str] = []
            reason_codes.extend(f"staged_{code}" for code in raw["staged_codes"])
            reason_codes.extend(f"unstaged_{code}" for code in raw["unstaged_codes"])
            if staged and unstaged:
                reason_codes.append("mixed_staged_unstaged")
            if untracked:
                reason_codes.append("untracked")

            change_type = self._review_change_type(
                staged_codes=raw["staged_codes"],
                unstaged_codes=raw["unstaged_codes"],
                untracked=untracked,
            )
            binary_text_status = self._binary_text_status(raw["numstats"])

            preview_summary: dict[str, Any] | None = None
            safe_preview_available = False
            if untracked:
                if untracked_preview_count < UNTRACKED_PREVIEW_MAX_FILES:
                    preview_summary = self._safe_untracked_preview_summary(repo, path)
                    untracked_preview_count += 1
                else:
                    preview_summary = {
                        "path": path,
                        "status": "skipped",
                        "reason": "preview_limit",
                        "safe_preview_available": False,
                        "binary_text_status": "unknown",
                        "excerpt_included": False,
                    }
                    omitted_preview_count += 1
                preview_items.append(preview_summary)
                safe_preview_available = preview_summary["safe_preview_available"]
                if preview_summary["status"] == "skipped":
                    reason_codes.append(f"untracked_preview_{preview_summary['reason']}")
                if preview_summary["binary_text_status"] != "unknown":
                    binary_text_status = preview_summary["binary_text_status"]

            binary = binary_text_status == "binary"
            diff_available = (
                (untracked and safe_preview_available)
                or (not untracked and binary_text_status == "text" and (staged or unstaged))
            )
            suggested_diff_source = self._suggested_diff_source(
                staged=staged,
                unstaged=unstaged,
                untracked=untracked,
                diff_available=diff_available,
            )
            likely_needs_targeted_review = diff_available or binary or change_type in {
                "renamed",
                "copied",
                "deleted",
                "unknown",
            } or (staged and unstaged)

            if diff_available:
                reason_codes.append("targeted_diff_available")
            if binary:
                reason_codes.append("binary")
            if likely_needs_targeted_review:
                reason_codes.append("likely_needs_targeted_review")

            entry = {
                "path": path,
                "change_type": change_type,
                "staged": staged,
                "unstaged": unstaged,
                "untracked": untracked,
                "binary_text_status": binary_text_status,
                "safe_preview_available": safe_preview_available,
                "diff_available": diff_available,
                "suggested_diff_source": suggested_diff_source,
                "likely_needs_targeted_review": likely_needs_targeted_review,
                "reason_codes": sorted(set(reason_codes)),
            }
            if raw["old_path"]:
                entry["old_path"] = raw["old_path"]
            changed_files.append(entry)

        untracked_previews = {
            "items": preview_items,
            "truncated": omitted_preview_count > 0,
            "omitted_count": omitted_preview_count,
            "policy": "full file contents are never included; excerpts are partial and bounded",
            "limits": {
                "max_files": UNTRACKED_PREVIEW_MAX_FILES,
                "max_excerpt_chars": REVIEW_PACKAGE_UNTRACKED_EXCERPT_MAX_CHARS,
                "max_bytes_per_file": UNTRACKED_PREVIEW_MAX_BYTES_PER_FILE,
            },
        }
        return changed_files, untracked_previews

    def _review_change_type(
        self,
        *,
        staged_codes: list[str],
        unstaged_codes: list[str],
        untracked: bool,
    ) -> str:
        if untracked:
            return "untracked"
        codes = staged_codes + unstaged_codes
        if any(code == "R" for code in codes):
            return "renamed"
        if any(code == "C" for code in codes):
            return "copied"
        if any(code == "T" for code in codes):
            return "type_changed"
        if codes and all(code == "D" for code in codes):
            return "deleted"
        if any(code == "A" for code in codes):
            return "added"
        if any(code == "M" for code in codes):
            return "modified"
        if any(code == "D" for code in codes):
            return "deleted"
        return "unknown"

    def _binary_text_status(self, numstats: list[dict[str, Any]]) -> str:
        if any(item["binary"] for item in numstats):
            return "binary"
        if any(item["added_lines"] is not None and item["deleted_lines"] is not None for item in numstats):
            return "text"
        return "unknown"

    def _suggested_diff_source(
        self,
        *,
        staged: bool,
        unstaged: bool,
        untracked: bool,
        diff_available: bool,
    ) -> str:
        if not diff_available:
            return "none"
        if untracked:
            return "untracked"
        if staged and unstaged:
            return "auto"
        if staged:
            return "staged"
        if unstaged:
            return "unstaged"
        return "none"

    def _safe_untracked_preview_summary(self, repo: Path, path: str) -> dict[str, Any]:
        base = {
            "path": path,
            "safe_preview_available": False,
            "binary_text_status": "unknown",
            "excerpt_included": False,
        }
        candidate_result = self._untracked_preview_candidate(repo, path)
        if candidate_result["status"] != "ok":
            return {
                **base,
                "status": "skipped",
                "reason": candidate_result["reason"],
            }

        preview = self._read_untracked_preview(candidate_result["path"])
        if preview["status"] != "included":
            binary_text_status = "binary" if preview["reason"] == "binary" else "unknown"
            return {
                **base,
                "status": "skipped",
                "reason": preview["reason"],
                "binary_text_status": binary_text_status,
            }

        result = {
            **base,
            "status": "available",
            "safe_preview_available": True,
            "binary_text_status": "text",
            "bytes_read": preview["bytes_read"],
            "preview_truncated": preview["truncated"],
            "full_content_omitted": True,
        }
        if preview["truncated"]:
            excerpt = preview["text"][:REVIEW_PACKAGE_UNTRACKED_EXCERPT_MAX_CHARS]
            result.update(
                {
                    "excerpt_included": True,
                    "excerpt": excerpt,
                    "excerpt_chars": len(excerpt),
                    "excerpt_is_partial": True,
                }
            )
        else:
            result["reason"] = "safe_short_file_full_content_omitted"
        return result

    def _suggested_next_inspection_calls(self, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        calls = []
        for item in files:
            if not item["diff_available"]:
                continue
            calls.append(
                {
                    "tool": "get_changed_file_diff",
                    "project_id": "<same project_id>",
                    "path": item["path"],
                    "source": item["suggested_diff_source"],
                    "reason_codes": item["reason_codes"],
                    "available_in": "future_A2",
                }
            )
        return calls

    def _truncate_review_package(self, package: dict[str, Any], max_chars: int) -> dict[str, Any]:
        def json_size() -> int:
            return len(json.dumps(package, ensure_ascii=False, sort_keys=True))

        if json_size() <= max_chars:
            return package

        truncation = package["truncation"]
        truncation["truncated"] = True

        for item in package["untracked_previews"]["items"]:
            if "excerpt" in item and json_size() > max_chars:
                item.pop("excerpt")
                item["excerpt_omitted_reason"] = "review_package_truncation"
                item["excerpt_included"] = False
                truncation["omitted_preview_count"] += 1
        if truncation["omitted_preview_count"]:
            truncation["omitted_sections"].append("untracked_preview_excerpts")

        for section in ["suggested_next_inspection_calls", "staged_files", "unstaged_files"]:
            values = package.get(section)
            if isinstance(values, list) and values and json_size() > max_chars:
                package[section] = []
                truncation["omitted_sections"].append(section)

        untracked_file_list = package.get("untracked_files", {}).get("files")
        if isinstance(untracked_file_list, list):
            while untracked_file_list and json_size() > max_chars:
                untracked_file_list.pop()
                package["untracked_files"]["truncated"] = True
                package["untracked_files"]["omitted_count"] += 1
            if package["untracked_files"].get("omitted_count"):
                truncation["omitted_sections"].append("untracked_files.files")

        files = package["files"]
        while files and json_size() > max_chars:
            files.pop()
            truncation["omitted_file_count"] += 1
        if truncation["omitted_file_count"]:
            truncation["omitted_sections"].append("files")

        if json_size() > max_chars:
            for key, value in package["evidence"].items():
                if isinstance(value, dict) and value.get("stdout") and json_size() > max_chars:
                    value["stdout"] = ""
                    value["stdout_omitted_reason"] = "review_package_truncation"
                    truncation["omitted_sections"].append(f"evidence.{key}.stdout")

        return package

    def _untracked_files(self, repo: Path) -> dict[str, Any]:
        cmd = ["git", "ls-files", "--others", "--exclude-standard", "-z"]
        proc = subprocess.run(
            cmd,
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            shell=False,
        )

        files: list[str] = []
        omitted_count = 0
        if proc.returncode == 0:
            for raw_path in proc.stdout.split(b"\0"):
                if not raw_path:
                    continue
                try:
                    path = raw_path.decode("utf-8")
                except UnicodeDecodeError:
                    omitted_count += 1
                    continue
                if not self._is_repo_relative_git_path(repo, path):
                    omitted_count += 1
                    continue
                if len(files) < UNTRACKED_LIST_MAX_FILES:
                    files.append(path)
                else:
                    omitted_count += 1

        return {
            "cmd": cmd,
            "returncode": proc.returncode,
            "stderr": proc.stderr.decode("utf-8", errors="replace"),
            "files": files,
            "truncated": omitted_count > 0,
            "omitted_count": omitted_count,
            "max_files": UNTRACKED_LIST_MAX_FILES,
        }

    def _untracked_previews(
        self,
        repo: Path,
        files: list[str],
        max_chars: int,
        enabled: bool,
    ) -> dict[str, Any]:
        max_total_chars = max(0, min(max_chars, UNTRACKED_PREVIEW_MAX_TOTAL_CHARS))
        limits = {
            "max_files": UNTRACKED_PREVIEW_MAX_FILES,
            "max_bytes_per_file": UNTRACKED_PREVIEW_MAX_BYTES_PER_FILE,
            "max_total_chars": max_total_chars,
        }
        if not enabled:
            return {
                "enabled": False,
                "limits": limits,
                "items": [],
                "truncated": False,
                "omitted_count": 0,
            }

        items: list[dict[str, Any]] = []
        used_chars = 0
        omitted_count = max(0, len(files) - UNTRACKED_PREVIEW_MAX_FILES)
        truncated = omitted_count > 0

        for path in files[:UNTRACKED_PREVIEW_MAX_FILES]:
            candidate_result = self._untracked_preview_candidate(repo, path)
            if candidate_result["status"] != "ok":
                items.append(
                    {
                        "path": path,
                        "status": "skipped",
                        "reason": candidate_result["reason"],
                    }
                )
                continue

            candidate = candidate_result["path"]
            preview = self._read_untracked_preview(candidate)
            if preview["status"] != "included":
                items.append({"path": path, "status": "skipped", "reason": preview["reason"]})
                continue

            text = preview["text"]
            file_truncated = preview["truncated"]
            remaining_chars = max_total_chars - used_chars
            if remaining_chars <= 0:
                items.append(
                    {
                        "path": path,
                        "status": "skipped",
                        "reason": "total_preview_budget_exhausted",
                    }
                )
                truncated = True
                continue
            if len(text) > remaining_chars:
                text = text[:remaining_chars]
                file_truncated = True
                truncated = True

            used_chars += len(text)
            items.append(
                {
                    "path": path,
                    "status": "included",
                    "text": text,
                    "truncated": file_truncated,
                    "bytes_read": preview["bytes_read"],
                    "chars": len(text),
                }
            )

        return {
            "enabled": True,
            "limits": limits,
            "items": items,
            "truncated": truncated,
            "omitted_count": omitted_count,
        }

    def _is_repo_relative_git_path(self, repo: Path, path: str) -> bool:
        if not path.strip():
            return False
        raw_path = Path(path)
        if raw_path.is_absolute():
            return False
        resolved = (repo / raw_path).resolve(strict=False)
        try:
            resolved.relative_to(repo)
        except ValueError:
            return False
        return True

    def _untracked_preview_candidate(self, repo: Path, path: str) -> dict[str, Any]:
        if not path.strip():
            return {"status": "skipped", "reason": "empty_path"}
        raw_path = Path(path)
        if raw_path.is_absolute():
            return {"status": "skipped", "reason": "absolute_path"}

        candidate = repo / raw_path
        if candidate.is_symlink():
            return {"status": "skipped", "reason": "symlink"}

        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(repo)
        except ValueError:
            return {"status": "skipped", "reason": "outside_repo"}

        if not candidate.exists():
            return {"status": "skipped", "reason": "missing"}
        if candidate.is_dir():
            return {"status": "skipped", "reason": "directory"}
        if not candidate.is_file():
            return {"status": "skipped", "reason": "non_regular_file"}
        return {"status": "ok", "path": candidate}

    def _read_untracked_preview(self, path: Path) -> dict[str, Any]:
        try:
            file_size = path.stat().st_size
            with path.open("rb") as handle:
                sample = handle.read(UNTRACKED_PREVIEW_MAX_BYTES_PER_FILE)
        except OSError:
            return {"status": "skipped", "reason": "unreadable"}

        truncated = file_size > len(sample)
        if b"\0" in sample:
            return {"status": "skipped", "reason": "binary"}

        try:
            text = sample.decode("utf-8")
        except UnicodeDecodeError as exc:
            if truncated and exc.start >= len(sample) - 4:
                try:
                    text = sample[: exc.start].decode("utf-8")
                except UnicodeDecodeError:
                    return {"status": "skipped", "reason": "utf8_decode_failed"}
                truncated = True
            else:
                return {"status": "skipped", "reason": "utf8_decode_failed"}

        return {
            "status": "included",
            "text": text,
            "truncated": truncated,
            "bytes_read": len(sample),
        }

    def _status_from_pid(self, rec: TaskRecord) -> str:
        return self._status_from_pid_path(rec.pid_path)

    def _current_task_status(self, meta: dict[str, Any], pid_path: Path) -> str:
        pid_status = self._status_from_pid_path(pid_path)
        if pid_status != "unknown":
            return pid_status
        meta_status = meta.get("status")
        if meta_status in {"dry_run", "failed_to_start"}:
            return meta_status
        return pid_status

    def _refresh_running_task_meta(
        self,
        rec: TaskRecord,
        meta: dict[str, Any],
    ) -> dict[str, Any]:
        if meta.get("status") != "running":
            return meta

        proc = self._task_processes.get(rec.task_id)
        if proc is not None:
            returncode = proc.poll()
            if returncode is not None:
                meta.update(
                    {
                        "status": "exited",
                        "ended_at": time.time(),
                        "returncode": returncode,
                    }
                )
                rec.meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
                return meta

        status = self._current_task_status(meta, rec.pid_path)
        if status != meta["status"]:
            meta["status"] = status
            rec.meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return meta

    def _status_from_pid_path(self, pid_path: Path) -> str:
        if not pid_path.exists():
            return "unknown"
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)
            return "running"
        except ProcessLookupError:
            return "exited"
        except PermissionError:
            return "running"

    def _tail_text(self, path: Path, max_chars: int) -> str:
        if not path.exists():
            return ""
        data = path.read_text(encoding="utf-8", errors="replace")
        return data[-max_chars:]

    def _git_z(self, cwd: Path, cmd: list[str], timeout: int, max_chars: int = 20000) -> dict[str, Any]:
        max_chars = max(0, max_chars)
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                shell=False,
            )
        except FileNotFoundError as exc:
            return {
                "cmd": cmd,
                "returncode": 127,
                "stdout": "",
                "stderr": str(exc),
            }
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        return {
            "cmd": cmd,
            "returncode": proc.returncode,
            "stdout": stdout[-max_chars:] if max_chars else "",
            "stderr": stderr[-max_chars:] if max_chars else "",
        }

    def _git_z_full(
        self,
        cwd: Path,
        cmd: list[str],
        timeout: int,
        evidence_max_chars: int = 20000,
    ) -> dict[str, Any]:
        evidence_max_chars = max(0, evidence_max_chars)
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                shell=False,
            )
        except FileNotFoundError as exc:
            evidence = {
                "cmd": cmd,
                "returncode": 127,
                "stdout": "",
                "stderr": str(exc),
                "stdout_truncated": False,
            }
            return {"stdout": "", "stderr": str(exc), "returncode": 127, "evidence": evidence}

        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        bounded_stdout = stdout[-evidence_max_chars:] if evidence_max_chars else ""
        evidence = {
            "cmd": cmd,
            "returncode": proc.returncode,
            "stdout": bounded_stdout,
            "stderr": stderr[-evidence_max_chars:] if evidence_max_chars else "",
            "stdout_truncated": len(stdout) > len(bounded_stdout),
        }
        return {
            "stdout": stdout,
            "stderr": stderr,
            "returncode": proc.returncode,
            "evidence": evidence,
        }

    def _run(
        self,
        cwd: Path,
        cmd: list[str],
        timeout: int,
        max_chars: int = 20000,
    ) -> dict[str, Any]:
        max_chars = max(0, max_chars)
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                shell=False,
            )
        except FileNotFoundError as exc:
            return {
                "cmd": cmd,
                "returncode": 127,
                "stdout": "",
                "stderr": str(exc),
            }
        stdout = proc.stdout[-max_chars:] if max_chars else ""
        stderr = proc.stderr[-max_chars:] if max_chars else ""
        return {
            "cmd": cmd,
            "returncode": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }

    def _resolve_verification_command(self, project: ProjectConfig, command_key: str) -> list[str]:
        if command_key not in project.verification:
            raise ValueError(
                f"Verification command '{command_key}' is not allowlisted. "
                f"Available: {sorted(project.verification)}"
            )
        return project.verification[command_key]

    def _run_verification_bundle_command(
        self,
        cwd: Path,
        command_key: str,
        cmd: list[str],
        timeout: int,
        max_chars: int,
    ) -> dict[str, Any]:
        started_at = time.monotonic()
        reason = None
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                shell=False,
            )
            returncode: int | None = proc.returncode
            stdout_value: str | bytes | None = proc.stdout
            stderr_value: str | bytes | None = proc.stderr
            if returncode != 0:
                reason = "nonzero exit status"
        except FileNotFoundError as exc:
            returncode = 127
            stdout_value = ""
            stderr_value = str(exc)
            reason = "executable not found"
        except subprocess.TimeoutExpired as exc:
            returncode = None
            stdout_value = exc.stdout
            stderr_value = exc.stderr
            reason = f"timed out after {timeout} seconds"

        stdout, stdout_truncated, stdout_omitted_chars = self._bounded_verification_output(
            stdout_value,
            max_chars,
        )
        stderr, stderr_truncated, stderr_omitted_chars = self._bounded_verification_output(
            stderr_value,
            max_chars,
        )
        return {
            "command_key": command_key,
            "cmd": cmd,
            "returncode": returncode,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "stdout_omitted_chars": stdout_omitted_chars,
            "stderr_omitted_chars": stderr_omitted_chars,
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
            "status": "passed" if returncode == 0 else "failed",
            "reason": reason,
        }

    def _bounded_verification_output(
        self,
        value: str | bytes | None,
        max_chars: int,
    ) -> tuple[str, bool, int]:
        if value is None:
            text = ""
        elif isinstance(value, bytes):
            text = value.decode(errors="replace")
        else:
            text = value

        max_chars = max(0, max_chars)
        bounded = (
            text[-max_chars:]
            if max_chars and len(text) > max_chars
            else (text if max_chars else "")
        )
        truncated = len(text) > len(bounded)
        omitted_chars = max(0, len(text) - len(bounded))
        return bounded, truncated, omitted_chars

    def _run_prefix(
        self,
        cwd: Path,
        cmd: list[str],
        timeout: int,
        max_chars: int = 20000,
    ) -> dict[str, Any]:
        max_chars = max(0, max_chars)
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                shell=False,
            )
        except FileNotFoundError as exc:
            return {
                "cmd": cmd,
                "returncode": 127,
                "stdout": "",
                "stderr": str(exc),
                "stdout_truncated": False,
                "stdout_omitted_chars": 0,
            }
        bounded_stdout = proc.stdout[:max_chars] if max_chars else ""
        return {
            "cmd": cmd,
            "returncode": proc.returncode,
            "stdout": bounded_stdout,
            "stderr": proc.stderr[:max_chars] if max_chars else "",
            "stdout_truncated": len(proc.stdout) > len(bounded_stdout),
            "stdout_omitted_chars": max(0, len(proc.stdout) - len(bounded_stdout)),
        }
