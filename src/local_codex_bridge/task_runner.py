from __future__ import annotations

import json
import os
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
BRANCH_NAME_MAX_CHARS = 200
BRANCH_NAME_ALLOWED_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


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
    ) -> dict[str, Any]:
        project = self._project(project_id)
        task_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        rec = self._task_record(task_id, project_id)
        rec.task_path.mkdir(parents=True, exist_ok=False)
        rec.prompt_path.write_text(prompt, encoding="utf-8")

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

    def _run(
        self,
        cwd: Path,
        cmd: list[str],
        timeout: int,
        max_chars: int = 20000,
    ) -> dict[str, Any]:
        max_chars = max(0, max_chars)
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            shell=False,
        )
        stdout = proc.stdout[-max_chars:] if max_chars else ""
        stderr = proc.stderr[-max_chars:] if max_chars else ""
        return {
            "cmd": cmd,
            "returncode": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }
