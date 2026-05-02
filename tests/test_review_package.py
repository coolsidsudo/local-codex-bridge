from __future__ import annotations

import json
import subprocess
from pathlib import Path

from local_codex_bridge.config import BridgeConfig, ProjectConfig, ServerConfig
from local_codex_bridge.task_runner import TaskRunner, UNTRACKED_PREVIEW_MAX_FILES


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


def make_runner(tmp_path: Path, branch: str = "main") -> tuple[TaskRunner, Path]:
    repo = tmp_path / "project"
    repo.mkdir()
    run(repo, ["git", "init", "-b", branch])
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


def make_runner_with_remote(tmp_path: Path, branch: str = "main") -> tuple[TaskRunner, Path]:
    runner, repo = make_runner(tmp_path, branch=branch)
    remote = tmp_path / "origin.git"
    run(tmp_path, ["git", "init", "--bare", str(remote)])
    run(repo, ["git", "remote", "add", "origin", str(remote)])
    run(repo, ["git", "push", "-u", "origin", branch])
    return runner, repo


def review_file(result: dict, path: str) -> dict:
    matches = [item for item in result["files"] if item["path"] == path]
    assert len(matches) == 1
    return matches[0]


def preview_item(result: dict, path: str) -> dict:
    matches = [item for item in result["untracked_previews"]["items"] if item["path"] == path]
    assert len(matches) == 1
    return matches[0]


def dumped(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


def test_clean_repo_returns_empty_review_index(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path)

    result = runner.get_review_package("dummy")

    assert result["status"] == "ok"
    assert result["package_version"] == 1
    assert result["repo"]["dirty"] is False
    assert result["limits"]["full_diffs_included"] is False
    assert result["limits"]["full_file_contents_included"] is False
    assert result["summary"]["changed_file_count"] == 0
    assert result["files"] == []


def test_unstaged_text_modification_is_indexed_without_full_diff(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("initial\nunstaged change\n", encoding="utf-8")

    result = runner.get_review_package("dummy")
    item = review_file(result, "tracked.txt")

    assert item["change_type"] == "modified"
    assert item["staged"] is False
    assert item["unstaged"] is True
    assert item["binary_text_status"] == "text"
    assert item["diff_available"] is True
    assert item["suggested_diff_source"] == "unstaged"
    output = dumped(result)
    assert "diff --git" not in output
    assert "unstaged change" not in output


def test_staged_text_modification_is_indexed(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("initial\nstaged change\n", encoding="utf-8")
    run(repo, ["git", "add", "tracked.txt"])

    result = runner.get_review_package("dummy", max_chars=100000)
    item = review_file(result, "tracked.txt")

    assert item["staged"] is True
    assert item["unstaged"] is False
    assert item["diff_available"] is True
    assert item["suggested_diff_source"] == "staged"
    assert "staged change" not in dumped(result)


def test_mixed_staged_and_unstaged_change_uses_auto_source(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("initial\nstaged\n", encoding="utf-8")
    run(repo, ["git", "add", "tracked.txt"])
    (repo / "tracked.txt").write_text("initial\nstaged\nunstaged\n", encoding="utf-8")

    result = runner.get_review_package("dummy")
    item = review_file(result, "tracked.txt")

    assert item["staged"] is True
    assert item["unstaged"] is True
    assert item["suggested_diff_source"] == "auto"
    assert "mixed_staged_unstaged" in item["reason_codes"]


def test_untracked_long_text_gets_partial_excerpt_only(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    content = "x" * 5000
    (repo / "long.txt").write_text(content, encoding="utf-8")

    result = runner.get_review_package("dummy")
    item = review_file(result, "long.txt")
    preview = preview_item(result, "long.txt")

    assert item["change_type"] == "untracked"
    assert item["safe_preview_available"] is True
    assert item["diff_available"] is True
    assert item["suggested_diff_source"] == "untracked"
    assert preview["excerpt_included"] is True
    assert preview["excerpt_is_partial"] is True
    assert len(preview["excerpt"]) == 1000
    assert content not in dumped(result)


def test_short_untracked_text_omits_full_content(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    content = "short untracked content\n"
    (repo / "short.txt").write_text(content, encoding="utf-8")

    result = runner.get_review_package("dummy")
    preview = preview_item(result, "short.txt")

    assert preview["safe_preview_available"] is True
    assert preview["excerpt_included"] is False
    assert preview["reason"] == "safe_short_file_full_content_omitted"
    assert content not in dumped(result)


def test_untracked_binary_is_safely_classified(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "binary.dat").write_bytes(b"abc\x00def")

    result = runner.get_review_package("dummy")
    item = review_file(result, "binary.dat")
    preview = preview_item(result, "binary.dat")

    assert item["binary_text_status"] == "binary"
    assert item["diff_available"] is False
    assert preview["status"] == "skipped"
    assert preview["reason"] == "binary"


def test_deleted_file_is_indexed(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "tracked.txt").unlink()

    result = runner.get_review_package("dummy")
    item = review_file(result, "tracked.txt")

    assert item["change_type"] == "deleted"
    assert item["unstaged"] is True
    assert item["diff_available"] is True


def test_renamed_file_includes_old_path(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    run(repo, ["git", "mv", "tracked.txt", "renamed.txt"])

    result = runner.get_review_package("dummy")
    item = review_file(result, "renamed.txt")

    assert item["change_type"] == "renamed"
    assert item["old_path"] == "tracked.txt"
    assert item["staged"] is True


def test_binary_tracked_change_is_not_diff_available(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "binary.bin").write_bytes(b"abc\x00def")
    run(repo, ["git", "add", "binary.bin"])
    run(repo, ["git", "commit", "-m", "add binary"])
    (repo / "binary.bin").write_bytes(b"abc\x00changed")

    result = runner.get_review_package("dummy")
    item = review_file(result, "binary.bin")

    assert item["binary_text_status"] == "binary"
    assert item["diff_available"] is False
    assert item["likely_needs_targeted_review"] is True


def test_detached_head_is_reported_without_blocking(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    run(repo, ["git", "checkout", "--detach", "HEAD"])

    result = runner.get_review_package("dummy")

    assert result["status"] == "ok"
    assert result["repo"]["current_branch"] is None
    assert result["repo"]["detached"] is True


def test_missing_upstream_is_non_fatal(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path)

    result = runner.get_review_package("dummy")

    assert result["status"] == "ok"
    assert result["repo"]["upstream"] is None
    assert result["repo"]["ahead_behind"] is None


def test_upstream_ahead_behind_is_reported(tmp_path: Path) -> None:
    runner, repo = make_runner_with_remote(tmp_path)
    (repo / "tracked.txt").write_text("ahead\n", encoding="utf-8")
    run(repo, ["git", "commit", "-am", "ahead"])

    result = runner.get_review_package("dummy")

    assert result["status"] == "ok"
    assert result["repo"]["upstream"] == "origin/main"
    assert result["repo"]["ahead_behind"] == {"ahead": 1, "behind": 0}


def test_output_truncation_preserves_summary_counts(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    for index in range(40):
        (repo / f"file-{index:02d}.txt").write_text("short\n", encoding="utf-8")

    result = runner.get_review_package("dummy", max_chars=1500)

    assert result["status"] == "ok"
    assert result["summary"]["changed_file_count"] == 40
    assert result["truncation"]["truncated"] is True
    assert result["truncation"]["omitted_file_count"] > 0
    assert "files" in result["truncation"]["omitted_sections"]


def test_full_z_output_is_parsed_before_bounded_evidence_truncation(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    tracked_count = 350
    for index in range(tracked_count):
        path = repo / f"tracked-review-package-long-path-{index:03d}-aaaaaaaaaaaaaaaaaaaa.txt"
        path.write_text("initial\n", encoding="utf-8")
    run(repo, ["git", "add", "."])
    run(repo, ["git", "commit", "-m", "add many tracked files"])

    for index in range(tracked_count):
        path = repo / f"tracked-review-package-long-path-{index:03d}-aaaaaaaaaaaaaaaaaaaa.txt"
        path.write_text("initial\nchanged\n", encoding="utf-8")

    result = runner.get_review_package("dummy", max_chars=1500)

    assert result["status"] == "ok"
    assert result["summary"]["changed_file_count"] == tracked_count
    assert result["summary"]["unstaged_count"] == tracked_count
    assert result["evidence"]["porcelain_status_z"]["stdout_truncated"] is True
    assert result["truncation"]["truncated"] is True
    assert result["truncation"]["omitted_file_count"] > 0
    assert "diff --git" not in dumped(result)


def test_untracked_previews_stop_at_preview_cap_without_losing_counts(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    untracked_count = UNTRACKED_PREVIEW_MAX_FILES + 5
    for index in range(untracked_count):
        (repo / f"untracked-{index:02d}.txt").write_text("short\n", encoding="utf-8")

    result = runner.get_review_package("dummy", max_chars=100000)

    assert result["status"] == "ok"
    assert result["summary"]["changed_file_count"] == untracked_count
    assert result["summary"]["untracked_count"] == untracked_count
    assert result["untracked_files"]["files"]
    assert len(result["untracked_files"]["files"]) == untracked_count
    previews = result["untracked_previews"]
    assert previews["truncated"] is True
    assert previews["omitted_count"] == 5
    assert sum(1 for item in previews["items"] if item["status"] == "available") == (
        UNTRACKED_PREVIEW_MAX_FILES
    )
    assert sum(1 for item in previews["items"] if item.get("reason") == "preview_limit") == 5
    assert "short\n" not in dumped(result)
