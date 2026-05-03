from __future__ import annotations

import ast
import json
from pathlib import Path

from typer.testing import CliRunner

from local_codex_bridge.cli import app
from local_codex_bridge.config import BridgeConfig, ProjectConfig, ServerConfig
from local_codex_bridge.task_runner import REVIEW_CONTRACT_MARKER, REVIEW_CONTRACT_VERSION, TaskRunner


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


def read_task_record(task_dir: Path, task_id: str) -> tuple[str, dict[str, object]]:
    task_path = task_dir / task_id
    prompt = (task_path / "prompt.md").read_text(encoding="utf-8")
    meta = json.loads((task_path / "meta.json").read_text(encoding="utf-8"))
    return prompt, meta


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
