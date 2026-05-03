from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

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


def make_runner(tmp_path: Path, branch: str = "feature") -> tuple[TaskRunner, Path, Path]:
    repo = tmp_path / "project"
    remote = tmp_path / "origin.git"
    repo.mkdir()
    run(tmp_path, ["git", "init", "--bare", str(remote)])
    run(repo, ["git", "init", "-b", branch])
    run(repo, ["git", "config", "user.email", "test@example.invalid"])
    run(repo, ["git", "config", "user.name", "Test User"])
    (repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
    run(repo, ["git", "add", "tracked.txt"])
    run(repo, ["git", "commit", "-m", "initial"])
    run(repo, ["git", "remote", "add", "origin", str(remote)])
    run(repo, ["git", "push", "-u", "origin", branch])

    cfg = BridgeConfig(
        server=ServerConfig(task_dir=tmp_path / "tasks"),
        projects={"dummy": ProjectConfig(name="Dummy", path=repo)},
    )
    return TaskRunner(cfg), repo, remote


def head(repo: Path) -> str:
    return run(repo, ["git", "rev-parse", "HEAD"]).stdout.strip()


def staged(repo: Path) -> list[str]:
    out = run(repo, ["git", "diff", "--cached", "--name-only"]).stdout
    return [line for line in out.splitlines() if line]


def readiness(runner: TaskRunner, files: list[str], **kwargs: Any) -> dict[str, Any]:
    return runner.get_acceptance_readiness("dummy", files, **kwargs)


def test_empty_files_blocked(tmp_path: Path) -> None:
    runner, _, _ = make_runner(tmp_path)
    result = runner.git_commit_and_push("dummy", [], "commit")
    assert result["status"] == "blocked_input"
    assert "files" in result["error"]


def test_blank_message_blocked(tmp_path: Path) -> None:
    runner, _, _ = make_runner(tmp_path)
    result = runner.git_commit_and_push("dummy", ["tracked.txt"], "  \n")
    assert result["status"] == "blocked_input"
    assert "message" in result["error"]


def test_path_escaping_project_root_blocked(tmp_path: Path) -> None:
    runner, repo, _ = make_runner(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    before = head(repo)

    result = runner.git_commit_and_push("dummy", ["../outside.txt"], "escape")

    assert result["status"] == "blocked_input"
    assert "escapes" in result["error"]
    assert head(repo) == before


def test_absolute_path_under_project_root_is_normalized(tmp_path: Path) -> None:
    runner, repo, _ = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")

    result = runner.git_commit_and_push("dummy", [str(repo / "tracked.txt")], "absolute")

    assert result["status"] == "pushed"
    assert result["approved_files"] == ["tracked.txt"]
    assert result["push_branch"] == "feature"


def test_non_origin_remote_blocked(tmp_path: Path) -> None:
    runner, repo, _ = make_runner(tmp_path)
    before = head(repo)

    result = runner.git_commit_and_push("dummy", ["tracked.txt"], "commit", remote="upstream")

    assert result["status"] == "blocked_input"
    assert "origin" in result["error"]
    assert head(repo) == before


def test_branch_mismatch_blocked(tmp_path: Path) -> None:
    runner, repo, _ = make_runner(tmp_path, branch="topic")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    before = head(repo)

    result = runner.git_commit_and_push("dummy", ["tracked.txt"], "commit", branch="other")

    assert result["status"] == "blocked_branch"
    assert result["current_branch"] == "topic"
    assert result["push_branch"] == "other"
    assert head(repo) == before
    assert staged(repo) == []


def test_modified_file_can_be_committed_on_current_branch_with_branch_omitted(
    tmp_path: Path,
) -> None:
    runner, repo, _ = make_runner(tmp_path, branch="work")
    before = head(repo)
    (repo / "tracked.txt").write_text("modified\n", encoding="utf-8")

    result = runner.git_commit_and_push("dummy", ["tracked.txt"], "modify tracked")

    assert result["status"] == "pushed"
    assert result["current_branch"] == "work"
    assert result["push_remote"] == "origin"
    assert result["push_branch"] == "work"
    assert head(repo) != before
    assert staged(repo) == []


def test_newly_added_file_can_be_committed(tmp_path: Path) -> None:
    runner, repo, _ = make_runner(tmp_path)
    before = head(repo)
    (repo / "new.txt").write_text("new\n", encoding="utf-8")

    result = runner.git_commit_and_push("dummy", ["new.txt"], "add new")

    assert result["status"] == "pushed"
    assert head(repo) != before
    assert staged(repo) == []


def test_deleted_file_can_be_committed(tmp_path: Path) -> None:
    runner, repo, _ = make_runner(tmp_path)
    before = head(repo)
    (repo / "tracked.txt").unlink()

    result = runner.git_commit_and_push("dummy", ["tracked.txt"], "delete tracked")

    assert result["status"] == "pushed"
    assert head(repo) != before
    assert not (repo / "tracked.txt").exists()
    assert staged(repo) == []


def test_unapproved_pre_staged_extra_file_causes_refusal_before_commit(
    tmp_path: Path,
) -> None:
    runner, repo, _ = make_runner(tmp_path)
    before = head(repo)
    (repo / "tracked.txt").write_text("approved\n", encoding="utf-8")
    (repo / "extra.txt").write_text("extra\n", encoding="utf-8")
    run(repo, ["git", "add", "extra.txt"])

    result = runner.git_commit_and_push("dummy", ["tracked.txt"], "commit approved")

    assert result["status"] == "blocked_staged_files"
    assert result["unapproved_staged_files"] == ["extra.txt"]
    assert head(repo) == before
    assert staged(repo) == ["extra.txt"]


def test_git_pathspec_magic_is_treated_literally(tmp_path: Path) -> None:
    runner, repo, _ = make_runner(tmp_path)
    before = head(repo)
    (repo / "tracked.txt").write_text("changed but not approved\n", encoding="utf-8")

    result = runner.git_commit_and_push("dummy", [":(glob)*"], "literal pathspec")

    assert result["status"] in {"blocked_add", "blocked_staged_files"}
    assert head(repo) == before
    assert staged(repo) == []


def test_acceptance_readiness_clean_empty_not_ready(tmp_path: Path) -> None:
    runner, _, _ = make_runner(tmp_path)

    result = readiness(runner, [])

    assert result["status"] == "ok"
    assert result["ready"] is False
    assert result["changed_files"]["all"] == []
    assert "approved_files must not be empty" in result["blocking_reasons"]


def test_acceptance_readiness_approved_tracked_modification_ready(tmp_path: Path) -> None:
    runner, repo, _ = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("modified\n", encoding="utf-8")

    result = readiness(runner, ["tracked.txt"])

    assert result["status"] == "ok"
    assert result["ready"] is True
    assert result["approved_files_normalized"] == ["tracked.txt"]
    assert result["changed_files"]["unstaged"] == ["tracked.txt"]
    assert staged(repo) == []


def test_acceptance_readiness_approved_untracked_file_ready(tmp_path: Path) -> None:
    runner, repo, _ = make_runner(tmp_path)
    (repo / "new.txt").write_text("new\n", encoding="utf-8")

    result = readiness(runner, ["new.txt"])

    assert result["status"] == "ok"
    assert result["ready"] is True
    assert result["changed_files"]["untracked"] == ["new.txt"]
    assert staged(repo) == []


def test_acceptance_readiness_approved_deleted_file_ready(tmp_path: Path) -> None:
    runner, repo, _ = make_runner(tmp_path)
    (repo / "tracked.txt").unlink()

    result = readiness(runner, ["tracked.txt"])

    assert result["status"] == "ok"
    assert result["ready"] is True
    assert result["changed_files"]["unstaged"] == ["tracked.txt"]


def test_acceptance_readiness_changed_not_approved_not_ready(tmp_path: Path) -> None:
    runner, repo, _ = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("modified\n", encoding="utf-8")

    result = readiness(runner, ["other.txt"])

    assert result["status"] == "ok"
    assert result["ready"] is False
    assert result["coverage"]["changed_but_not_approved"] == ["tracked.txt"]
    assert result["coverage"]["approved_but_not_changed"] == ["other.txt"]


def test_acceptance_readiness_approved_not_changed_not_ready(tmp_path: Path) -> None:
    runner, _, _ = make_runner(tmp_path)

    result = readiness(runner, ["tracked.txt"])

    assert result["status"] == "ok"
    assert result["ready"] is False
    assert result["coverage"]["approved_but_not_changed"] == ["tracked.txt"]


def test_acceptance_readiness_unapproved_staged_file_not_ready(tmp_path: Path) -> None:
    runner, repo, _ = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("approved\n", encoding="utf-8")
    (repo / "extra.txt").write_text("extra\n", encoding="utf-8")
    run(repo, ["git", "add", "extra.txt"])

    result = readiness(runner, ["tracked.txt"])

    assert result["status"] == "ok"
    assert result["ready"] is False
    assert result["coverage"]["unapproved_staged"] == ["extra.txt"]


def test_acceptance_readiness_approved_staged_only_ready(tmp_path: Path) -> None:
    runner, repo, _ = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("staged\n", encoding="utf-8")
    run(repo, ["git", "add", "tracked.txt"])

    result = readiness(runner, ["tracked.txt"])

    assert result["status"] == "ok"
    assert result["ready"] is True
    assert result["changed_files"]["staged"] == ["tracked.txt"]
    assert result["coverage"]["staged_within_approved"] is True


def test_acceptance_readiness_mixed_staged_and_unstaged_approved_ready(tmp_path: Path) -> None:
    runner, repo, _ = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("staged\n", encoding="utf-8")
    run(repo, ["git", "add", "tracked.txt"])
    (repo / "new.txt").write_text("new\n", encoding="utf-8")

    result = readiness(runner, ["tracked.txt", "new.txt"])

    assert result["status"] == "ok"
    assert result["ready"] is True
    assert result["changed_files"]["staged"] == ["tracked.txt"]
    assert result["changed_files"]["untracked"] == ["new.txt"]


def test_acceptance_readiness_branch_mismatch_not_ready(tmp_path: Path) -> None:
    runner, repo, _ = make_runner(tmp_path, branch="topic")
    (repo / "tracked.txt").write_text("modified\n", encoding="utf-8")

    result = readiness(runner, ["tracked.txt"], branch="other")

    assert result["status"] == "ok"
    assert result["ready"] is False
    assert result["current_branch"] == "topic"
    assert "Requested branch does not match current checked-out branch" in result["blocking_reasons"]


def test_acceptance_readiness_detached_head_not_ready(tmp_path: Path) -> None:
    runner, repo, _ = make_runner(tmp_path)
    run(repo, ["git", "checkout", "--detach"])
    (repo / "tracked.txt").write_text("modified\n", encoding="utf-8")

    result = readiness(runner, ["tracked.txt"])

    assert result["status"] == "ok"
    assert result["ready"] is False
    assert result["detached"] is True
    assert result["current_branch"] is None


def test_acceptance_readiness_non_origin_remote_blocked(tmp_path: Path) -> None:
    runner, _, _ = make_runner(tmp_path)

    result = readiness(runner, ["tracked.txt"], remote="upstream")

    assert result["status"] == "blocked_input"
    assert result["ready"] is False
    assert "origin" in result["error"]


def test_acceptance_readiness_path_escaping_project_root_blocked(tmp_path: Path) -> None:
    runner, _, _ = make_runner(tmp_path)

    result = readiness(runner, ["../outside.txt"])

    assert result["status"] == "blocked_input"
    assert result["ready"] is False
    assert "escapes" in result["error"]


def test_acceptance_readiness_unknown_project_blocked(tmp_path: Path) -> None:
    runner, _, _ = make_runner(tmp_path)

    result = runner.get_acceptance_readiness("missing", ["tracked.txt"])

    assert result["status"] == "blocked_input"
    assert result["ready"] is False
    assert "Unknown project_id" in result["error"]


def test_acceptance_readiness_git_failure_blocked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner, repo, _ = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("modified\n", encoding="utf-8")
    original_run = runner._run

    def fake_run(cwd: Path, cmd: list[str], timeout: int, max_chars: int = 20000) -> dict[str, Any]:
        if cmd == ["git", "status", "--short", "--branch"]:
            return {"cmd": cmd, "returncode": 1, "stdout": "", "stderr": "forced failure"}
        return original_run(cwd, cmd, timeout, max_chars)

    monkeypatch.setattr(runner, "_run", fake_run)

    result = readiness(runner, ["tracked.txt"])

    assert result["status"] == "blocked_git"
    assert result["ready"] is False
    assert "status_short_branch" in result["error"]


def test_acceptance_readiness_missing_origin_not_ready(tmp_path: Path) -> None:
    runner, repo, _ = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("modified\n", encoding="utf-8")
    run(repo, ["git", "remote", "remove", "origin"])

    result = readiness(runner, ["tracked.txt"])

    assert result["status"] == "ok"
    assert result["ready"] is False
    assert "origin remote is not configured" in result["blocking_reasons"]


def test_acceptance_readiness_ahead_upstream_not_ready(tmp_path: Path) -> None:
    runner, repo, _ = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("local commit\n", encoding="utf-8")
    run(repo, ["git", "add", "tracked.txt"])
    run(repo, ["git", "commit", "-m", "local only"])
    (repo / "tracked.txt").write_text("approved after local commit\n", encoding="utf-8")

    result = readiness(runner, ["tracked.txt"])

    assert result["status"] == "ok"
    assert result["ready"] is False
    assert "Current branch is already ahead of upstream" in result["blocking_reasons"]


def test_acceptance_readiness_behind_upstream_not_ready(tmp_path: Path) -> None:
    runner, repo, remote = make_runner(tmp_path)
    other = tmp_path / "other"
    run(tmp_path, ["git", "clone", "-b", "feature", str(remote), str(other)])
    run(other, ["git", "config", "user.email", "test@example.invalid"])
    run(other, ["git", "config", "user.name", "Test User"])
    (other / "tracked.txt").write_text("remote commit\n", encoding="utf-8")
    run(other, ["git", "add", "tracked.txt"])
    run(other, ["git", "commit", "-m", "remote only"])
    run(other, ["git", "push", "origin", "feature"])
    run(repo, ["git", "fetch", "origin", "feature:refs/remotes/origin/feature"])
    (repo / "tracked.txt").write_text("approved local change\n", encoding="utf-8")

    result = readiness(runner, ["tracked.txt"])

    assert result["status"] == "ok"
    assert result["ready"] is False
    assert "Current branch is behind upstream" in result["blocking_reasons"]
