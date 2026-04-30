from __future__ import annotations

import subprocess
from pathlib import Path

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
