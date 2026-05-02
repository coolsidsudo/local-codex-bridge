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
    run(repo, ["git", "init", "-b", "main"])
    run(repo, ["git", "config", "user.email", "test@example.invalid"])
    run(repo, ["git", "config", "user.name", "Test User"])
    (repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
    (repo / "other.txt").write_text("other\n", encoding="utf-8")
    run(repo, ["git", "add", "."])
    run(repo, ["git", "commit", "-m", "initial"])

    cfg = BridgeConfig(
        server=ServerConfig(task_dir=tmp_path / "tasks"),
        projects={"dummy": ProjectConfig(name="Dummy", path=repo)},
    )
    return TaskRunner(cfg), repo


def test_unstaged_text_diff_is_targeted(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("initial\nunstaged change\n", encoding="utf-8")
    (repo / "other.txt").write_text("other\nunrequested change\n", encoding="utf-8")

    result = runner.get_changed_file_diff("dummy", "tracked.txt", source="unstaged")

    assert result["status"] == "ok"
    assert result["source_resolved"] == "unstaged"
    assert result["file"]["unstaged"] is True
    assert "unstaged change" in result["diff"]["stdout"]
    assert "unrequested change" not in result["diff"]["stdout"]
    assert result["diff"]["cmd"] == ["git", "diff", "--", "tracked.txt"]


def test_staged_text_diff_is_targeted(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("initial\nstaged change\n", encoding="utf-8")
    run(repo, ["git", "add", "tracked.txt"])

    result = runner.get_changed_file_diff("dummy", "tracked.txt", source="staged")

    assert result["status"] == "ok"
    assert result["source_resolved"] == "staged"
    assert "staged change" in result["diff"]["stdout"]
    assert result["diff"]["cmd"] == ["git", "diff", "--cached", "--", "tracked.txt"]


def test_auto_resolves_only_unstaged(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("initial\nauto unstaged\n", encoding="utf-8")

    result = runner.get_changed_file_diff("dummy", "tracked.txt")

    assert result["status"] == "ok"
    assert result["source_requested"] == "auto"
    assert result["source_resolved"] == "unstaged"


def test_auto_resolves_only_staged(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("initial\nauto staged\n", encoding="utf-8")
    run(repo, ["git", "add", "tracked.txt"])

    result = runner.get_changed_file_diff("dummy", "tracked.txt")

    assert result["status"] == "ok"
    assert result["source_resolved"] == "staged"


def test_auto_blocks_mixed_staged_and_unstaged(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("initial\nstaged\n", encoding="utf-8")
    run(repo, ["git", "add", "tracked.txt"])
    (repo / "tracked.txt").write_text("initial\nstaged\nunstaged\n", encoding="utf-8")

    result = runner.get_changed_file_diff("dummy", "tracked.txt")

    assert result["status"] == "blocked_ambiguous_source"
    assert result["available_sources"] == ["staged", "unstaged"]


def test_untracked_text_diff_treats_no_index_returncode_one_as_success(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "new.txt").write_text("hello\n", encoding="utf-8")

    result = runner.get_changed_file_diff("dummy", "new.txt", source="untracked")

    assert result["status"] == "ok"
    assert result["source_resolved"] == "untracked"
    assert result["diff"]["returncode"] == 1
    assert "hello" in result["diff"]["stdout"]
    assert result["diff"]["cmd"] == ["git", "diff", "--no-index", "--", os.devnull, "new.txt"]


def test_untracked_binary_refusal(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "binary.dat").write_bytes(b"abc\x00def")

    result = runner.get_changed_file_diff("dummy", "binary.dat", source="untracked")

    assert result["status"] == "blocked_unsafe"
    assert result["reason"] == "binary"
    assert "diff" not in result


def test_symlink_refusal(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlink is not available on this platform")
    runner, repo = make_runner(tmp_path)
    try:
        os.symlink("tracked.txt", repo / "link.txt")
    except OSError as exc:
        pytest.skip(f"symlink could not be created: {exc}")

    result = runner.get_changed_file_diff("dummy", "link.txt", source="untracked")

    assert result["status"] == "blocked_unsafe"
    assert result["reason"] == "symlink"


def test_directory_refusal(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "dir").mkdir()

    result = runner.get_changed_file_diff("dummy", "dir", source="untracked")

    assert result["status"] == "blocked_unsafe"
    assert result["reason"] == "directory"


def test_outside_path_refusal(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path)

    result = runner.get_changed_file_diff("dummy", "../outside.txt")

    assert result["status"] == "blocked_input"


def test_clean_path_refusal(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path)

    result = runner.get_changed_file_diff("dummy", "tracked.txt")

    assert result["status"] == "blocked_unchanged"


def test_deleted_tracked_text_file_diff(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "tracked.txt").unlink()

    result = runner.get_changed_file_diff("dummy", "tracked.txt", source="unstaged")

    assert result["status"] == "ok"
    assert result["file"]["change_type"] == "deleted"
    assert "-initial" in result["diff"]["stdout"]


def test_staged_deleted_tracked_text_file_diff(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    run(repo, ["git", "rm", "tracked.txt"])

    result = runner.get_changed_file_diff("dummy", "tracked.txt", source="staged")

    assert result["status"] == "ok"
    assert result["source_resolved"] == "staged"
    assert result["file"]["change_type"] == "deleted"
    assert "-initial" in result["diff"]["stdout"]


def test_auto_resolves_staged_deleted_tracked_text_file(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    run(repo, ["git", "rm", "tracked.txt"])

    result = runner.get_changed_file_diff("dummy", "tracked.txt")

    assert result["status"] == "ok"
    assert result["source_resolved"] == "staged"
    assert result["file"]["change_type"] == "deleted"


def test_deleted_tracked_binary_file_refusal(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "binary.bin").write_bytes(b"abc\x00def")
    run(repo, ["git", "add", "binary.bin"])
    run(repo, ["git", "commit", "-m", "add binary"])
    (repo / "binary.bin").unlink()

    result = runner.get_changed_file_diff("dummy", "binary.bin", source="unstaged")

    assert result["status"] == "blocked_unsafe"
    assert result["reason"] == "binary"


def test_staged_deleted_tracked_binary_file_refusal(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "binary.bin").write_bytes(b"abc\x00def")
    run(repo, ["git", "add", "binary.bin"])
    run(repo, ["git", "commit", "-m", "add binary"])
    run(repo, ["git", "rm", "binary.bin"])

    result = runner.get_changed_file_diff("dummy", "binary.bin", source="staged")

    assert result["status"] == "blocked_unsafe"
    assert result["reason"] == "binary"


def test_renamed_file_uses_new_path(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    run(repo, ["git", "mv", "tracked.txt", "renamed.txt"])

    old_path = runner.get_changed_file_diff("dummy", "tracked.txt", source="staged")
    new_path = runner.get_changed_file_diff("dummy", "renamed.txt", source="staged")

    assert old_path["status"] == "ok"
    assert old_path["file"]["change_type"] == "deleted"
    assert new_path["status"] == "ok"
    assert new_path["normalized_path"] == "renamed.txt"


def test_changed_file_diff_does_not_run_repo_wide_rename_scans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo = make_runner(tmp_path)
    run(repo, ["git", "mv", "tracked.txt", "renamed.txt"])
    commands: list[list[str]] = []
    original = runner._git_z_full

    def recording_git_z_full(cwd: Path, cmd: list[str], timeout: int, evidence_max_chars: int = 20000):
        commands.append(cmd)
        return original(cwd, cmd, timeout, evidence_max_chars)

    monkeypatch.setattr(runner, "_git_z_full", recording_git_z_full)

    result = runner.get_changed_file_diff("dummy", "renamed.txt", source="staged")

    assert result["status"] == "ok"
    for cmd in commands:
        if cmd[:3] == ["git", "diff", "--name-status"] and "--find-renames" in cmd:
            assert "--" in cmd
            assert cmd[cmd.index("--") + 1 :] == ["renamed.txt"]


def test_max_chars_prefix_truncation_metadata(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("initial\n" + ("x\n" * 100), encoding="utf-8")

    result = runner.get_changed_file_diff("dummy", "tracked.txt", max_chars=20)

    assert result["status"] == "ok"
    assert result["diff"]["stdout"] == result["diff"]["stdout"][:20]
    assert result["diff"]["stdout"].startswith("diff --git")
    assert result["truncation"]["truncated"] is True
    assert result["truncation"]["stdout_chars"] == 20
    assert result["truncation"]["omitted_chars"] > 0


def test_max_chars_zero_omits_nonempty_diff(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("initial\nchanged\n", encoding="utf-8")

    result = runner.get_changed_file_diff("dummy", "tracked.txt", max_chars=0)

    assert result["status"] == "ok"
    assert result["diff"]["stdout"] == ""
    assert result["truncation"]["truncated"] is True
    assert result["truncation"]["stdout_chars"] == 0
    assert result["truncation"]["omitted_chars"] > 0
