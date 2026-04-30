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
        branch: str = "main",
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
        if branch != "main":
            return {"status": "blocked_input", "error": "Only branch='main' is supported"}

        resolved_files: list[str] = []
        for raw in files:
            candidate = (repo / raw).resolve()
            try:
                candidate.relative_to(repo)
            except ValueError:
                return {"status": "blocked_input", "error": f"File path escapes project root: {raw}"}
            if not candidate.exists():
                return {"status": "blocked_input", "error": f"File does not exist: {raw}"}
            resolved_files.append(raw)

        before_status = self._run(repo, ["git", "status", "--short", "--branch"], timeout=20)
        head_before = self._run(repo, ["git", "rev-parse", "HEAD"], timeout=20)
        remotes = self._run(repo, ["git", "remote", "-v"], timeout=20)

        add = self._run(repo, ["git", "add", *resolved_files], timeout=timeout, max_chars=40000)
        if add["returncode"] != 0:
            return {
                "status": "blocked_add",
                "before_status": before_status,
                "head_before": head_before,
                "remotes": remotes,
                "add": add,
            }

        commit = self._run(repo, ["git", "commit", "-m", message], timeout=timeout, max_chars=40000)
        if commit["returncode"] != 0:
            return {
                "status": "blocked_commit",
                "before_status": before_status,
                "head_before": head_before,
                "remotes": remotes,
                "add": add,
                "commit": commit,
                "status_after_commit_failure": self._run(repo, ["git", "status", "--short", "--branch"], timeout=20),
            }

        commit_hash = self._run(repo, ["git", "rev-parse", "HEAD"], timeout=20)
        push = self._run(repo, ["git", "push", remote, branch], timeout=timeout, max_chars=40000)
        final_status = self._run(repo, ["git", "status", "--short", "--branch"], timeout=20)
        log = self._run(repo, ["git", "log", "-1", "--oneline", "--decorate"], timeout=20)

        if push["returncode"] != 0:
            return {
                "status": "blocked_push",
                "before_status": before_status,
                "head_before": head_before,
                "remotes": remotes,
                "add": add,
                "commit": commit,
                "commit_hash": commit_hash,
                "push": push,
                "final_status": final_status,
                "log": log,
            }

        return {
            "status": "pushed",
            "before_status": before_status,
            "head_before": head_before,
            "remotes": remotes,
            "add": add,
            "commit": commit,
            "commit_hash": commit_hash,
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
