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


def make_runner_with_remote(tmp_path: Path, branch: str = "main") -> tuple[TaskRunner, Path, Path]:
    runner, repo = make_runner(tmp_path, branch=branch)
    remote = tmp_path / "origin.git"
    run(tmp_path, ["git", "init", "--bare", str(remote)])
    run(repo, ["git", "remote", "add", "origin", str(remote)])
    run(repo, ["git", "push", "-u", "origin", branch])
    return runner, repo, remote


def current_branch(repo: Path) -> str:
    return run(repo, ["git", "branch", "--show-current"]).stdout.strip()


def head(repo: Path) -> str:
    return run(repo, ["git", "rev-parse", "HEAD"]).stdout.strip()


def test_branch_status_without_upstream_is_ok(tmp_path: Path) -> None:
    runner, _ = make_runner(tmp_path)

    result = runner.git_get_branch_status("dummy")

    assert result["status"] == "ok"
    assert result["current_branch"] == "main"
    assert result["detached"] is False
    assert result["dirty"] is False
    assert result["upstream"] is None
    assert result["ahead_behind"] is None
    assert result["upstream_result"]["returncode"] != 0


def test_branch_status_with_upstream_reports_ahead_behind(tmp_path: Path) -> None:
    runner, repo, _ = make_runner_with_remote(tmp_path)
    (repo / "tracked.txt").write_text("ahead\n", encoding="utf-8")
    run(repo, ["git", "commit", "-am", "ahead"])

    result = runner.git_get_branch_status("dummy")

    assert result["status"] == "ok"
    assert result["upstream"] == "origin/main"
    assert result["ahead_behind"] == {"ahead": 1, "behind": 0}


def test_branch_status_reports_detached_head(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    run(repo, ["git", "checkout", "--detach", "HEAD"])

    result = runner.git_get_branch_status("dummy")

    assert result["status"] == "ok"
    assert result["current_branch"] is None
    assert result["detached"] is True


def test_create_work_branch_defaults_to_current_branch_and_switches(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    before = head(repo)

    result = runner.git_create_work_branch("dummy", "codex/test-branch")

    assert result["status"] == "created"
    assert result["current_branch_before"] == "main"
    assert result["current_branch_after"] == "codex/test-branch"
    assert result["base_branch"] == "main"
    assert result["head_before"]["stdout"].strip() == before
    assert result["head_after"]["stdout"].strip() == before
    assert current_branch(repo) == "codex/test-branch"


def test_create_work_branch_allows_safe_slash_target_name(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)

    result = runner.git_create_work_branch("dummy", "codex/topic")

    assert result["status"] == "created"
    assert current_branch(repo) == "codex/topic"


def test_create_work_branch_from_explicit_local_base(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    run(repo, ["git", "switch", "-c", "develop"])
    (repo / "tracked.txt").write_text("develop\n", encoding="utf-8")
    run(repo, ["git", "commit", "-am", "develop"])
    develop_head = head(repo)
    run(repo, ["git", "switch", "main"])

    result = runner.git_create_work_branch("dummy", "codex/from-develop", base_branch="develop")

    assert result["status"] == "created"
    assert result["base_branch"] == "develop"
    assert result["base_head"]["stdout"].strip() == develop_head
    assert result["head_after"]["stdout"].strip() == develop_head


def test_create_work_branch_blocks_unstaged_staged_and_untracked_dirty_states(
    tmp_path: Path,
) -> None:
    runner, repo = make_runner(tmp_path)
    (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    unstaged = runner.git_create_work_branch("dummy", "codex/unstaged")
    assert unstaged["status"] == "blocked_dirty"
    assert current_branch(repo) == "main"

    run(repo, ["git", "checkout", "--", "tracked.txt"])
    (repo / "tracked.txt").write_text("staged\n", encoding="utf-8")
    run(repo, ["git", "add", "tracked.txt"])

    staged = runner.git_create_work_branch("dummy", "codex/staged")
    assert staged["status"] == "blocked_dirty"
    assert current_branch(repo) == "main"

    run(repo, ["git", "reset", "--hard"])
    (repo / "new.txt").write_text("new\n", encoding="utf-8")

    untracked = runner.git_create_work_branch("dummy", "codex/untracked")
    assert untracked["status"] == "blocked_dirty"
    assert current_branch(repo) == "main"


def test_create_work_branch_blocks_detached_head(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)
    before = head(repo)
    run(repo, ["git", "checkout", "--detach", "HEAD"])

    result = runner.git_create_work_branch("dummy", "codex/detached")

    assert result["status"] == "blocked_detached_head"
    assert head(repo) == before


def test_create_work_branch_blocks_invalid_branch_names_before_mutation(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)

    invalid_names = [
        " codex/space",
        "codex/space ",
        "codex/with space",
        "codex/ümlaut",
        "refs/heads/main",
        "HEAD",
        "codex..bad",
        "codex:bad",
        "-bad",
    ]

    for name in invalid_names:
        result = runner.git_create_work_branch("dummy", name)
        assert result["status"] == "blocked_input"
        assert current_branch(repo) == "main"


def test_create_work_branch_blocks_missing_existing_and_remote_style_base(
    tmp_path: Path,
) -> None:
    runner, repo, _ = make_runner_with_remote(tmp_path)

    missing = runner.git_create_work_branch("dummy", "codex/missing", base_branch="missing")
    assert missing["status"] == "blocked_base_branch"
    assert current_branch(repo) == "main"

    existing = runner.git_create_work_branch("dummy", "main")
    assert existing["status"] == "blocked_branch_exists"
    assert current_branch(repo) == "main"

    remote_style = runner.git_create_work_branch(
        "dummy",
        "codex/remote-style",
        base_branch="origin/main",
    )
    assert remote_style["status"] == "blocked_input"
    assert current_branch(repo) == "main"


def test_create_work_branch_blocks_refs_heads_and_head_bases(tmp_path: Path) -> None:
    runner, repo = make_runner(tmp_path)

    origin_target = runner.git_create_work_branch("dummy", "origin/main")
    assert origin_target["status"] == "blocked_input"
    assert current_branch(repo) == "main"

    upstream_target = runner.git_create_work_branch("dummy", "upstream/topic")
    assert upstream_target["status"] == "blocked_input"
    assert current_branch(repo) == "main"

    origin_base = runner.git_create_work_branch("dummy", "codex/origin", base_branch="origin/main")
    assert origin_base["status"] == "blocked_input"
    assert current_branch(repo) == "main"

    refs_base = runner.git_create_work_branch("dummy", "codex/refs", base_branch="refs/heads/main")
    assert refs_base["status"] == "blocked_input"
    assert current_branch(repo) == "main"

    head_base = runner.git_create_work_branch("dummy", "codex/head", base_branch="HEAD")
    assert head_base["status"] == "blocked_input"
    assert current_branch(repo) == "main"


def test_show_ref_unexpected_returncode_blocks_git(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    runner, _ = make_runner(tmp_path)
    original_run = runner._run

    def fake_run(cwd: Path, cmd: list[str], timeout: int, max_chars: int = 20000) -> dict[str, Any]:
        if cmd[:4] == ["git", "show-ref", "--verify", "--quiet"]:
            return {"cmd": cmd, "returncode": 2, "stdout": "", "stderr": "boom"}
        return original_run(cwd, cmd, timeout, max_chars)

    monkeypatch.setattr(runner, "_run", fake_run)

    result = runner.git_create_work_branch("dummy", "codex/show-ref-error")

    assert result["status"] == "blocked_git"
    assert result["existing_branch"]["show_ref"]["returncode"] == 2


def test_porcelain_status_failure_blocks_instead_of_guessing(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    runner, _ = make_runner(tmp_path)

    def fake_porcelain_status(repo: Path) -> dict[str, Any]:
        return {
            "cmd": ["git", "status", "--porcelain=v1", "--untracked-files=normal"],
            "returncode": 1,
            "stdout": "",
            "stderr": "status failed",
        }

    monkeypatch.setattr(runner, "_porcelain_status", fake_porcelain_status)

    result = runner.git_create_work_branch("dummy", "codex/status-error")

    assert result["status"] == "blocked_git"
    assert result["porcelain_status_before"]["returncode"] == 1


def test_post_switch_branch_mismatch_does_not_return_created(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    runner, repo = make_runner(tmp_path)
    original_current_branch = runner._current_branch
    calls = 0

    def fake_current_branch(repo: Path) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls >= 2:
            return {
                "cmd": ["git", "branch", "--show-current"],
                "returncode": 0,
                "stdout": "main\n",
                "stderr": "",
            }
        return original_current_branch(repo)

    monkeypatch.setattr(runner, "_current_branch", fake_current_branch)

    result = runner.git_create_work_branch("dummy", "codex/post-switch-mismatch")

    assert result["status"] == "blocked_git"
    assert "final branch state" in result["error"]
    assert result["current_branch_after"] == "main"
    assert current_branch(repo) == "codex/post-switch-mismatch"


def test_post_switch_probe_failure_does_not_return_created(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    runner, repo = make_runner(tmp_path)
    original_porcelain_status = runner._porcelain_status
    calls = 0

    def fake_porcelain_status(repo: Path) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls >= 2:
            return {
                "cmd": ["git", "status", "--porcelain=v1", "--untracked-files=normal"],
                "returncode": 1,
                "stdout": "",
                "stderr": "status failed",
            }
        return original_porcelain_status(repo)

    monkeypatch.setattr(runner, "_porcelain_status", fake_porcelain_status)

    result = runner.git_create_work_branch("dummy", "codex/post-switch-failure")

    assert result["status"] == "blocked_git"
    assert "final branch state" in result["error"]
    assert result["porcelain_status_after"]["returncode"] == 1
    assert current_branch(repo) == "codex/post-switch-failure"
