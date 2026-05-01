from __future__ import annotations

import os
import subprocess
from pathlib import Path

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


def make_runner(tmp_path: Path) -> tuple[TaskRunner, Path]:
    repo = tmp_path / "project"
    repo.mkdir()
    run(repo, ["git", "init", "-b", "feature"])
    run(repo, ["git", "config", "user.email", "test@example.invalid"])
    run(repo, ["git", "config", "user.name", "Test User"])
    (repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
    run(repo, ["git", "add", "tracked.txt"])
    run(repo, ["git", "commit", "-m", "initial"])

    cfg = BridgeConfig(
        server=ServerConfig(task_dir=tmp_path / "tasks"),
        projects={"dummy": ProjectConfig(name="Dummy", path=repo)},
    )
    return TaskRunner(cfg), repo


def preview_item(result: dict, path: str) -> dict:
    matches = [item for item in result["untracked_previews"]["items"] if item["path"] == path]
    assert len(matches) == 1
    return matches[0]


def test_unstaged_modification_appears_in_old_and_new_unstaged_fields(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("initial\nunstaged change\n", encoding="utf-8")

    result = runner.git_diff("dummy")

    assert result["stat"] == result["unstaged_stat"]
    assert result["diff"] == result["unstaged_diff"]
    assert "tracked.txt" in result["diff"]["stdout"]
    assert "unstaged change" in result["diff"]["stdout"]
    assert "tracked.txt" in result["unstaged_stat"]["stdout"]


def test_staged_modification_appears_in_staged_fields_only(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("initial\nstaged change\n", encoding="utf-8")
    run(repo, ["git", "add", "tracked.txt"])

    result = runner.git_diff("dummy")

    assert "staged change" in result["staged_diff"]["stdout"]
    assert "tracked.txt" in result["staged_stat"]["stdout"]
    assert result["diff"]["stdout"] == ""
    assert result["unstaged_diff"]["stdout"] == ""


def test_untracked_text_file_is_listed_and_previewed(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "new.txt").write_text("hello from an untracked file\n", encoding="utf-8")

    result = runner.git_diff("dummy")

    assert "new.txt" in result["untracked_files"]["files"]
    item = preview_item(result, "new.txt")
    assert item["status"] == "included"
    assert item["text"] == "hello from an untracked file\n"
    assert item["truncated"] is False


def test_large_untracked_text_file_preview_is_bounded_and_truncated(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "large.txt").write_text("x" * 5000, encoding="utf-8")

    result = runner.git_diff("dummy")

    item = preview_item(result, "large.txt")
    assert item["status"] == "included"
    assert len(item["text"]) == 4096
    assert item["chars"] == 4096
    assert item["bytes_read"] == 4096
    assert item["truncated"] is True


def test_binary_untracked_file_is_listed_but_preview_is_skipped(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "binary.dat").write_bytes(b"abc\x00def")

    result = runner.git_diff("dummy")

    assert "binary.dat" in result["untracked_files"]["files"]
    item = preview_item(result, "binary.dat")
    assert item == {"path": "binary.dat", "status": "skipped", "reason": "binary"}


def test_untracked_symlink_is_listed_but_preview_is_skipped(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlink is not available on this platform")
    runner, repo = make_runner(tmp_path)
    try:
        os.symlink("tracked.txt", repo / "link.txt")
    except OSError as exc:
        pytest.skip(f"symlink could not be created: {exc}")

    result = runner.git_diff("dummy")

    assert "link.txt" in result["untracked_files"]["files"]
    item = preview_item(result, "link.txt")
    assert item == {"path": "link.txt", "status": "skipped", "reason": "symlink"}
