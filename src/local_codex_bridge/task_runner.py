from __future__ import annotations

import json
import os
import posixpath
import re
import signal
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
- Do not commit, push, create a PR, or touch tags/releases.
- Do not paste full diffs or full file contents in your final response.
- Return a concise implementation summary.
- List changed files.
- List exact verification commands run and their results.
- List risks, deviations, or follow-up needs.
- Confirm no commit, push, PR, tag, or release work was performed.
- ChatGPT will inspect actual repository state through Local Codex Bridge review tools, not from pasted diffs: `get_review_package`, `get_changed_file_diff`, `get_changed_file_text`, and `run_verification`.
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
GH_JSON_MAX_CHARS = 60000
CHANGED_FILE_TEXT_DEFAULT_MAX_CHARS = 60000
CHANGED_FILE_TEXT_MIN_READ_BYTES = 4096
CHANGED_FILE_TEXT_UTF8_BOUNDARY_BYTES = 4
CHANGED_FILE_TEXT_MAX_READ_BYTES = 1024 * 1024


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
            "git": self._run(project.path, ["git", "status", "--short", "--branch"], timeout=20),
            "head": self._run(project.path, ["git", "rev-parse", "HEAD"], timeout=20),
            "remotes": self._run(project.path, ["git", "remote", "-v"], timeout=20),
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
        cmd = [self.config.server.codex_bin, "exec"]
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

        proc = subprocess.Popen(
            cmd,
            cwd=str(project.path),
            stdin=prompt_stdin,
            stdout=stdout,
            stderr=stderr,
            shell=False,
            start_new_session=True,
        )
        rec.pid_path.write_text(str(proc.pid), encoding="utf-8")
        return {"task_id": task_id, "status": "running", "pid": proc.pid, "cmd": cmd}

    def get_task(self, task_id: str, max_chars: int = 20000) -> dict[str, Any]:
        rec = self._task_record(task_id)
        meta = json.loads(rec.meta_path.read_text(encoding="utf-8"))
        status = self._status_from_pid(rec)
        meta["status"] = status
        rec.meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

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

    def run_verification(self, project_id: str, command_key: str, timeout: int = 600) -> dict[str, Any]:
        project = self._project(project_id)
        if command_key not in project.verification:
            raise ValueError(
                f"Verification command '{command_key}' is not allowlisted. "
                f"Available: {sorted(project.verification)}"
            )
        cmd = project.verification[command_key]
        return self._run(project.path, cmd, timeout=timeout, max_chars=40000)

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

        origin = self._github_origin_info(repo)
        evidence: dict[str, Any] = {
            "project_id": project_id,
            "path": str(repo),
            "origin": origin,
            "requested_pr": pr_url_or_number,
        }
        if origin["status"] != "ok":
            return {"status": "blocked_remote", "error": "origin is not a supported GitHub remote", **evidence}

        gh_ready = self._github_cli_ready(repo)
        evidence["gh"] = gh_ready
        if gh_ready["status"] != "ok":
            return {**gh_ready, **evidence}

        if pr_url_or_number is None:
            branch = self._current_branch(repo)
            evidence["current_branch_result"] = branch
            current_branch = branch["stdout"].strip()
            evidence["current_branch"] = current_branch or None
            if branch["returncode"] != 0 or not current_branch:
                return {
                    "status": "blocked_detached_head",
                    "error": "Current branch could not be determined for branch PR lookup",
                    **evidence,
                }
            prs = self._github_prs_for_branch(repo, origin["repo_arg"], current_branch)
            evidence["prs_for_branch"] = prs
            if prs["status"] != "ok":
                return {**prs, **evidence}
            if not prs["prs"]:
                return {"status": "no_pr", "error": "No open PR found for current branch", **evidence}
            if len(prs["prs"]) > 1:
                return {
                    "status": "blocked_ambiguous_pr",
                    "error": "Multiple open PRs matched the current branch",
                    **evidence,
                }
            return {"status": "ok", "pr": prs["prs"][0], **evidence}

        parsed = self._parse_pr_reference(pr_url_or_number, origin["owner"], origin["repo"])
        evidence["parsed_pr_reference"] = parsed
        if parsed["status"] != "ok":
            return {"status": "blocked_input", "error": "Invalid PR number or URL", **evidence}

        canonical = self._github_pr_view(repo, origin["repo_arg"], parsed["reference"])
        evidence["canonical_pr"] = canonical
        if canonical["status"] != "ok":
            return {**canonical, **evidence}
        return {"status": "ok", "pr": canonical["pr"], **evidence}

    def list_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        task_dirs = sorted(self.config.server.task_dir.glob("*"), key=lambda p: p.name, reverse=True)
        items = []
        for task_dir in task_dirs[:limit]:
            meta_path = task_dir / "meta.json"
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    meta["status"] = self._status_from_pid_path(task_dir / "pid")
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
        allowed = {"auto", "worktree", "staged", "untracked"}
        if source not in allowed:
            return {
                "status": "blocked_input",
                "error": "source must be one of auto, worktree, staged, untracked",
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
            if source not in available_sources:
                return {
                    "status": "blocked_unchanged",
                    "error": f"path has no {source} content source",
                    "available_sources": available_sources,
                }
            return {"status": "ok", "source": source, "available_sources": available_sources}

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
