from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from local_codex_bridge.config import BridgeConfig, ProjectConfig, ServerConfig
from local_codex_bridge.task_runner import TaskRunner


OWNER = "coolsidsudo"
REPO = "local-codex-bridge"
REPO_ARG = f"{OWNER}/{REPO}"
HTTPS_REMOTE = f"https://github.com/{REPO_ARG}.git"
SSH_REMOTE = f"git@github.com:{REPO_ARG}.git"


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


def make_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    branch: str = "feature",
    remote_url: str = HTTPS_REMOTE,
    install_gh: bool = True,
) -> tuple[TaskRunner, Path, Path]:
    repo = tmp_path / "project"
    repo.mkdir(parents=True)
    run(repo, ["git", "init", "-b", "main"])
    run(repo, ["git", "config", "user.email", "test@example.invalid"])
    run(repo, ["git", "config", "user.name", "Test User"])
    (repo / "tracked.txt").write_text("main\n", encoding="utf-8")
    run(repo, ["git", "add", "tracked.txt"])
    run(repo, ["git", "commit", "-m", "initial"])
    if branch != "main":
        run(repo, ["git", "switch", "-c", branch])
        (repo / "tracked.txt").write_text(f"{branch}\n", encoding="utf-8")
        run(repo, ["git", "commit", "-am", branch])
    run(repo, ["git", "remote", "add", "origin", remote_url])

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    if install_gh:
        install_fake_gh(bin_dir)
        monkeypatch.setenv("LCB_FAKE_GH_LOG", str(tmp_path / "gh-log.jsonl"))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    cfg = BridgeConfig(
        server=ServerConfig(task_dir=tmp_path / "tasks"),
        projects={"dummy": ProjectConfig(name="Dummy", path=repo)},
    )
    runner = TaskRunner(cfg)
    patch_origin_branch_sha(monkeypatch, runner, repo)
    return runner, repo, tmp_path / "gh-log.jsonl"


def install_fake_gh(bin_dir: Path) -> None:
    script = bin_dir / "gh"
    script.write_text(
        r'''#!/usr/bin/env python3
import json
import os
import re
import sys

args = sys.argv[1:]
log = os.environ.get("LCB_FAKE_GH_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(args) + "\n")
scenario = os.environ.get("LCB_FAKE_GH_SCENARIO", "")
default_branch = os.environ.get("LCB_FAKE_GH_DEFAULT_BRANCH", "main")
existing = os.environ.get("LCB_FAKE_GH_EXISTING_PR", "0") == "1"
repo = "coolsidsudo/local-codex-bridge"
checks = os.environ.get("LCB_FAKE_GH_CHECKS", "missing")
if checks == "passing":
    status_check_rollup = [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}]
elif checks == "failing":
    status_check_rollup = [{"name": "ci", "status": "COMPLETED", "conclusion": "FAILURE"}]
elif checks == "pending":
    status_check_rollup = [{"name": "ci", "status": "IN_PROGRESS", "conclusion": None}]
elif checks == "unknown":
    status_check_rollup = [{"name": "ci"}]
elif checks == "unparseable":
    status_check_rollup = {"unexpected": "shape"}
else:
    status_check_rollup = []
head_sha = os.environ.get("LCB_FAKE_GH_HEAD_SHA", "abc123")
review_decision = os.environ.get("LCB_FAKE_GH_REVIEW_DECISION", "")
pr = {
    "number": 123,
    "url": "https://github.com/coolsidsudo/local-codex-bridge/pull/123",
    "title": "Test PR",
    "state": os.environ.get("LCB_FAKE_GH_PR_STATE", "OPEN"),
    "isDraft": os.environ.get("LCB_FAKE_GH_PR_DRAFT", "1") == "1",
    "baseRefName": os.environ.get("LCB_FAKE_GH_BASE", default_branch),
    "headRefName": os.environ.get("LCB_FAKE_GH_HEAD_BRANCH", "feature"),
    "headRefOid": None if head_sha == "__NULL__" else head_sha,
    "mergeable": os.environ.get("LCB_FAKE_GH_MERGEABLE", "UNKNOWN"),
    "mergeStateStatus": os.environ.get("LCB_FAKE_GH_MERGE_STATE", "UNKNOWN"),
    "reviewDecision": None if review_decision == "__NULL__" else review_decision,
    "statusCheckRollup": status_check_rollup,
    "updatedAt": "2026-05-02T00:00:00Z",
}
if args == ["--version"]:
    print("gh version 2.92.0")
elif args == ["auth", "status", "-h", "github.com"]:
    if scenario == "auth_fail":
        print("not logged in", file=sys.stderr)
        sys.exit(1)
    print("github.com authenticated")
elif args[:2] == ["repo", "view"]:
    print(json.dumps({"nameWithOwner": repo, "defaultBranchRef": {"name": default_branch}}))
elif args[:2] == ["pr", "list"]:
    print(json.dumps([pr] if existing else []))
elif args[:2] == ["pr", "create"]:
    if scenario == "create_fail":
        print("create failed", file=sys.stderr)
        sys.exit(1)
    if scenario == "bad_create_url":
        print("https://evil.example.test/not-a-pr")
    else:
        print("https://github.com/coolsidsudo/local-codex-bridge/pull/123")
elif args[:2] == ["pr", "view"]:
    merged_marker = log + ".merged" if log else ""
    if scenario == "after_status_fail" and merged_marker and os.path.exists(merged_marker):
        print("after status failed", file=sys.stderr)
        sys.exit(1)
    if scenario == "json_fail":
        print("not-json")
    else:
        ref = args[2]
        match = re.search(r"/pull/(\d+)$", ref)
        number = int(match.group(1) if match else ref)
        pr["number"] = number
        pr["url"] = f"https://github.com/coolsidsudo/local-codex-bridge/pull/{number}"
        print(json.dumps(pr))
elif args[:2] == ["pr", "merge"]:
    if scenario == "merge_fail":
        print("merge failed", file=sys.stderr)
        sys.exit(1)
    if log:
        with open(log + ".merged", "w", encoding="utf-8") as handle:
            handle.write("1\n")
    print("merged")
else:
    print("unexpected args: " + json.dumps(args), file=sys.stderr)
    sys.exit(2)
''',
        encoding="utf-8",
    )
    script.chmod(0o755)


def patch_origin_branch_sha(
    monkeypatch: pytest.MonkeyPatch,
    runner: TaskRunner,
    repo: Path,
    mapping: dict[str, str | None] | None = None,
) -> None:
    if mapping is None:
        mapping = {"main": head(repo, "main"), "feature": head(repo, "HEAD")}

    def fake_origin_branch_sha(_repo: Path, branch: str) -> dict[str, Any]:
        sha = mapping.get(branch)
        stdout = "" if sha is None else f"{sha}\trefs/heads/{branch}\n"
        return {
            "status": "ok",
            "sha": sha,
            "ref": f"refs/heads/{branch}" if sha else None,
            "ls_remote": {
                "cmd": ["git", "ls-remote", "--heads", "origin", branch],
                "returncode": 0,
                "stdout": stdout,
                "stderr": "",
            },
        }

    monkeypatch.setattr(runner, "_origin_branch_sha", fake_origin_branch_sha)


def head(repo: Path, ref: str = "HEAD") -> str:
    return run(repo, ["git", "rev-parse", ref]).stdout.strip()


def gh_log(log_path: Path) -> list[list[str]]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def set_ready_pr_env(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    monkeypatch.setenv("LCB_FAKE_GH_PR_DRAFT", "0")
    monkeypatch.setenv("LCB_FAKE_GH_MERGEABLE", "MERGEABLE")
    monkeypatch.setenv("LCB_FAKE_GH_MERGE_STATE", "CLEAN")
    monkeypatch.setenv("LCB_FAKE_GH_REVIEW_DECISION", "APPROVED")
    monkeypatch.setenv("LCB_FAKE_GH_CHECKS", "passing")
    monkeypatch.setenv("LCB_FAKE_GH_HEAD_SHA", head(repo))


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


def test_dirty_worktree_blocks_create(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, repo, _ = make_runner(tmp_path, monkeypatch)
    (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    result = runner.github_create_pr("dummy", "Title", "Body")

    assert result["status"] == "blocked_dirty"


def test_detached_head_blocks_create(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, repo, _ = make_runner(tmp_path, monkeypatch)
    run(repo, ["git", "checkout", "--detach", "HEAD"])

    result = runner.github_create_pr("dummy", "Title", "Body")

    assert result["status"] == "blocked_detached_head"


def test_non_github_origin_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, _, _ = make_runner(
        tmp_path,
        monkeypatch,
        remote_url="https://example.com/owner/repo.git",
    )

    result = runner.github_create_pr("dummy", "Title", "Body")

    assert result["status"] == "blocked_remote"


def test_https_and_ssh_github_origin_parsing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    https_runner, https_repo, _ = make_runner(tmp_path / "https", monkeypatch, remote_url=HTTPS_REMOTE)
    ssh_runner, ssh_repo, _ = make_runner(tmp_path / "ssh", monkeypatch, remote_url=SSH_REMOTE)

    assert https_runner._github_origin_info(https_repo)["repo_arg"] == REPO_ARG
    assert ssh_runner._github_origin_info(ssh_repo)["repo_arg"] == REPO_ARG


def test_missing_gh_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, _, _ = make_runner(tmp_path, monkeypatch)

    original_run = runner._run

    def fake_run(cwd: Path, cmd: list[str], timeout: int, max_chars: int = 20000) -> dict[str, Any]:
        if cmd == ["gh", "--version"]:
            return {"cmd": cmd, "returncode": 127, "stdout": "", "stderr": "missing"}
        return original_run(cwd, cmd, timeout, max_chars)

    monkeypatch.setattr(runner, "_run", fake_run)

    result = runner.github_create_pr("dummy", "Title", "Body")

    assert result["status"] == "blocked_gh"


def test_gh_auth_failure_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, _, _ = make_runner(tmp_path, monkeypatch)
    monkeypatch.setenv("LCB_FAKE_GH_SCENARIO", "auth_fail")

    result = runner.github_create_pr("dummy", "Title", "Body")

    assert result["status"] == "blocked_gh_auth"


def test_unpublished_branch_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, repo, _ = make_runner(tmp_path, monkeypatch)
    patch_origin_branch_sha(monkeypatch, runner, repo, {"main": head(repo, "main"), "feature": None})

    result = runner.github_create_pr("dummy", "Title", "Body")

    assert result["status"] == "blocked_unpublished"


def test_remote_branch_sha_mismatch_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, repo, _ = make_runner(tmp_path, monkeypatch)
    patch_origin_branch_sha(monkeypatch, runner, repo, {"main": head(repo, "main"), "feature": "0" * 40})

    result = runner.github_create_pr("dummy", "Title", "Body")

    assert result["status"] == "blocked_remote_mismatch"


def test_default_branch_source_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, _, _ = make_runner(tmp_path, monkeypatch, branch="main")

    result = runner.github_create_pr("dummy", "Title", "Body")

    assert result["status"] == "blocked_default_branch"


def test_current_branch_same_as_base_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, _, _ = make_runner(tmp_path, monkeypatch)

    result = runner.github_create_pr("dummy", "Title", "Body", base_branch="feature")

    assert result["status"] == "blocked_branch"


@pytest.mark.parametrize("branch", ["origin/topic", "upstream/topic"])
def test_remote_style_current_branch_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
) -> None:
    runner, _, _ = make_runner(tmp_path, monkeypatch, branch=branch)

    result = runner.github_create_pr("dummy", "Title", "Body")

    assert result["status"] == "blocked_input"
    assert result["error"] == "current branch must not be remote-style"


@pytest.mark.parametrize("base_branch", ["origin/main", "upstream/main"])
def test_remote_style_explicit_base_branch_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    base_branch: str,
) -> None:
    runner, _, _ = make_runner(tmp_path, monkeypatch)

    result = runner.github_create_pr("dummy", "Title", "Body", base_branch=base_branch)

    assert result["status"] == "blocked_input"
    assert result["error"] == "base_branch must not be remote-style"


def test_normal_slash_branch_and_explicit_base_are_allowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, _ = make_runner(tmp_path, monkeypatch, branch="codex/topic")
    patch_origin_branch_sha(
        monkeypatch,
        runner,
        repo,
        {
            "develop": head(repo, "main"),
            "codex/topic": head(repo),
        },
    )

    result = runner.github_create_pr("dummy", "Title", "Body", base_branch="develop")

    assert result["status"] == "created"
    assert result["base_branch"] == "develop"


def test_omitted_base_uses_gh_repo_default_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, repo, log = make_runner(tmp_path, monkeypatch)
    monkeypatch.setenv("LCB_FAKE_GH_DEFAULT_BRANCH", "develop")
    patch_origin_branch_sha(monkeypatch, runner, repo, {"develop": head(repo, "main"), "feature": head(repo)})

    result = runner.github_create_pr("dummy", "Title", "Body")

    assert result["status"] == "created"
    create_calls = [args for args in gh_log(log) if args[:2] == ["pr", "create"]]
    assert create_calls[0][create_calls[0].index("--base") + 1] == "develop"


def test_explicit_base_validation_and_origin_existence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, _ = make_runner(tmp_path, monkeypatch)

    invalid = runner.github_create_pr("dummy", "Title", "Body", base_branch="bad branch")
    assert invalid["status"] == "blocked_input"

    patch_origin_branch_sha(monkeypatch, runner, repo, {"main": head(repo, "main"), "feature": head(repo), "develop": None})
    missing = runner.github_create_pr("dummy", "Title", "Body", base_branch="develop")
    assert missing["status"] == "blocked_base_branch"


def test_existing_pr_returns_evidence_without_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, _, log = make_runner(tmp_path, monkeypatch)
    monkeypatch.setenv("LCB_FAKE_GH_EXISTING_PR", "1")

    result = runner.github_create_pr("dummy", "Title", "Body")

    assert result["status"] == "existing_pr"
    assert result["pr"]["number"] == 123
    assert not [args for args in gh_log(log) if args[:2] == ["pr", "create"]]


def test_draft_and_non_draft_create_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, _, log = make_runner(tmp_path / "draft", monkeypatch)
    draft_result = runner.github_create_pr("dummy", "Title", "Body", draft=True)
    assert draft_result["status"] == "created"
    draft_call = [args for args in gh_log(log) if args[:2] == ["pr", "create"]][0]
    assert "--draft" in draft_call

    runner2, _, log2 = make_runner(tmp_path / "ready", monkeypatch)
    ready_result = runner2.github_create_pr("dummy", "Title", "Body", draft=False)
    assert ready_result["status"] == "created"
    ready_call = [args for args in gh_log(log2) if args[:2] == ["pr", "create"]][0]
    assert "--draft" not in ready_call


def test_create_evidence_redacts_title_body_and_bounds_args(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, _, _ = make_runner(tmp_path, monkeypatch)
    long_title = "T" * 5000
    long_body = "B" * 5000

    result = runner.github_create_pr("dummy", long_title, long_body)

    assert result["status"] == "created"
    cmd = result["create"]["cmd"]
    assert cmd[:3] == ["gh", "pr", "create"]
    assert cmd[cmd.index("--title") + 1] == "<redacted-pr-title>"
    assert cmd[cmd.index("--body") + 1] == "<redacted-pr-body>"
    assert long_title not in cmd
    assert long_body not in cmd
    assert max(len(arg) for arg in cmd) <= 300


def test_invalid_create_url_returns_blocked_gh_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, _, _ = make_runner(tmp_path, monkeypatch)
    monkeypatch.setenv("LCB_FAKE_GH_SCENARIO", "bad_create_url")

    result = runner.github_create_pr("dummy", "Title", "Body")

    assert result["status"] == "blocked_gh_output"


@pytest.mark.parametrize("reference", [123, "https://github.com/coolsidsudo/local-codex-bridge/pull/123"])
def test_pr_status_by_number_and_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reference: int | str,
) -> None:
    runner, _, _ = make_runner(tmp_path, monkeypatch)

    result = runner.github_get_pr_status("dummy", reference)

    assert result["status"] == "ok"
    assert result["pr"]["number"] == 123
    assert result["pr"]["url"] == "https://github.com/coolsidsudo/local-codex-bridge/pull/123"


def test_pr_status_by_current_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, _, _ = make_runner(tmp_path, monkeypatch)
    monkeypatch.setenv("LCB_FAKE_GH_EXISTING_PR", "1")

    result = runner.github_get_pr_status("dummy")

    assert result["status"] == "ok"
    assert result["pr"]["headRefName"] == "feature"


def test_pr_status_readiness_ready_pr_with_matching_local_branch_and_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, _ = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)

    result = runner.github_get_pr_status("dummy", 123)

    readiness = result["pr_readiness"]
    assert result["status"] == "ok"
    assert readiness["status"] == "ok"
    assert readiness["ready_to_consider_merge"] is True
    assert readiness["check_summary"]["status"] == "passing"
    assert readiness["local_branch_matches_pr_head"] is True
    assert readiness["local_head_matches_pr_head_sha"] is True
    assert readiness["blocking_reasons"] == []


@pytest.mark.parametrize(
    ("env_name", "env_value"),
    [
        ("LCB_FAKE_GH_PR_DRAFT", "1"),
        ("LCB_FAKE_GH_PR_STATE", "CLOSED"),
        ("LCB_FAKE_GH_MERGEABLE", "CONFLICTING"),
        ("LCB_FAKE_GH_MERGEABLE", "UNKNOWN"),
        ("LCB_FAKE_GH_MERGE_STATE", "BLOCKED"),
        ("LCB_FAKE_GH_MERGE_STATE", "UNKNOWN"),
        ("LCB_FAKE_GH_CHECKS", "failing"),
        ("LCB_FAKE_GH_CHECKS", "pending"),
        ("LCB_FAKE_GH_REVIEW_DECISION", "CHANGES_REQUESTED"),
    ],
)
def test_pr_status_readiness_pr_blockers_are_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    env_value: str,
) -> None:
    runner, repo, _ = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)
    monkeypatch.setenv(env_name, env_value)

    result = runner.github_get_pr_status("dummy", 123)

    assert result["status"] == "ok"
    assert result["pr_readiness"]["ready_to_consider_merge"] is False
    assert result["pr_readiness"]["blocking_reasons"]


@pytest.mark.parametrize("checks", ["missing", "unknown"])
def test_pr_status_readiness_missing_or_unknown_checks_not_ready_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checks: str,
) -> None:
    runner, repo, _ = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)
    monkeypatch.setenv("LCB_FAKE_GH_CHECKS", checks)

    result = runner.github_get_pr_status("dummy", 123)

    readiness = result["pr_readiness"]
    assert readiness["check_summary"]["status"] == checks
    assert readiness["ready_to_consider_merge"] is False
    assert any("check evidence" in warning for warning in readiness["warnings"])


def test_pr_status_readiness_missing_review_decision_not_ready_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, _ = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)
    monkeypatch.setenv("LCB_FAKE_GH_REVIEW_DECISION", "")

    result = runner.github_get_pr_status("dummy", 123)

    readiness = result["pr_readiness"]
    assert readiness["review_decision"] == ""
    assert readiness["ready_to_consider_merge"] is False
    assert any("review-decision evidence" in warning for warning in readiness["warnings"])


def test_pr_status_readiness_merged_pr_suppresses_open_pr_merge_blockers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, _ = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)
    monkeypatch.setenv("LCB_FAKE_GH_PR_STATE", "MERGED")
    monkeypatch.setenv("LCB_FAKE_GH_PR_DRAFT", "1")
    monkeypatch.setenv("LCB_FAKE_GH_MERGEABLE", "UNKNOWN")
    monkeypatch.setenv("LCB_FAKE_GH_MERGE_STATE", "BLOCKED")
    monkeypatch.setenv("LCB_FAKE_GH_REVIEW_DECISION", "")
    monkeypatch.setenv("LCB_FAKE_GH_CHECKS", "missing")
    monkeypatch.setenv("LCB_FAKE_GH_HEAD_BRANCH", "other")
    monkeypatch.setenv("LCB_FAKE_GH_HEAD_SHA", "0" * 40)

    result = runner.github_get_pr_status("dummy", 123)

    readiness = result["pr_readiness"]
    assert result["status"] == "ok"
    assert readiness["status"] == "ok"
    assert readiness["ready_to_consider_merge"] is False
    assert readiness["merge_readiness_applicable"] is False
    assert readiness["pr_lifecycle_state"] == "merged"
    assert readiness["post_merge_note"] == (
        "PR is already merged; merge readiness checks are not applicable. "
        "Use local target sync readiness instead."
    )
    assert readiness["state"] == "MERGED"
    assert readiness["number"] == 123
    assert readiness["url"] == "https://github.com/coolsidsudo/local-codex-bridge/pull/123"
    assert readiness["check_summary"]["status"] == "missing"
    assert readiness["local_branch_matches_pr_head"] is False
    assert readiness["local_head_matches_pr_head_sha"] is False
    assert readiness["blocking_reasons"] == []
    assert readiness["post_merge_note"] in readiness["warnings"]


def test_pr_status_readiness_open_pr_conservative_blockers_remain_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, _ = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)
    monkeypatch.setenv("LCB_FAKE_GH_MERGEABLE", "UNKNOWN")
    monkeypatch.setenv("LCB_FAKE_GH_MERGE_STATE", "BLOCKED")
    monkeypatch.setenv("LCB_FAKE_GH_REVIEW_DECISION", "")
    monkeypatch.setenv("LCB_FAKE_GH_CHECKS", "missing")
    monkeypatch.setenv("LCB_FAKE_GH_HEAD_BRANCH", "other")
    monkeypatch.setenv("LCB_FAKE_GH_HEAD_SHA", "0" * 40)

    result = runner.github_get_pr_status("dummy", 123)

    blockers = result["pr_readiness"]["blocking_reasons"]
    assert result["pr_readiness"]["state"] == "OPEN"
    assert result["pr_readiness"]["ready_to_consider_merge"] is False
    assert result["pr_readiness"]["merge_readiness_applicable"] is True
    assert "PR mergeability is not confirmed mergeable" in blockers
    assert "PR merge state is not clean" in blockers
    assert "PR checks are not confirmed passing" in blockers
    assert "PR review decision is missing or unknown" in blockers
    assert "Local current branch does not match PR head branch" in blockers
    assert "Local HEAD does not match PR head SHA" in blockers


def test_pr_status_readiness_dirty_worktree_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, _ = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)
    (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    result = runner.github_get_pr_status("dummy", 123)

    assert result["pr_readiness"]["ready_to_consider_merge"] is False
    assert "Local worktree is dirty" in result["pr_readiness"]["blocking_reasons"]


def test_pr_status_readiness_local_branch_mismatch_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, _ = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)
    monkeypatch.setenv("LCB_FAKE_GH_HEAD_BRANCH", "other")

    result = runner.github_get_pr_status("dummy", 123)

    assert result["pr_readiness"]["local_branch_matches_pr_head"] is False
    assert result["pr_readiness"]["ready_to_consider_merge"] is False


def test_pr_status_readiness_local_head_mismatch_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, _ = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)
    monkeypatch.setenv("LCB_FAKE_GH_HEAD_SHA", "0" * 40)

    result = runner.github_get_pr_status("dummy", 123)

    assert result["pr_readiness"]["local_head_matches_pr_head_sha"] is False
    assert result["pr_readiness"]["ready_to_consider_merge"] is False


def test_pr_status_no_pr_found_includes_not_ready_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, _ = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)

    result = runner.github_get_pr_status("dummy")

    assert result["status"] == "no_pr"
    assert result["pr_readiness"]["status"] == "no_pr"
    assert result["pr_readiness"]["ready_to_consider_merge"] is False
    assert "No open PR found for current branch" in result["pr_readiness"]["blocking_reasons"]


def test_json_parse_failure_returns_blocked_gh_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, _, _ = make_runner(tmp_path, monkeypatch)
    monkeypatch.setenv("LCB_FAKE_GH_SCENARIO", "json_fail")

    result = runner.github_get_pr_status("dummy", 123)

    assert result["status"] == "blocked_gh_output"
    assert result["pr_readiness"]["status"] == "blocked_gh_output"
    assert result["pr_readiness"]["ready_to_consider_merge"] is False


def test_pr_status_gh_failure_includes_not_ready_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, _, _ = make_runner(tmp_path, monkeypatch)
    monkeypatch.setenv("LCB_FAKE_GH_SCENARIO", "auth_fail")

    result = runner.github_get_pr_status("dummy", 123)

    assert result["status"] == "blocked_gh_auth"
    assert result["pr_readiness"]["status"] == "blocked_gh_auth"
    assert result["pr_readiness"]["ready_to_consider_merge"] is False


def merge_commands(log_path: Path) -> list[list[str]]:
    return [args for args in gh_log(log_path) if args[:2] == ["pr", "merge"]]


def assert_safe_merge_argv(args: list[str]) -> None:
    assert "--admin" not in args
    assert "--auto" not in args
    assert "--repo" not in args


def test_github_merge_pr_unknown_project_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, _, log_path = make_runner(tmp_path, monkeypatch)

    result = runner.github_merge_pr("missing", 123)

    assert result["status"] == "blocked_input"
    assert result["merge_command_executed"] is False
    assert result["mutation_performed"] is False
    assert merge_commands(log_path) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"pr_url_or_number": None},
        {"pr_url_or_number": ""},
        {"pr_url_or_number": "   "},
        {"pr_url_or_number": 0},
        {"pr_url_or_number": True},
        {"pr_url_or_number": 123, "merge_method": "fast-forward"},
        {"pr_url_or_number": 123, "delete_branch": "false"},
        {"pr_url_or_number": 123, "expected_head_sha": "abc123"},
        {"pr_url_or_number": 123, "expected_head_sha": object()},
    ],
)
def test_github_merge_pr_invalid_inputs_block_before_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, Any],
) -> None:
    runner, _, log_path = make_runner(tmp_path, monkeypatch)

    result = runner.github_merge_pr("dummy", **kwargs)

    assert result["status"] == "blocked_input"
    assert result["merge_command_executed"] is False
    assert result["mutation_performed"] is False
    assert merge_commands(log_path) == []


@pytest.mark.parametrize(
    ("env_name", "env_value"),
    [
        ("LCB_FAKE_GH_PR_STATE", "CLOSED"),
        ("LCB_FAKE_GH_PR_STATE", "MERGED"),
        ("LCB_FAKE_GH_PR_DRAFT", "1"),
        ("LCB_FAKE_GH_HEAD_SHA", "__NULL__"),
        ("LCB_FAKE_GH_HEAD_SHA", "abc123"),
        ("LCB_FAKE_GH_REVIEW_DECISION", "CHANGES_REQUESTED"),
        ("LCB_FAKE_GH_REVIEW_DECISION", "REVIEW_REQUIRED"),
        ("LCB_FAKE_GH_REVIEW_DECISION", ""),
        ("LCB_FAKE_GH_REVIEW_DECISION", "__NULL__"),
        ("LCB_FAKE_GH_REVIEW_DECISION", "UNKNOWN"),
        ("LCB_FAKE_GH_CHECKS", "failing"),
        ("LCB_FAKE_GH_CHECKS", "pending"),
        ("LCB_FAKE_GH_CHECKS", "missing"),
        ("LCB_FAKE_GH_CHECKS", "unparseable"),
        ("LCB_FAKE_GH_MERGEABLE", "CONFLICTING"),
        ("LCB_FAKE_GH_MERGEABLE", "UNKNOWN"),
        ("LCB_FAKE_GH_MERGE_STATE", "BLOCKED"),
        ("LCB_FAKE_GH_MERGE_STATE", "UNKNOWN"),
    ],
)
def test_github_merge_pr_readiness_blockers_do_not_run_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    env_value: str,
) -> None:
    runner, repo, log_path = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)
    monkeypatch.setenv(env_name, env_value)

    result = runner.github_merge_pr("dummy", 123)

    assert result["status"] == "blocked_readiness"
    assert result["ready"] is False
    assert result["blocking_reasons"]
    assert result["merge_command_executed"] is False
    assert result["mutation_performed"] is False
    assert merge_commands(log_path) == []


def test_github_merge_pr_expected_head_sha_mismatch_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, log_path = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)

    result = runner.github_merge_pr("dummy", 123, expected_head_sha="0" * 40)

    assert result["status"] == "blocked_readiness"
    assert "expected_head_sha does not match fresh PR head SHA" in result["blocking_reasons"]
    assert result["merge_command_executed"] is False
    assert merge_commands(log_path) == []


def test_github_merge_pr_dirty_worktree_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, repo, log_path = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)
    (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    result = runner.github_merge_pr("dummy", 123)

    assert result["status"] == "blocked_readiness"
    assert "Local worktree is dirty" in result["blocking_reasons"]
    assert merge_commands(log_path) == []


def test_github_merge_pr_detached_repo_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, repo, log_path = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)
    run(repo, ["git", "checkout", "--detach", "HEAD"])

    result = runner.github_merge_pr("dummy", 123)

    assert result["status"] == "blocked_readiness"
    assert result["merge_command_executed"] is False
    assert merge_commands(log_path) == []


def test_github_merge_pr_local_branch_mismatch_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, log_path = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)
    monkeypatch.setenv("LCB_FAKE_GH_HEAD_BRANCH", "other")

    result = runner.github_merge_pr("dummy", 123)

    assert result["status"] == "blocked_readiness"
    assert "Local current branch does not match PR head branch" in result["blocking_reasons"]
    assert merge_commands(log_path) == []


def test_github_merge_pr_local_head_mismatch_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, log_path = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)
    monkeypatch.setenv("LCB_FAKE_GH_HEAD_SHA", "0" * 40)

    result = runner.github_merge_pr("dummy", 123)

    assert result["status"] == "blocked_readiness"
    assert "Local HEAD does not match PR head SHA" in result["blocking_reasons"]
    assert merge_commands(log_path) == []


def test_github_merge_pr_default_squash_uses_fixed_match_head_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, log_path = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)
    fresh_head = head(repo)

    result = runner.github_merge_pr("dummy", 123, expected_head_sha=f" {fresh_head} ")

    assert result["status"] == "ok_merged"
    assert result["merge_command_executed"] is True
    assert result["mutation_performed"] is True
    assert result["matched_head_sha"] == fresh_head
    commands = merge_commands(log_path)
    assert commands == [["pr", "merge", "123", "--squash", "--match-head-commit", fresh_head]]
    assert "--delete-branch" not in commands[0]
    assert_safe_merge_argv(commands[0])


def test_github_merge_pr_delete_branch_uses_only_gh_delete_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, log_path = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)

    result = runner.github_merge_pr("dummy", 123, delete_branch=True)

    assert result["status"] == "ok_merged"
    commands = merge_commands(log_path)
    assert commands == [
        ["pr", "merge", "123", "--squash", "--match-head-commit", head(repo), "--delete-branch"]
    ]
    assert_safe_merge_argv(commands[0])


@pytest.mark.parametrize(("method", "flag"), [("merge", "--merge"), ("rebase", "--rebase")])
def test_github_merge_pr_method_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    flag: str,
) -> None:
    runner, repo, log_path = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)

    result = runner.github_merge_pr("dummy", 123, merge_method=method)

    assert result["status"] == "ok_merged"
    commands = merge_commands(log_path)
    assert commands[0][3] == flag
    assert_safe_merge_argv(commands[0])


def test_github_merge_pr_merge_failure_reports_command_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, log_path = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)
    monkeypatch.setenv("LCB_FAKE_GH_SCENARIO", "merge_fail")

    result = runner.github_merge_pr("dummy", 123)

    assert result["status"] == "failed_merge"
    assert result["merge_command_executed"] is True
    assert result["mutation_performed"] is False
    assert result["mutation_state"] == "unknown_after_failed_merge_command"
    assert result["limits"]["mutation_performed"] is False
    assert result["limits"]["mutation_state"] == "unknown_after_failed_merge_command"
    assert result["merge_command_evidence"]["returncode"] == 1
    assert merge_commands(log_path)
    assert any("check PR status" in warning for warning in result["warnings"])


def test_github_merge_pr_after_status_failure_preserves_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, log_path = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)
    monkeypatch.setenv("LCB_FAKE_GH_SCENARIO", "after_status_fail")

    result = runner.github_merge_pr("dummy", 123)

    assert result["status"] == "ok_merged"
    assert result["mutation_performed"] is True
    assert result["after_evidence_error"]
    assert any("after-status evidence" in warning for warning in result["warnings"])
    assert len(merge_commands(log_path)) == 1


def test_pr_sync_readiness_ready_pr_with_matching_local_branch_and_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, _ = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)

    result = runner.get_pr_sync_readiness("dummy", 123)

    assert result["status"] == "ok"
    assert result["ready_to_consider_merge"] is True
    assert result["pr_readiness"]["ready_to_consider_merge"] is True
    assert result["pr_readiness"]["check_summary"]["status"] == "passing"
    assert result["pr_readiness"]["local_branch_matches_pr_head"] is True
    assert result["pr_readiness"]["local_head_matches_pr_head_sha"] is True


@pytest.mark.parametrize(
    ("env_name", "env_value"),
    [
        ("LCB_FAKE_GH_PR_DRAFT", "1"),
        ("LCB_FAKE_GH_PR_STATE", "CLOSED"),
        ("LCB_FAKE_GH_MERGEABLE", "CONFLICTING"),
        ("LCB_FAKE_GH_MERGE_STATE", "BLOCKED"),
        ("LCB_FAKE_GH_CHECKS", "failing"),
        ("LCB_FAKE_GH_CHECKS", "pending"),
        ("LCB_FAKE_GH_REVIEW_DECISION", "CHANGES_REQUESTED"),
    ],
)
def test_pr_sync_readiness_pr_blockers_are_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    env_value: str,
) -> None:
    runner, repo, _ = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)
    monkeypatch.setenv(env_name, env_value)

    result = runner.get_pr_sync_readiness("dummy", 123)

    assert result["status"] == "ok"
    assert result["pr_readiness"]["ready_to_consider_merge"] is False
    assert result["pr_readiness"]["blocking_reasons"]


@pytest.mark.parametrize("checks", ["missing", "unknown"])
def test_pr_sync_readiness_missing_or_unknown_checks_not_ready_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checks: str,
) -> None:
    runner, repo, _ = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)
    monkeypatch.setenv("LCB_FAKE_GH_CHECKS", checks)

    result = runner.get_pr_sync_readiness("dummy", 123)

    assert result["pr_readiness"]["check_summary"]["status"] == checks
    assert result["pr_readiness"]["ready_to_consider_merge"] is False
    assert any("check evidence" in warning for warning in result["pr_readiness"]["warnings"])


def test_pr_sync_readiness_merged_pr_can_be_sync_ready_without_merge_blocker_noise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, _ = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)
    run(repo, ["git", "update-ref", "refs/remotes/origin/main", head(repo, "main")])
    monkeypatch.setenv("LCB_FAKE_GH_PR_STATE", "MERGED")
    monkeypatch.setenv("LCB_FAKE_GH_MERGEABLE", "UNKNOWN")
    monkeypatch.setenv("LCB_FAKE_GH_MERGE_STATE", "BLOCKED")
    monkeypatch.setenv("LCB_FAKE_GH_REVIEW_DECISION", "")
    monkeypatch.setenv("LCB_FAKE_GH_HEAD_BRANCH", "other")
    monkeypatch.setenv("LCB_FAKE_GH_HEAD_SHA", "0" * 40)

    result = runner.get_pr_sync_readiness("dummy", 123, target_branch="main")

    blockers = result["pr_readiness"]["blocking_reasons"]
    assert result["status"] == "ok"
    assert result["ready_to_consider_merge"] is False
    assert result["ready_to_sync_local_target"] is True
    assert result["pr_readiness"]["ready_to_consider_merge"] is False
    assert result["pr_readiness"]["merge_readiness_applicable"] is False
    assert result["pr_readiness"]["pr_lifecycle_state"] == "merged"
    assert result["pr_readiness"]["post_merge_note"] in result["pr_readiness"]["warnings"]
    assert result["local_sync_readiness"]["ready_to_sync_local_target"] is True
    assert result["local_sync_readiness"]["relation"] == "equal"
    assert blockers == []


def test_pr_sync_readiness_dirty_worktree_blocks_pr_and_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, _ = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)
    (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    result = runner.get_pr_sync_readiness("dummy", 123)

    assert result["pr_readiness"]["ready_to_consider_merge"] is False
    assert "Local worktree is dirty" in result["pr_readiness"]["blocking_reasons"]
    assert result["local_sync_readiness"]["ready_to_sync_local_target"] is False
    assert "Local worktree is dirty" in result["local_sync_readiness"]["blocking_reasons"]


def test_pr_sync_readiness_local_branch_mismatch_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, _ = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)
    monkeypatch.setenv("LCB_FAKE_GH_HEAD_BRANCH", "other")

    result = runner.get_pr_sync_readiness("dummy", 123)

    assert result["pr_readiness"]["local_branch_matches_pr_head"] is False
    assert result["pr_readiness"]["ready_to_consider_merge"] is False


def test_pr_sync_readiness_local_head_mismatch_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, _ = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)
    monkeypatch.setenv("LCB_FAKE_GH_HEAD_SHA", "0" * 40)

    result = runner.get_pr_sync_readiness("dummy", 123)

    assert result["pr_readiness"]["local_head_matches_pr_head_sha"] is False
    assert result["pr_readiness"]["ready_to_consider_merge"] is False


def test_pr_sync_readiness_no_pr_found_returns_structured_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, _ = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)

    result = runner.get_pr_sync_readiness("dummy")

    assert result["status"] == "ok"
    assert result["pr_readiness"]["status"] == "no_pr"
    assert result["pr_readiness"]["ready_to_consider_merge"] is False


def test_pr_sync_readiness_gh_failure_is_structured_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, _ = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)
    monkeypatch.setenv("LCB_FAKE_GH_SCENARIO", "json_fail")

    result = runner.get_pr_sync_readiness("dummy", 123)

    assert result["status"] == "ok"
    assert result["pr_readiness"]["status"] == "blocked_gh_output"
    assert result["pr_readiness"]["ready_to_consider_merge"] is False


def test_pr_sync_readiness_git_failure_is_structured_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, repo, _ = make_runner(tmp_path, monkeypatch)
    set_ready_pr_env(monkeypatch, repo)
    original_run = runner._run

    def fake_run(cwd: Path, cmd: list[str], timeout: int, max_chars: int = 20000) -> dict[str, Any]:
        if cmd == ["git", "status", "--short", "--branch"]:
            return {"cmd": cmd, "returncode": 1, "stdout": "", "stderr": "forced failure"}
        return original_run(cwd, cmd, timeout, max_chars)

    monkeypatch.setattr(runner, "_run", fake_run)

    result = runner.get_pr_sync_readiness("dummy", 123)

    assert result["status"] == "ok"
    assert result["pr_readiness"]["status"] == "blocked_git"
    assert result["local_sync_readiness"]["status"] == "blocked_git"


def test_pr_sync_readiness_clean_target_equal_origin_target_sync_ready(tmp_path: Path) -> None:
    runner, _, _ = make_sync_runner(tmp_path)

    result = runner.get_pr_sync_readiness("dummy", target_branch="main")

    sync = result["local_sync_readiness"]
    assert sync["ready_to_sync_local_target"] is True
    assert sync["relation"] == "equal"
    assert sync["local_target_has_unique_commits"] is False
    assert any("No sync appears necessary" in warning for warning in sync["warnings"])
    assert sync["suggested_operator_commands"]


def test_pr_sync_readiness_clean_target_behind_origin_target_sync_ready(tmp_path: Path) -> None:
    runner, repo, initial = make_sync_runner(tmp_path)
    remote_only = make_commit_object(repo, initial, "remote only")
    run(repo, ["git", "update-ref", "refs/remotes/origin/main", remote_only])

    result = runner.get_pr_sync_readiness("dummy", target_branch="main")

    sync = result["local_sync_readiness"]
    assert sync["ready_to_sync_local_target"] is True
    assert sync["relation"] == "behind"
    assert sync["local_target_has_unique_commits"] is False
    assert sync["remote_target_has_unique_commits"] is True


def test_pr_sync_readiness_local_target_ahead_not_sync_ready(tmp_path: Path) -> None:
    runner, repo, _ = make_sync_runner(tmp_path)
    (repo / "tracked.txt").write_text("local\n", encoding="utf-8")
    run(repo, ["git", "commit", "-am", "local only"])

    result = runner.get_pr_sync_readiness("dummy", target_branch="main")

    sync = result["local_sync_readiness"]
    assert sync["ready_to_sync_local_target"] is False
    assert sync["relation"] == "ahead"
    assert sync["local_target_has_unique_commits"] is True


def test_pr_sync_readiness_local_target_diverged_not_sync_ready(tmp_path: Path) -> None:
    runner, repo, initial = make_sync_runner(tmp_path)
    remote_only = make_commit_object(repo, initial, "remote only")
    run(repo, ["git", "update-ref", "refs/remotes/origin/main", remote_only])
    (repo / "tracked.txt").write_text("local\n", encoding="utf-8")
    run(repo, ["git", "commit", "-am", "local only"])

    result = runner.get_pr_sync_readiness("dummy", target_branch="main")

    sync = result["local_sync_readiness"]
    assert sync["ready_to_sync_local_target"] is False
    assert sync["relation"] == "diverged"
    assert sync["local_target_has_unique_commits"] is True
    assert sync["remote_target_has_unique_commits"] is True


def test_pr_sync_readiness_detached_head_not_sync_ready(tmp_path: Path) -> None:
    runner, repo, _ = make_sync_runner(tmp_path)
    run(repo, ["git", "checkout", "--detach", "HEAD"])

    result = runner.get_pr_sync_readiness("dummy", target_branch="main")

    sync = result["local_sync_readiness"]
    assert sync["ready_to_sync_local_target"] is False
    assert "Local repository is detached or current branch is unknown" in sync["blocking_reasons"]


def test_pr_sync_readiness_missing_origin_remote_not_sync_ready(tmp_path: Path) -> None:
    runner, repo, _ = make_sync_runner(tmp_path)
    run(repo, ["git", "remote", "remove", "origin"])

    result = runner.get_pr_sync_readiness("dummy", target_branch="main")

    sync = result["local_sync_readiness"]
    assert sync["ready_to_sync_local_target"] is False
    assert "origin remote is not configured" in sync["blocking_reasons"]


def test_pr_sync_readiness_missing_origin_target_ref_not_sync_ready(tmp_path: Path) -> None:
    runner, repo, _ = make_sync_runner(tmp_path)
    run(repo, ["git", "update-ref", "-d", "refs/remotes/origin/main"])

    result = runner.get_pr_sync_readiness("dummy", target_branch="main")

    sync = result["local_sync_readiness"]
    assert sync["ready_to_sync_local_target"] is False
    assert "Local origin target ref does not exist" in sync["blocking_reasons"]


@pytest.mark.parametrize(
    ("kwargs", "error_text"),
    [
        ({"remote": "upstream"}, "origin"),
        ({"target_branch": "origin/main"}, "remote-style"),
        ({"target_branch": "HEAD"}, "HEAD"),
    ],
)
def test_pr_sync_readiness_invalid_remote_or_target_branch_blocked(
    tmp_path: Path,
    kwargs: dict[str, str],
    error_text: str,
) -> None:
    runner, _, _ = make_sync_runner(tmp_path)

    result = runner.get_pr_sync_readiness("dummy", **kwargs)

    assert result["status"] == "blocked_input"
    assert error_text in result["error"]


def test_pr_sync_readiness_unknown_project_blocked(tmp_path: Path) -> None:
    runner, _, _ = make_sync_runner(tmp_path)

    result = runner.get_pr_sync_readiness("missing")

    assert result["status"] == "blocked_input"
    assert result["ready_to_consider_merge"] is False
    assert result["ready_to_sync_local_target"] is False
