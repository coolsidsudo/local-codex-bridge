from pathlib import Path

from local_codex_bridge.config import BridgeConfig


def test_load_config(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        f"""
[server]
host = "127.0.0.1"
port = 8765
task_dir = "{tmp_path / "tasks"}"
codex_bin = "codex"
default_model = "gpt-5.5"
default_codex_args = ["--json"]

[projects.demo]
name = "Demo"
path = "{project_dir}"

[projects.demo.verification]
git_status = ["git", "status", "--short", "--branch"]
""",
        encoding="utf-8",
    )

    cfg = BridgeConfig.load(cfg_file)

    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.port == 8765
    assert cfg.projects["demo"].name == "Demo"
    assert cfg.projects["demo"].path == project_dir.resolve()
