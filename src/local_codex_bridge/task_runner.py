from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import BridgeConfig, ProjectConfig


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
        stat = self._run(project.path, ["git", "diff", "--stat"], timeout=20)
        diff = self._run(project.path, ["git", "diff"], timeout=20, max_chars=max_chars)
        return {"status": status, "stat": stat, "diff": diff}

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
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            shell=False,
        )
        stdout = proc.stdout[-max_chars:]
        stderr = proc.stderr[-max_chars:]
        return {
            "cmd": cmd,
            "returncode": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }
