from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from local_codex_bridge.cli import app
from local_codex_bridge.config import BridgeConfig

SECRET_TOKEN = "super-secret-test-token"
OIDC_CLIENT_ID = "oidc-client-id-secret-value"
OIDC_CLIENT_SECRET = "oidc-client-secret-value"


def invoke_init(config_path: Path, user_input: str, *args: str):
    return CliRunner().invoke(
        app,
        ["init", "--config", str(config_path), *args],
        input=user_input,
    )


def local_input(project_path: Path, project_id: str = "demo") -> str:
    return f"\n{project_id}\nDemo Project\n{project_path}\n"


def oidc_input(project_path: Path, public_base_url: str = "https://lcb.example.test") -> str:
    return (
        "2\n"
        f"{public_base_url}\n"
        "https://idp.example.test/.well-known/openid-configuration\n"
        "\n"
        "\n"
        "demo\n"
        "Demo Project\n"
        f"{project_path}\n"
    )


def static_input(project_path: Path) -> str:
    return f"3\n\nstatic_demo\nStatic Demo\n{project_path}\n"


def test_local_only_interactive_config_generation(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    project_path = tmp_path / "project"

    result = invoke_init(config_path, local_input(project_path))

    assert result.exit_code == 0, result.output
    assert config_path.exists()
    text = config_path.read_text(encoding="utf-8")
    assert 'host = "127.0.0.1"' in text
    assert "port = 8765" in text
    assert 'task_dir = "~/.local-codex-bridge/tasks"' in text
    assert 'codex_bin = "codex"' in text
    assert 'default_model = "gpt-5.5"' in text
    assert 'default_codex_args = ["--json"]' in text
    assert '[auth]\nmode = "auto"' in text
    assert "[projects.demo]" in text
    assert 'git_status = ["git", "status", "--short", "--branch"]' in text
    cfg = BridgeConfig.load_for_doctor(config_path)
    assert cfg.auth.mode == "auto"
    assert "demo" in cfg.projects


def test_oidc_interactive_config_generation_without_secrets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("LCB_OIDC_CLIENT_ID", raising=False)
    monkeypatch.delenv("LCB_OIDC_CLIENT_SECRET", raising=False)
    config_path = tmp_path / "config.toml"

    result = invoke_init(config_path, oidc_input(tmp_path / "project"))

    assert result.exit_code == 0, result.output
    text = config_path.read_text(encoding="utf-8")
    assert 'public_base_url = "https://lcb.example.test"' in text
    assert (
        'provider_config_url = "https://idp.example.test/.well-known/openid-configuration"'
        in text
    )
    assert 'client_id_env = "LCB_OIDC_CLIENT_ID"' in text
    assert 'client_secret_env = "LCB_OIDC_CLIENT_SECRET"' in text
    assert OIDC_CLIENT_ID not in text
    assert OIDC_CLIENT_SECRET not in text
    assert OIDC_CLIENT_ID not in result.output
    assert OIDC_CLIENT_SECRET not in result.output
    cfg = BridgeConfig.load_for_doctor(config_path)
    assert cfg.auth.mode == "oidc_proxy"


def test_oidc_reprompts_invalid_client_id_env_var_and_uses_valid_name(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    user_input = (
        "2\n"
        "https://lcb.example.test\n"
        "https://idp.example.test/.well-known/openid-configuration\n"
        "1-BAD-NAME\n"
        "VALID_CLIENT_ID_ENV\n"
        "VALID_CLIENT_SECRET_ENV\n"
        "demo\n"
        "Demo Project\n"
        f"{tmp_path / 'project'}\n"
    )

    result = invoke_init(config_path, user_input)

    assert result.exit_code == 0, result.output
    assert "Env var name must contain only letters" in result.output
    text = config_path.read_text(encoding="utf-8")
    assert 'client_id_env = "VALID_CLIENT_ID_ENV"' in text
    assert 'client_secret_env = "VALID_CLIENT_SECRET_ENV"' in text
    assert "1-BAD-NAME" not in text
    assert OIDC_CLIENT_ID not in text
    assert OIDC_CLIENT_SECRET not in text
    assert OIDC_CLIENT_ID not in result.output
    assert OIDC_CLIENT_SECRET not in result.output


def test_static_bearer_interactive_config_generation_without_token(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("LCB_AUTH_TOKEN", raising=False)
    config_path = tmp_path / "config.toml"

    result = invoke_init(config_path, static_input(tmp_path / "project"))

    assert result.exit_code == 0, result.output
    text = config_path.read_text(encoding="utf-8")
    assert 'mode = "static_bearer"' in text
    assert 'token_env = "LCB_AUTH_TOKEN"' in text
    assert 'client_id = "local-codex-bridge-static"' in text
    assert SECRET_TOKEN not in text
    assert SECRET_TOKEN not in result.output
    cfg = BridgeConfig.load_for_doctor(config_path)
    assert cfg.auth.mode == "static_bearer"


def test_static_bearer_reprompts_invalid_token_env_var_and_uses_valid_name(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    user_input = (
        "3\n"
        "BAD-NAME\n"
        "VALID_TOKEN_ENV\n"
        "static_demo\n"
        "Static Demo\n"
        f"{tmp_path / 'project'}\n"
    )

    result = invoke_init(config_path, user_input)

    assert result.exit_code == 0, result.output
    assert "Env var name must contain only letters" in result.output
    text = config_path.read_text(encoding="utf-8")
    assert 'token_env = "VALID_TOKEN_ENV"' in text
    assert "BAD-NAME" not in text
    assert SECRET_TOKEN not in text
    assert SECRET_TOKEN not in result.output


def test_existing_config_refusal_happens_before_prompts(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("original\n", encoding="utf-8")

    result = invoke_init(config_path, "")

    assert result.exit_code != 0
    assert "Config already exists" in result.output
    assert "--dry-run" in result.output
    assert "--force" in result.output
    assert "Choose auth mode" not in result.output
    assert config_path.read_text(encoding="utf-8") == "original\n"


def test_force_backs_up_existing_file_and_writes_new_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("original\n", encoding="utf-8")

    result = invoke_init(config_path, local_input(tmp_path / "project"), "--force")

    assert result.exit_code == 0, result.output
    backups = list(tmp_path.glob("config.toml.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "original\n"
    assert '[auth]\nmode = "auto"' in config_path.read_text(encoding="utf-8")


def test_dry_run_prints_toml_without_creating_files_or_directories(tmp_path: Path) -> None:
    config_dir = tmp_path / "new-config-dir"
    config_path = config_dir / "config.toml"

    result = invoke_init(config_path, local_input(tmp_path / "project"), "--dry-run")

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("[server]")
    assert "[projects.demo]" in result.stdout
    assert "Next step" not in result.stdout
    assert "doctor" not in result.stdout
    assert "export" not in result.stdout
    assert not config_path.exists()
    assert not config_dir.exists()
    assert not list(tmp_path.glob("config.toml.bak.*"))


def test_dry_run_wins_over_force_without_write_or_backup(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("original\n", encoding="utf-8")

    result = invoke_init(
        config_path,
        local_input(tmp_path / "project"),
        "--dry-run",
        "--force",
    )

    assert result.exit_code == 0, result.output
    assert config_path.read_text(encoding="utf-8") == "original\n"
    assert not list(tmp_path.glob("config.toml.bak.*"))


def test_init_validation_does_not_create_task_dir(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    config_path = tmp_path / "config.toml"

    result = invoke_init(config_path, local_input(tmp_path / "project"))

    assert result.exit_code == 0, result.output
    assert not (home / ".local-codex-bridge" / "tasks").exists()


def test_oidc_init_does_not_construct_oidc_proxy(tmp_path: Path, monkeypatch) -> None:
    constructed = False

    class FailingOIDCProxy:
        def __init__(self, **_: object) -> None:
            nonlocal constructed
            constructed = True
            raise AssertionError("init must not construct OIDCProxy")

    monkeypatch.setattr("local_codex_bridge.auth.OIDCProxy", FailingOIDCProxy)
    config_path = tmp_path / "config.toml"

    result = invoke_init(config_path, oidc_input(tmp_path / "project"))

    assert result.exit_code == 0, result.output
    assert constructed is False


def test_bad_oidc_public_base_url_exits_nonzero_and_does_not_write(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"

    result = invoke_init(
        config_path,
        oidc_input(tmp_path / "project", public_base_url="https://lcb.example.test/mcp"),
    )

    assert result.exit_code != 0
    assert "Generated config failed validation" in result.output
    assert "/mcp" in result.output
    assert not config_path.exists()


def test_bad_oidc_http_public_base_url_exits_nonzero_and_does_not_write(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"

    result = invoke_init(
        config_path,
        oidc_input(tmp_path / "project", public_base_url="http://lcb.example.test"),
    )

    assert result.exit_code != 0
    assert "Generated config failed validation" in result.output
    assert "https://" in result.output
    assert not config_path.exists()


def test_toml_escaping_for_prompted_strings(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    project_path = tmp_path / 'path-with-"quote"-and-\\backslash'
    user_input = f"\nescaped\nDemo \"Quoted\" \\ Name\n{project_path}\n"

    result = invoke_init(config_path, user_input)

    assert result.exit_code == 0, result.output
    text = config_path.read_text(encoding="utf-8")
    assert 'name = "Demo \\"Quoted\\" \\\\ Name"' in text
    assert '\\"quote\\"' in text
    cfg = BridgeConfig.load_for_doctor(config_path)
    assert cfg.projects["escaped"].name == 'Demo "Quoted" \\ Name'
