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
pr = {
    "number": 123,
    "url": "https://github.com/coolsidsudo/local-codex-bridge/pull/123",
    "title": "Test PR",
    "state": "OPEN",
    "isDraft": True,
    "baseRefName": default_branch,
    "headRefName": "feature",
    "headRefOid": "abc123",
    "mergeable": "UNKNOWN",
    "mergeStateStatus": "UNKNOWN",
    "reviewDecision": "",
    "statusCheckRollup": [],
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
    if scenario == "json_fail":
        print("not-json")
    else:
        ref = args[2]
        match = re.search(r"/pull/(\d+)$", ref)
        number = int(match.group(1) if match else ref)
        pr["number"] = number
        pr["url"] = f"https://github.com/coolsidsudo/local-codex-bridge/pull/{number}"
        print(json.dumps(pr))
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


def test_json_parse_failure_returns_blocked_gh_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, _, _ = make_runner(tmp_path, monkeypatch)
    monkeypatch.setenv("LCB_FAKE_GH_SCENARIO", "json_fail")

    result = runner.github_get_pr_status("dummy", 123)

    assert result["status"] == "blocked_gh_output"
