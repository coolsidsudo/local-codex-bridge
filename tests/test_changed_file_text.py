from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from local_codex_bridge.config import BridgeConfig, ProjectConfig, ServerConfig
from local_codex_bridge.task_runner import CHANGED_FILE_TEXT_MAX_READ_BYTES, TaskRunner


def run(cwd: Path, cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=check,
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
    (repo / "other.txt").write_text("other unchanged secret\n", encoding="utf-8")
    run(repo, ["git", "add", "."])
    run(repo, ["git", "commit", "-m", "initial"])

    cfg = BridgeConfig(
        server=ServerConfig(task_dir=tmp_path / "tasks"),
        projects={"dummy": ProjectConfig(name="Dummy", path=repo)},
    )
    return TaskRunner(cfg), repo


def test_unstaged_worktree_text_is_targeted(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("initial\nunstaged change\n", encoding="utf-8")
    (repo / "other.txt").write_text("other unchanged secret\nunrequested\n", encoding="utf-8")

    worktree = runner.get_changed_file_text("dummy", "tracked.txt", source="worktree")
    unstaged = runner.get_changed_file_text("dummy", "tracked.txt", source="unstaged")

    assert worktree["status"] == "ok"
    assert worktree["source_requested"] == "worktree"
    assert worktree["source_resolved"] == "worktree"
    assert worktree["content"]["text"] == "initial\nunstaged change\n"
    assert "unrequested" not in worktree["content"]["text"]
    assert worktree["limits"]["changed_file_only"] is True

    assert unstaged["status"] == "ok"
    assert unstaged["source_requested"] == "unstaged"
    assert unstaged["source_resolved"] == "worktree"
    assert unstaged["content"]["text"] == "initial\nunstaged change\n"
    assert "unrequested" not in unstaged["content"]["text"]
    assert unstaged["limits"]["changed_file_only"] is True


def test_staged_text_reads_index_content(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("initial\nstaged\n", encoding="utf-8")
    run(repo, ["git", "add", "tracked.txt"])
    (repo / "tracked.txt").write_text("initial\nstaged\nworktree\n", encoding="utf-8")

    result = runner.get_changed_file_text("dummy", "tracked.txt", source="staged")

    assert result["status"] == "ok"
    assert result["source_resolved"] == "staged"
    assert result["content"]["text"] == "initial\nstaged\n"
    assert "worktree" not in result["content"]["text"]


def test_staged_only_can_read_staged_and_worktree_text(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    expected = "initial\nstaged only\n"
    (repo / "tracked.txt").write_text(expected, encoding="utf-8")
    run(repo, ["git", "add", "tracked.txt"])

    staged = runner.get_changed_file_text("dummy", "tracked.txt", source="staged")
    worktree = runner.get_changed_file_text("dummy", "tracked.txt", source="worktree")

    assert staged["status"] == "ok"
    assert worktree["status"] == "ok"
    assert staged["content"]["text"] == expected
    assert worktree["content"]["text"] == expected


def test_mixed_staged_and_unstaged_auto_blocks_as_ambiguous(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("initial\nstaged\n", encoding="utf-8")
    run(repo, ["git", "add", "tracked.txt"])
    (repo / "tracked.txt").write_text("initial\nstaged\nworktree\n", encoding="utf-8")

    result = runner.get_changed_file_text("dummy", "tracked.txt")

    assert result["status"] == "blocked_ambiguous_source"
    assert result["available_sources"] == ["staged", "worktree"]


def test_explicit_sources_read_different_snapshots_for_mixed_state(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("initial\nstaged\n", encoding="utf-8")
    run(repo, ["git", "add", "tracked.txt"])
    (repo / "tracked.txt").write_text("initial\nstaged\nworktree\n", encoding="utf-8")

    staged = runner.get_changed_file_text("dummy", "tracked.txt", source="staged")
    worktree = runner.get_changed_file_text("dummy", "tracked.txt", source="worktree")

    assert staged["status"] == "ok"
    assert worktree["status"] == "ok"
    assert staged["content"]["text"] == "initial\nstaged\n"
    assert worktree["content"]["text"] == "initial\nstaged\nworktree\n"


def test_untracked_text_reads_exact_safe_path(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "new.txt").write_text("hello\n", encoding="utf-8")

    result = runner.get_changed_file_text("dummy", "new.txt", source="untracked")

    assert result["status"] == "ok"
    assert result["source_resolved"] == "untracked"
    assert result["content"]["text"] == "hello\n"


def test_untracked_binary_refusal(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "binary.dat").write_bytes(b"abc\x00def")

    result = runner.get_changed_file_text("dummy", "binary.dat", source="untracked")

    assert result["status"] == "blocked_unsafe"
    assert result["reason"] == "binary"
    assert "content" not in result


def test_tracked_binary_refusal(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "binary.bin").write_bytes(b"abc\x00def")
    run(repo, ["git", "add", "binary.bin"])
    run(repo, ["git", "commit", "-m", "add binary"])
    (repo / "binary.bin").write_bytes(b"abc\x00changed")

    result = runner.get_changed_file_text("dummy", "binary.bin", source="worktree")

    assert result["status"] == "blocked_unsafe"
    assert result["reason"] == "binary"


def test_invalid_utf8_refusal(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "tracked.txt").write_bytes(b"initial\n\xff\n")

    result = runner.get_changed_file_text("dummy", "tracked.txt", source="worktree")

    assert result["status"] == "blocked_unsafe"
    assert result["reason"] == "invalid_utf8"


def test_symlink_refusal(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlink is not available on this platform")
    runner, repo = make_runner(tmp_path)
    try:
        os.symlink("tracked.txt", repo / "link.txt")
    except OSError as exc:
        pytest.skip(f"symlink could not be created: {exc}")

    result = runner.get_changed_file_text("dummy", "link.txt", source="untracked")

    assert result["status"] == "blocked_unsafe"
    assert result["reason"] == "symlink"


def test_directory_refusal(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "dir").mkdir()

    result = runner.get_changed_file_text("dummy", "dir", source="untracked")

    assert result["status"] == "blocked_unsafe"
    assert result["reason"] == "directory"


@pytest.mark.parametrize("path", ["../outside.txt", "/tmp/outside.txt", "."])
def test_path_refusals(tmp_path: Path, path: str) -> None:
    runner, _ = make_runner(tmp_path)

    result = runner.get_changed_file_text("dummy", path)

    assert result["status"] == "blocked_input"


def test_invalid_source_refusal(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path)

    result = runner.get_changed_file_text("dummy", "tracked.txt", source="invalid")

    assert result["status"] == "blocked_input"
    assert result["error"] == "source must be one of auto, worktree, unstaged, staged, untracked"
    assert result["allowed_sources"] == ["auto", "staged", "unstaged", "untracked", "worktree"]


def test_clean_unchanged_path_refusal(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path)

    result = runner.get_changed_file_text("dummy", "tracked.txt")

    assert result["status"] == "blocked_unchanged"


def test_unstaged_deleted_file_refuses_content_with_diff_suggestion(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "tracked.txt").unlink()

    result = runner.get_changed_file_text("dummy", "tracked.txt", source="worktree")

    assert result["status"] == "blocked_deleted"
    assert result["reason"] == "deleted"
    assert result["suggested_next_tool"] == "get_changed_file_diff"


def test_staged_deleted_file_refuses_content_with_diff_suggestion(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    run(repo, ["git", "rm", "tracked.txt"])

    result = runner.get_changed_file_text("dummy", "tracked.txt", source="staged")

    assert result["status"] == "blocked_deleted"
    assert result["reason"] == "deleted"
    assert result["suggested_next_tool"] == "get_changed_file_diff"


def test_rename_old_path_has_no_current_content(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    run(repo, ["git", "mv", "tracked.txt", "renamed.txt"])

    result = runner.get_changed_file_text("dummy", "tracked.txt", source="staged")

    assert result["status"] == "blocked_deleted"
    assert result["suggested_next_tool"] == "get_changed_file_diff"


def test_rename_new_path_reads_staged_content(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    run(repo, ["git", "mv", "tracked.txt", "renamed.txt"])

    result = runner.get_changed_file_text("dummy", "renamed.txt", source="staged")

    assert result["status"] == "ok"
    assert result["content"]["text"] == "initial\n"


def test_max_chars_truncation_metadata(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("initial\n" + ("x\n" * 100), encoding="utf-8")

    result = runner.get_changed_file_text("dummy", "tracked.txt", max_chars=10)

    assert result["status"] == "ok"
    assert result["content"]["text"] == "initial\nx\n"
    assert result["content"]["chars"] == 10
    assert result["truncation"]["truncated"] is True
    assert result["truncation"]["omitted_chars"] > 0
    assert result["truncation"]["omitted_bytes"] > 0


def test_max_chars_zero_returns_empty_text_with_metadata(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("initial\nchanged\n", encoding="utf-8")

    result = runner.get_changed_file_text("dummy", "tracked.txt", max_chars=0)

    assert result["status"] == "ok"
    assert result["content"]["text"] == ""
    assert result["content"]["chars"] == 0
    assert result["truncation"]["truncated"] is True
    assert result["truncation"]["omitted_bytes"] == len("initial\nchanged\n".encode())


def test_unchanged_file_content_is_not_returned_when_other_file_changed(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("initial\nchanged\n", encoding="utf-8")

    result = runner.get_changed_file_text("dummy", "tracked.txt")

    assert result["status"] == "ok"
    assert "other unchanged secret" not in result["content"]["text"]


def test_worktree_source_refuses_untracked_only_path(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "new.txt").write_text("hello\n", encoding="utf-8")

    result = runner.get_changed_file_text("dummy", "new.txt", source="worktree")

    assert result["status"] == "blocked_unchanged"
    assert result["available_sources"] == ["untracked"]


def test_staged_blob_reader_uses_bounded_prefix_for_large_blob(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("initial\n" + ("x" * 2_000_000), encoding="utf-8")
    run(repo, ["git", "add", "tracked.txt"])
    max_bytes_seen: list[int] = []
    original = runner._read_git_blob_prefix

    def recording_read_blob(
        repo_arg: Path,
        oid: str,
        *,
        max_bytes: int,
        blob_size: int,
    ) -> dict:
        max_bytes_seen.append(max_bytes)
        return original(repo_arg, oid, max_bytes=max_bytes, blob_size=blob_size)

    monkeypatch.setattr(runner, "_read_git_blob_prefix", recording_read_blob)

    result = runner.get_changed_file_text("dummy", "tracked.txt", source="staged", max_chars=20)

    assert result["status"] == "ok"
    assert max_bytes_seen
    assert max_bytes_seen[0] < 2_000_000
    assert max_bytes_seen[0] <= CHANGED_FILE_TEXT_MAX_READ_BYTES


def test_conflict_multi_stage_index_refusal(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    run(repo, ["git", "checkout", "-b", "other"])
    (repo / "tracked.txt").write_text("other branch\n", encoding="utf-8")
    run(repo, ["git", "commit", "-am", "other change"])
    run(repo, ["git", "checkout", "main"])
    (repo / "tracked.txt").write_text("main branch\n", encoding="utf-8")
    run(repo, ["git", "commit", "-am", "main change"])
    merge = run(repo, ["git", "merge", "other"], check=False)
    assert merge.returncode != 0

    result = runner.get_changed_file_text("dummy", "tracked.txt", source="staged")

    assert result["status"] == "blocked_conflict"
    assert result["reason"] == "multi_stage_index"
