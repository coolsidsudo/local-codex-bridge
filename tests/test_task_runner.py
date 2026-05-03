from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from local_codex_bridge.cli import app
from local_codex_bridge.config import BridgeConfig, ProjectConfig, ServerConfig
from local_codex_bridge.task_runner import (
    REVIEW_CONTRACT_MARKER,
    REVIEW_CONTRACT_VERSION,
    TaskRunner,
)


def make_runner(tmp_path: Path) -> tuple[TaskRunner, Path, Path]:
    project = tmp_path / "project"
    project.mkdir()
    task_dir = tmp_path / "tasks"
    cfg = BridgeConfig(
        server=ServerConfig(
            task_dir=task_dir,
            codex_bin="codex",
            default_model="gpt-5.5",
            default_codex_args=["--json"],
        ),
        projects={"demo": ProjectConfig(name="Demo", path=project)},
    )
    return TaskRunner(cfg), project, task_dir


def make_verification_runner(tmp_path: Path) -> tuple[TaskRunner, Path]:
    project = tmp_path / "project"
    project.mkdir()
    cfg = BridgeConfig(
        server=ServerConfig(task_dir=tmp_path / "tasks"),
        projects={
            "demo": ProjectConfig(
                name="Demo",
                path=project,
                verification={
                    "pass_one": [sys.executable, "-c", "print('pass one')"],
                    "pass_two": [sys.executable, "-c", "print('pass two')"],
                    "fail": [
                        sys.executable,
                        "-c",
                        (
                            "import sys; print('fail out'); "
                            "print('fail err', file=sys.stderr); sys.exit(3)"
                        ),
                    ],
                    "big_output": [
                        sys.executable,
                        "-c",
                        "import sys; print('o' * 40005); print('e' * 40006, file=sys.stderr)",
                    ],
                    "timeout": [sys.executable, "-c", "import time; time.sleep(2)"],
                    "missing_executable": ["definitely-missing-lcb-executable"],
                },
            )
        },
    )
    return TaskRunner(cfg), project


def read_task_record(task_dir: Path, task_id: str) -> tuple[str, dict[str, object]]:
    task_path = task_dir / task_id
    prompt = (task_path / "prompt.md").read_text(encoding="utf-8")
    meta = json.loads((task_path / "meta.json").read_text(encoding="utf-8"))
    return prompt, meta


def test_run_verification_bundle_all_commands_passes_in_order(tmp_path: Path) -> None:
    runner, project = make_verification_runner(tmp_path)

    result = runner.run_verification_bundle("demo", ["pass_one", "pass_two"])

    assert result["status"] == "ok"
    assert result["project_id"] == "demo"
    assert result["name"] == "Demo"
    assert result["path"] == str(project)
    assert result["requested_command_keys"] == ["pass_one", "pass_two"]
    assert result["timeout_per_command"] == 600
    assert result["stop_on_fail"] is False
    assert result["summary"] == {"passed": 2, "failed": 0, "not_run": 0}
    assert [item["command_key"] for item in result["results"]] == ["pass_one", "pass_two"]
    assert [item["status"] for item in result["results"]] == ["passed", "passed"]
    assert all(item["returncode"] == 0 for item in result["results"])
    assert result["results"][0]["stdout"].strip() == "pass one"
    assert isinstance(result["elapsed_seconds"], float)


def test_run_verification_bundle_continues_after_failure_by_default(tmp_path: Path) -> None:
    runner, _ = make_verification_runner(tmp_path)

    result = runner.run_verification_bundle("demo", ["pass_one", "fail", "pass_two"])

    assert result["status"] == "failed_verification"
    assert result["summary"] == {"passed": 2, "failed": 1, "not_run": 0}
    assert [item["status"] for item in result["results"]] == ["passed", "failed", "passed"]
    assert result["results"][1]["returncode"] == 3
    assert result["results"][1]["reason"] == "nonzero exit status"
    assert "pass two" in result["results"][2]["stdout"]


def test_run_verification_bundle_stop_on_fail_marks_remaining_not_run(tmp_path: Path) -> None:
    runner, _ = make_verification_runner(tmp_path)

    result = runner.run_verification_bundle(
        "demo",
        ["pass_one", "fail", "pass_two"],
        stop_on_fail=True,
    )

    assert result["status"] == "failed_verification"
    assert result["summary"] == {"passed": 1, "failed": 1, "not_run": 1}
    assert [item["status"] for item in result["results"]] == ["passed", "failed", "not_run"]
    assert result["results"][2]["command_key"] == "pass_two"
    assert result["results"][2]["returncode"] is None
    assert result["results"][2]["stdout"] == ""
    assert result["results"][2]["reason"].startswith("not run because stop_on_fail")


def test_run_verification_bundle_allows_duplicate_command_keys(tmp_path: Path) -> None:
    runner, _ = make_verification_runner(tmp_path)

    result = runner.run_verification_bundle("demo", ["pass_one", "pass_one"])

    assert result["status"] == "ok"
    assert result["summary"] == {"passed": 2, "failed": 0, "not_run": 0}
    assert [item["command_key"] for item in result["results"]] == ["pass_one", "pass_one"]


def test_run_verification_bundle_unknown_key_is_blocked_before_running(tmp_path: Path) -> None:
    runner, _ = make_verification_runner(tmp_path)

    result = runner.run_verification_bundle("demo", ["pass_one", "unknown", "pass_two"])

    assert result["status"] == "blocked_input"
    assert "not allowlisted" in result["error"]
    assert "results" not in result


def test_run_verification_bundle_rejects_non_list_command_keys(tmp_path: Path) -> None:
    runner, _ = make_verification_runner(tmp_path)

    result = runner.run_verification_bundle("demo", "pass_one")  # type: ignore[arg-type]

    assert result == {"status": "blocked_input", "error": "command_keys must be a non-empty list"}


def test_run_verification_bundle_rejects_empty_command_keys(tmp_path: Path) -> None:
    runner, _ = make_verification_runner(tmp_path)

    result = runner.run_verification_bundle("demo", [])

    assert result == {"status": "blocked_input", "error": "command_keys must not be empty"}


@pytest.mark.parametrize("command_key", ["", "   ", 123, None])
def test_run_verification_bundle_rejects_blank_or_non_string_keys(
    tmp_path: Path,
    command_key: object,
) -> None:
    runner, _ = make_verification_runner(tmp_path)

    result = runner.run_verification_bundle("demo", [command_key])  # type: ignore[list-item]

    assert result["status"] == "blocked_input"


@pytest.mark.parametrize("timeout", [0, -1, "10", True])
def test_run_verification_bundle_rejects_invalid_timeout(
    tmp_path: Path,
    timeout: object,
) -> None:
    runner, _ = make_verification_runner(tmp_path)

    result = runner.run_verification_bundle(
        "demo",
        ["pass_one"],
        timeout_per_command=timeout,  # type: ignore[arg-type]
    )

    assert result == {
        "status": "blocked_input",
        "error": "timeout_per_command must be a positive integer",
    }


def test_run_verification_bundle_rejects_invalid_stop_on_fail(tmp_path: Path) -> None:
    runner, _ = make_verification_runner(tmp_path)

    result = runner.run_verification_bundle(
        "demo",
        ["pass_one"],
        stop_on_fail="yes",  # type: ignore[arg-type]
    )

    assert result == {"status": "blocked_input", "error": "stop_on_fail must be a boolean"}


def test_run_verification_bundle_bounds_output_and_reports_truncation(tmp_path: Path) -> None:
    runner, _ = make_verification_runner(tmp_path)

    result = runner.run_verification_bundle("demo", ["big_output"])

    item = result["results"][0]
    assert result["status"] == "ok"
    assert item["stdout_truncated"] is True
    assert item["stderr_truncated"] is True
    assert item["stdout_omitted_chars"] > 0
    assert item["stderr_omitted_chars"] > 0
    assert len(item["stdout"]) == 40000
    assert len(item["stderr"]) == 40000


def test_run_verification_bundle_timeout_returns_failure_evidence(tmp_path: Path) -> None:
    runner, _ = make_verification_runner(tmp_path)

    result = runner.run_verification_bundle("demo", ["timeout"], timeout_per_command=1)

    item = result["results"][0]
    assert result["status"] == "failed_verification"
    assert result["summary"] == {"passed": 0, "failed": 1, "not_run": 0}
    assert item["status"] == "failed"
    assert item["returncode"] is None
    assert item["reason"] == "timed out after 1 seconds"


def test_run_verification_bundle_missing_executable_returns_failure_evidence(
    tmp_path: Path,
) -> None:
    runner, _ = make_verification_runner(tmp_path)

    result = runner.run_verification_bundle("demo", ["missing_executable"])

    item = result["results"][0]
    assert result["status"] == "failed_verification"
    assert item["status"] == "failed"
    assert item["returncode"] == 127
    assert item["reason"] == "executable not found"
    assert "definitely-missing-lcb-executable" in item["stderr"]


def test_start_codex_task_dry_run_without_review_contract_keeps_prompt(
    tmp_path: Path,
) -> None:
    runner, _, task_dir = make_runner(tmp_path)
    result = runner.start_codex_task("demo", "Implement the thing.\n", dry_run=True)

    prompt, meta = read_task_record(task_dir, result["task_id"])

    assert prompt == "Implement the thing.\n"
    assert meta["review_contract_requested"] is False
    assert meta["review_contract_version"] is None
    assert meta["review_contract_footer_appended"] is False
    assert result["cmd"] == ["codex", "exec", "-m", "gpt-5.5", "--json"]
    assert meta["cmd"] == result["cmd"]


def test_start_codex_task_dry_run_with_review_contract_appends_footer(
    tmp_path: Path,
) -> None:
    runner, _, task_dir = make_runner(tmp_path)
    result = runner.start_codex_task(
        "demo",
        "Implement the thing.  \n\n",
        dry_run=True,
        review_contract=True,
    )

    prompt, meta = read_task_record(task_dir, result["task_id"])

    assert prompt.startswith("Implement the thing.\n\n---\n")
    assert REVIEW_CONTRACT_MARKER in prompt
    assert "Do not paste full diffs or full file contents" in prompt
    assert meta["review_contract_requested"] is True
    assert meta["review_contract_version"] == REVIEW_CONTRACT_VERSION
    assert meta["review_contract_footer_appended"] is True
    assert result["cmd"] == ["codex", "exec", "-m", "gpt-5.5", "--json"]
    assert meta["cmd"] == result["cmd"]


def test_start_codex_task_dry_run_existing_review_contract_marker_is_not_duplicated(
    tmp_path: Path,
) -> None:
    runner, _, task_dir = make_runner(tmp_path)
    prompt_with_marker = f"Implement the thing.\n\n{REVIEW_CONTRACT_MARKER}\n"
    result = runner.start_codex_task(
        "demo",
        prompt_with_marker,
        dry_run=True,
        review_contract=True,
    )

    prompt, meta = read_task_record(task_dir, result["task_id"])

    assert prompt == prompt_with_marker
    assert prompt.count(REVIEW_CONTRACT_MARKER) == 1
    assert meta["review_contract_requested"] is True
    assert meta["review_contract_version"] == REVIEW_CONTRACT_VERSION
    assert meta["review_contract_footer_appended"] is False


def test_dry_run_task_cli_review_contract_creates_inspectable_prompt(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task_dir = tmp_path / "tasks"
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        f"""
[server]
task_dir = "{task_dir}"
codex_bin = "codex"
default_model = "gpt-5.5"
default_codex_args = ["--json"]

[projects.demo]
name = "Demo"
path = "{project}"
""",
        encoding="utf-8",
    )
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("Implement via CLI dry run.\n", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "dry-run-task",
            "demo",
            str(prompt_file),
            "--review-contract",
            "--config",
            str(cfg_file),
        ],
    )

    assert result.exit_code == 0
    output = ast.literal_eval(result.output.strip())
    prompt, meta = read_task_record(task_dir, output["task_id"])
    assert "Implement via CLI dry run." in prompt
    assert REVIEW_CONTRACT_MARKER in prompt
    assert meta["status"] == "dry_run"
    assert meta["review_contract_requested"] is True
    assert meta["review_contract_version"] == REVIEW_CONTRACT_VERSION
    assert meta["review_contract_footer_appended"] is True
