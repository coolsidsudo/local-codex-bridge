from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from local_codex_bridge.config import BridgeConfig, ProjectConfig, ServerConfig
from local_codex_bridge.task_runner import TaskRunner


def run(cwd: Path, cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )


def head(repo: Path, ref: str = "HEAD") -> str:
    return run(repo, ["git", "rev-parse", ref]).stdout.strip()


def make_sync_runner(tmp_path: Path) -> tuple[TaskRunner, Path, str]:
    repo = tmp_path / "project"
    remote = tmp_path / "origin.git"
    run(tmp_path, ["git", "init", "--bare", str(remote)])
    repo.mkdir(parents=True)
    run(repo, ["git", "init", "-b", "main"])
    run(repo, ["git", "config", "user.email", "test@example.invalid"])
    run(repo, ["git", "config", "user.name", "Test User"])
    (repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
    run(repo, ["git", "add", "tracked.txt"])
    run(repo, ["git", "commit", "-m", "initial"])
    run(repo, ["git", "remote", "add", "origin", str(remote)])
    run(repo, ["git", "push", "-u", "origin", "main"])
    initial = head(repo)
    run(repo, ["git", "update-ref", "refs/remotes/origin/main", initial])

    cfg = BridgeConfig(
        server=ServerConfig(task_dir=tmp_path / "tasks"),
        projects={"dummy": ProjectConfig(name="Dummy", path=repo)},
    )
    return TaskRunner(cfg), repo, initial


def make_commit_object(repo: Path, parent: str, message: str) -> str:
    tree = run(repo, ["git", "rev-parse", f"{parent}^{{tree}}"]).stdout.strip()
    return run(repo, ["git", "commit-tree", tree, "-p", parent, "-m", message]).stdout.strip()


def make_origin_main_ahead(repo: Path, initial: str) -> str:
    remote_only = make_commit_object(repo, initial, "remote only")
    run(repo, ["git", "update-ref", "refs/remotes/origin/main", remote_only])
    return remote_only


def assert_no_forbidden_sync_commands(commands: list[list[str]]) -> None:
    forbidden_git_verbs = {"fetch", "pull", "push", "merge", "tag"}
    assert not [cmd for cmd in commands if cmd[:1] == ["gh"]]
    assert not [
        cmd
        for cmd in commands
        if cmd[:1] == ["git"] and len(cmd) > 1 and cmd[1] in forbidden_git_verbs
    ]
    assert not [
        cmd
        for cmd in commands
        if cmd[:2] == ["git", "branch"] and any(arg in {"-d", "-D", "--delete"} for arg in cmd[2:])
    ]


def test_sync_local_branch_equal_main_noops_without_reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, repo, initial = make_sync_runner(tmp_path)
    commands: list[list[str]] = []
    original_run = runner._run

    def spy_run(cwd: Path, cmd: list[str], timeout: int, max_chars: int = 20000) -> dict[str, Any]:
        commands.append(cmd)
        return original_run(cwd, cmd, timeout, max_chars)

    monkeypatch.setattr(runner, "_run", spy_run)

    result = runner.git_sync_local_branch_to_origin("dummy", target_branch="main")

    assert result["status"] == "ok_noop"
    assert result["action"] == "no_op"
    assert result["executed_commands"] == []
    assert ["git", "switch", "main"] not in commands
    assert ["git", "reset", "--hard", "origin/main"] not in commands
    assert_no_forbidden_sync_commands(commands)
    assert head(repo) == initial
    assert run(repo, ["git", "status", "--porcelain=v1", "--untracked-files=normal"]).stdout == ""


def test_sync_local_branch_equal_target_noops_without_switching_from_feature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, initial = make_sync_runner(tmp_path)
    run(repo, ["git", "switch", "-c", "feature"])
    commands: list[list[str]] = []
    original_run = runner._run

    def spy_run(cwd: Path, cmd: list[str], timeout: int, max_chars: int = 20000) -> dict[str, Any]:
        commands.append(cmd)
        return original_run(cwd, cmd, timeout, max_chars)

    monkeypatch.setattr(runner, "_run", spy_run)

    result = runner.git_sync_local_branch_to_origin("dummy", target_branch="main")

    assert result["status"] == "ok_noop"
    assert result["action"] == "no_op"
    assert result["executed_commands"] == []
    assert result["before_evidence"]["current_branch"] == "feature"
    assert head(repo, "main") == initial
    assert head(repo, "origin/main") == initial
    assert run(repo, ["git", "branch", "--show-current"]).stdout.strip() == "feature"
    assert ["git", "switch", "main"] not in commands
    assert ["git", "reset", "--hard", "origin/main"] not in commands
    assert_no_forbidden_sync_commands(commands)


def test_sync_local_branch_behind_main_switches_and_resets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, repo, initial = make_sync_runner(tmp_path)
    remote_only = make_origin_main_ahead(repo, initial)
    commands: list[list[str]] = []
    original_run = runner._run

    def spy_run(cwd: Path, cmd: list[str], timeout: int, max_chars: int = 20000) -> dict[str, Any]:
        commands.append(cmd)
        return original_run(cwd, cmd, timeout, max_chars)

    monkeypatch.setattr(runner, "_run", spy_run)

    result = runner.git_sync_local_branch_to_origin("dummy", target_branch="main")

    assert result["status"] == "ok_synced"
    assert result["action"] == "synced"
    assert result["executed_commands"] == [
        ["git", "switch", "main"],
        ["git", "reset", "--hard", "origin/main"],
    ]
    assert head(repo) == remote_only
    assert run(repo, ["git", "status", "--porcelain=v1", "--untracked-files=normal"]).stdout == ""
    assert_no_forbidden_sync_commands(commands)


def test_sync_local_branch_from_clean_feature_switches_to_target_when_behind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, initial = make_sync_runner(tmp_path)
    remote_only = make_origin_main_ahead(repo, initial)
    run(repo, ["git", "switch", "-c", "feature"])
    (repo / "tracked.txt").write_text("feature\n", encoding="utf-8")
    run(repo, ["git", "commit", "-am", "feature"])
    commands: list[list[str]] = []
    original_run = runner._run

    def spy_run(cwd: Path, cmd: list[str], timeout: int, max_chars: int = 20000) -> dict[str, Any]:
        commands.append(cmd)
        return original_run(cwd, cmd, timeout, max_chars)

    monkeypatch.setattr(runner, "_run", spy_run)

    result = runner.git_sync_local_branch_to_origin("dummy", target_branch="main")

    assert result["status"] == "ok_synced"
    assert result["before_evidence"]["current_branch"] == "feature"
    assert result["after_evidence"]["current_branch"] == "main"
    assert head(repo) == remote_only
    assert_no_forbidden_sync_commands(commands)


def test_sync_local_branch_target_ahead_blocks(tmp_path: Path) -> None:
    runner, repo, _ = make_sync_runner(tmp_path)
    (repo / "tracked.txt").write_text("local\n", encoding="utf-8")
    run(repo, ["git", "commit", "-am", "local only"])

    result = runner.git_sync_local_branch_to_origin("dummy", target_branch="main")

    assert result["status"] == "blocked_sync"
    assert result["action"] == "blocked"
    assert result["before_evidence"]["relation"] == "ahead"
    assert result["executed_commands"] == []


def test_sync_local_branch_target_diverged_blocks(tmp_path: Path) -> None:
    runner, repo, initial = make_sync_runner(tmp_path)
    make_origin_main_ahead(repo, initial)
    (repo / "tracked.txt").write_text("local\n", encoding="utf-8")
    run(repo, ["git", "commit", "-am", "local only"])

    result = runner.git_sync_local_branch_to_origin("dummy", target_branch="main")

    assert result["status"] == "blocked_sync"
    assert result["before_evidence"]["relation"] == "diverged"
    assert result["executed_commands"] == []


def test_sync_local_branch_dirty_tracked_changes_block(tmp_path: Path) -> None:
    runner, repo, _ = make_sync_runner(tmp_path)
    (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    result = runner.git_sync_local_branch_to_origin("dummy", target_branch="main")

    assert result["status"] == "blocked_sync"
    assert "Local worktree is dirty" in result["blocking_reasons"]


def test_sync_local_branch_untracked_file_blocks(tmp_path: Path) -> None:
    runner, repo, _ = make_sync_runner(tmp_path)
    (repo / "new.txt").write_text("untracked\n", encoding="utf-8")

    result = runner.git_sync_local_branch_to_origin("dummy", target_branch="main")

    assert result["status"] == "blocked_sync"
    assert "Local worktree is dirty" in result["blocking_reasons"]


def test_sync_local_branch_detached_head_blocks(tmp_path: Path) -> None:
    runner, repo, _ = make_sync_runner(tmp_path)
    run(repo, ["git", "checkout", "--detach", "HEAD"])

    result = runner.git_sync_local_branch_to_origin("dummy", target_branch="main")

    assert result["status"] == "blocked_sync"
    assert "Local repository is detached or current branch is unknown" in result["blocking_reasons"]


def test_sync_local_branch_missing_origin_remote_blocks(tmp_path: Path) -> None:
    runner, repo, _ = make_sync_runner(tmp_path)
    run(repo, ["git", "remote", "remove", "origin"])

    result = runner.git_sync_local_branch_to_origin("dummy", target_branch="main")

    assert result["status"] == "blocked_sync"
    assert any("origin remote is not configured" in reason for reason in result["blocking_reasons"])


def test_sync_local_branch_missing_local_target_branch_blocks(tmp_path: Path) -> None:
    runner, repo, _ = make_sync_runner(tmp_path)
    run(repo, ["git", "switch", "-c", "feature"])
    run(repo, ["git", "branch", "-D", "main"])

    result = runner.git_sync_local_branch_to_origin("dummy", target_branch="main")

    assert result["status"] == "blocked_sync"
    assert "Local target branch does not exist" in result["blocking_reasons"]


def test_sync_local_branch_missing_origin_target_ref_blocks(tmp_path: Path) -> None:
    runner, repo, _ = make_sync_runner(tmp_path)
    run(repo, ["git", "update-ref", "-d", "refs/remotes/origin/main"])

    result = runner.git_sync_local_branch_to_origin("dummy", target_branch="main")

    assert result["status"] == "blocked_sync"
    assert "Local origin target ref does not exist" in result["blocking_reasons"]


@pytest.mark.parametrize(
    ("kwargs", "error_text"),
    [
        ({"remote": "upstream"}, "origin"),
        ({"target_branch": "origin/main"}, "remote-style"),
        ({"target_branch": "HEAD"}, "HEAD"),
        ({"target_branch": "refs/heads/main"}, "refs/"),
        ({"target_branch": ""}, "empty"),
        ({"target_branch": "bad;name"}, "unsafe"),
    ],
)
def test_sync_local_branch_invalid_inputs_blocked(
    tmp_path: Path,
    kwargs: dict[str, str],
    error_text: str,
) -> None:
    runner, _, _ = make_sync_runner(tmp_path)

    result = runner.git_sync_local_branch_to_origin("dummy", **kwargs)

    assert result["status"] == "blocked_input"
    assert error_text in result["error"]
    assert result["executed_commands"] == []


def test_sync_local_branch_switch_failure_blocks_with_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, initial = make_sync_runner(tmp_path)
    make_origin_main_ahead(repo, initial)
    original_run = runner._run
    commands: list[list[str]] = []

    def fail_switch(cwd: Path, cmd: list[str], timeout: int, max_chars: int = 20000) -> dict[str, Any]:
        commands.append(cmd)
        if cmd == ["git", "switch", "main"]:
            return {"cmd": cmd, "returncode": 1, "stdout": "", "stderr": "switch failed"}
        return original_run(cwd, cmd, timeout, max_chars)

    monkeypatch.setattr(runner, "_run", fail_switch)

    result = runner.git_sync_local_branch_to_origin("dummy", target_branch="main")

    assert result["status"] == "blocked_sync"
    assert result["error"] == "git switch failed"
    assert result["executed_commands"] == [["git", "switch", "main"]]
    assert "after_evidence" in result
    assert_no_forbidden_sync_commands(commands)


def test_sync_local_branch_reset_failure_blocks_with_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, initial = make_sync_runner(tmp_path)
    make_origin_main_ahead(repo, initial)
    original_run = runner._run
    commands: list[list[str]] = []

    def fail_reset(cwd: Path, cmd: list[str], timeout: int, max_chars: int = 20000) -> dict[str, Any]:
        commands.append(cmd)
        if cmd == ["git", "reset", "--hard", "origin/main"]:
            return {"cmd": cmd, "returncode": 1, "stdout": "", "stderr": "reset failed"}
        return original_run(cwd, cmd, timeout, max_chars)

    monkeypatch.setattr(runner, "_run", fail_reset)

    result = runner.git_sync_local_branch_to_origin("dummy", target_branch="main")

    assert result["status"] == "blocked_sync"
    assert result["error"] == "git reset failed"
    assert result["executed_commands"] == [
        ["git", "switch", "main"],
        ["git", "reset", "--hard", "origin/main"],
    ]
    assert "intermediate_evidence" in result
    assert "after_evidence" in result
    assert_no_forbidden_sync_commands(commands)
