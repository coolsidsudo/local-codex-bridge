from __future__ import annotations

import json
import logging
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


def make_runner(tmp_path: Path) -> TaskRunner:
    repo = tmp_path / "project"
    remote = tmp_path / "origin.git"
    run(tmp_path, ["git", "init", "--bare", str(remote)])
    repo.mkdir()
    run(repo, ["git", "init", "-b", "main"])
    run(repo, ["git", "config", "user.email", "test@example.invalid"])
    run(repo, ["git", "config", "user.name", "Test User"])
    (repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
    run(repo, ["git", "add", "tracked.txt"])
    run(repo, ["git", "commit", "-m", "initial"])
    run(repo, ["git", "remote", "add", "origin", str(remote)])
    run(repo, ["git", "push", "-u", "origin", "main"])

    cfg = BridgeConfig(
        server=ServerConfig(task_dir=tmp_path / "tasks"),
        projects={"dummy": ProjectConfig(name="Dummy", path=repo)},
    )
    return TaskRunner(cfg)


def mutation_records(caplog):
    return [
        record
        for record in caplog.records
        if getattr(record, "lcb_diagnostic_event", None) == "approval_card_mutation"
    ]


def diagnostic_events(runner: TaskRunner) -> list[dict[str, object]]:
    path = runner.config.server.task_dir / "mutation_diagnostics.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def assert_common_event(event: dict[str, object], phase: str, tool: str) -> None:
    assert event["timestamp"]
    assert event["event"] == "approval_card_mutation"
    assert event["phase"] == phase
    assert event["tool"] == tool
    assert set(event) == {
        "timestamp",
        "event",
        "phase",
        "tool",
        "project_id",
        "task_id",
        "outcome",
        "status",
        "error_type",
    }


def test_mutation_diagnostics_log_noop_attempt(tmp_path: Path, caplog) -> None:
    runner = make_runner(tmp_path)

    with caplog.at_level(logging.INFO, logger="local_codex_bridge.task_runner"):
        result = runner.git_sync_local_branch_to_origin("dummy", target_branch="main")

    assert result["status"] == "ok_noop"
    records = mutation_records(caplog)
    assert [(r.lcb_phase, r.lcb_tool) for r in records] == [
        ("invoked", "git_sync_local_branch_to_origin"),
        ("completed", "git_sync_local_branch_to_origin"),
    ]
    assert records[0].lcb_project_id == "dummy"
    assert records[1].lcb_outcome == "no_op"
    assert records[1].lcb_status == "ok_noop"

    events = diagnostic_events(runner)
    assert [(e["phase"], e["tool"]) for e in events] == [
        ("invoked", "git_sync_local_branch_to_origin"),
        ("completed", "git_sync_local_branch_to_origin"),
    ]
    assert_common_event(events[0], "invoked", "git_sync_local_branch_to_origin")
    assert events[0]["project_id"] == "dummy"
    assert events[0]["task_id"] is None
    assert events[0]["outcome"] is None
    assert events[0]["status"] is None
    assert events[0]["error_type"] is None
    assert_common_event(events[1], "completed", "git_sync_local_branch_to_origin")
    assert events[1]["project_id"] == "dummy"
    assert events[1]["outcome"] == "no_op"
    assert events[1]["status"] == "ok_noop"
    assert events[1]["error_type"] is None


def test_mutation_diagnostics_log_blocked_attempt_without_sensitive_inputs(
    tmp_path: Path,
    caplog,
) -> None:
    runner = make_runner(tmp_path)
    sensitive_message = "sensitive commit message"

    with caplog.at_level(logging.INFO, logger="local_codex_bridge.task_runner"):
        result = runner.git_commit_and_push("dummy", [], sensitive_message)

    assert result["status"] == "blocked_input"
    records = mutation_records(caplog)
    assert [(r.lcb_phase, r.lcb_tool) for r in records] == [
        ("invoked", "git_commit_and_push"),
        ("completed", "git_commit_and_push"),
    ]
    assert records[0].lcb_project_id == "dummy"
    assert records[1].lcb_outcome == "blocked"
    assert records[1].lcb_status == "blocked_input"
    assert sensitive_message not in caplog.text

    events = diagnostic_events(runner)
    assert [(e["phase"], e["tool"]) for e in events] == [
        ("invoked", "git_commit_and_push"),
        ("completed", "git_commit_and_push"),
    ]
    assert_common_event(events[0], "invoked", "git_commit_and_push")
    assert events[0]["project_id"] == "dummy"
    assert events[0]["outcome"] is None
    assert_common_event(events[1], "completed", "git_commit_and_push")
    assert events[1]["project_id"] == "dummy"
    assert events[1]["outcome"] == "blocked"
    assert events[1]["status"] == "blocked_input"
    assert sensitive_message not in (runner.config.server.task_dir / "mutation_diagnostics.jsonl").read_text(
        encoding="utf-8"
    )


def test_mutation_diagnostics_append_failure_is_non_fatal(tmp_path: Path, monkeypatch) -> None:
    runner = make_runner(tmp_path)

    def fail_append(*_args, **_kwargs) -> None:
        raise OSError("diagnostic append failed")

    monkeypatch.setattr("local_codex_bridge.task_runner._append_mutation_diagnostic_jsonl", fail_append)

    result = runner.git_sync_local_branch_to_origin("dummy", target_branch="main")

    assert result["status"] == "ok_noop"
