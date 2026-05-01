from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from local_codex_bridge.cli import app
from local_codex_bridge.config import BridgeConfig


SECRET_TOKEN = "super-secret-test-token"
OIDC_CLIENT_ID = "oidc-client-id-secret-value"
OIDC_CLIENT_SECRET = "oidc-client-secret-value"


def make_project(tmp_path: Path) -> Path:
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    return project_dir


def write_config(
    tmp_path: Path,
    *,
    host: str = "127.0.0.1",
    public_base_url: str | None = None,
    provider_config_url: str = "https://idp.example.test/.well-known/openid-configuration",
    task_dir: Path | None = None,
    auth_block: str | None = None,
) -> Path:
    project_dir = make_project(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    public_line = f'public_base_url = "{public_base_url}"\n' if public_base_url is not None else ""
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        f"""
[server]
host = "{host}"
port = 8765
{public_line}task_dir = "{task_dir or tmp_path / 'tasks'}"
codex_bin = "codex"
default_model = "gpt-5.5"
default_codex_args = ["--json"]

{auth_block or ""}

[projects.demo]
name = "Demo"
path = "{project_dir}"
""",
        encoding="utf-8",
    )
    return cfg_file


def oidc_auth_block(
    *,
    provider_config_url: str = "https://idp.example.test/.well-known/openid-configuration",
) -> str:
    return (
        '[auth]\n'
        'mode = "oidc_proxy"\n'
        f'provider_config_url = "{provider_config_url}"\n'
        'client_id_env = "LCB_OIDC_CLIENT_ID"\n'
        'client_secret_env = "LCB_OIDC_CLIENT_SECRET"\n'
    )


def run_doctor(config_file: Path):
    return CliRunner().invoke(app, ["doctor", "--config", str(config_file)])


def test_oidc_doctor_prints_setup_values_without_secrets_or_oidc_proxy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("LCB_OIDC_CLIENT_ID", OIDC_CLIENT_ID)
    monkeypatch.setenv("LCB_OIDC_CLIENT_SECRET", OIDC_CLIENT_SECRET)
    constructed = False

    class FailingOIDCProxy:
        def __init__(self, **_: object) -> None:
            nonlocal constructed
            constructed = True
            raise AssertionError("doctor must not construct OIDCProxy")

    monkeypatch.setattr("local_codex_bridge.auth.OIDCProxy", FailingOIDCProxy)
    cfg_file = write_config(
        tmp_path,
        public_base_url="https://lcb.example.test",
        auth_block=oidc_auth_block(),
    )

    result = run_doctor(cfg_file)

    assert result.exit_code == 0
    assert "Auth mode:" in result.output
    assert "oidc_proxy" in result.output
    assert "https://lcb.example.test" in result.output
    assert "https://lcb.example.test/mcp" in result.output
    assert "https://lcb.example.test/auth/callback" in result.output
    assert "https://idp.example.test/.well-known/openid-configuration" in result.output
    assert "LCB_OIDC_CLIENT_ID" in result.output
    assert "LCB_OIDC_CLIENT_SECRET" in result.output
    assert "(set)" in result.output
    assert OIDC_CLIENT_ID not in result.output
    assert OIDC_CLIENT_SECRET not in result.output
    assert constructed is False


def test_oidc_doctor_missing_env_vars_exits_one_without_weakening_strict_load(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("LCB_OIDC_CLIENT_ID", raising=False)
    monkeypatch.setenv("LCB_OIDC_CLIENT_SECRET", "   ")
    cfg_file = write_config(
        tmp_path,
        public_base_url="https://lcb.example.test",
        auth_block=oidc_auth_block(),
    )

    result = run_doctor(cfg_file)

    assert result.exit_code == 1
    assert "LCB_OIDC_CLIENT_ID" in result.output
    assert "LCB_OIDC_CLIENT_SECRET" in result.output
    assert "(missing)" in result.output
    assert OIDC_CLIENT_SECRET not in result.output

    try:
        BridgeConfig.load(cfg_file)
    except ValueError as exc:
        assert "requires non-empty env var" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("BridgeConfig.load must remain strict")


def test_static_bearer_doctor_reports_env_name_not_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LCB_AUTH_TOKEN", SECRET_TOKEN)
    cfg_file = write_config(
        tmp_path,
        auth_block='[auth]\nmode = "static_bearer"\ntoken_env = "LCB_AUTH_TOKEN"\n',
    )

    result = run_doctor(cfg_file)

    assert result.exit_code == 0
    assert "static_bearer" in result.output
    assert "LCB_AUTH_TOKEN" in result.output
    assert "(set)" in result.output
    assert SECRET_TOKEN not in result.output
    assert "local/internal/test" in result.output


def test_static_bearer_doctor_missing_or_blank_token_exits_one(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg_file = write_config(
        tmp_path,
        auth_block='[auth]\nmode = "static_bearer"\ntoken_env = "LCB_AUTH_TOKEN"\n',
    )

    monkeypatch.delenv("LCB_AUTH_TOKEN", raising=False)
    missing = run_doctor(cfg_file)
    assert missing.exit_code == 1
    assert "LCB_AUTH_TOKEN" in missing.output
    assert "(missing)" in missing.output

    monkeypatch.setenv("LCB_AUTH_TOKEN", " \t ")
    blank = run_doctor(cfg_file)
    assert blank.exit_code == 1
    assert "(missing)" in blank.output
    assert " \t " not in blank.output


def test_auto_and_disabled_doctor_loopback_local_only_exit_zero(tmp_path: Path) -> None:
    auto_result = run_doctor(write_config(tmp_path / "auto", auth_block='[auth]\nmode = "auto"\n'))
    disabled_result = run_doctor(
        write_config(tmp_path / "disabled", auth_block='[auth]\nmode = "disabled"\n')
    )

    assert auto_result.exit_code == 0
    assert "No-auth local-only status:" in auto_result.output
    assert "Do not expose" in auto_result.output
    assert disabled_result.exit_code == 0
    assert "disabled" in disabled_result.output
    assert "Do not expose" in disabled_result.output


def test_doctor_still_rejects_unsafe_no_auth_configs(tmp_path: Path) -> None:
    non_loopback = run_doctor(
        write_config(
            tmp_path / "non-loopback",
            host="0.0.0.0",
            auth_block='[auth]\nmode = "auto"\n',
        )
    )
    public_url = run_doctor(
        write_config(
            tmp_path / "public-url",
            public_base_url="https://lcb.example.test",
            auth_block='[auth]\nmode = "disabled"\n',
        )
    )

    assert non_loopback.exit_code != 0
    assert public_url.exit_code != 0


def test_doctor_prints_placeholder_warning(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LCB_OIDC_CLIENT_ID", OIDC_CLIENT_ID)
    monkeypatch.setenv("LCB_OIDC_CLIENT_SECRET", OIDC_CLIENT_SECRET)
    cfg_file = write_config(
        tmp_path,
        public_base_url="https://example.com",
        auth_block=oidc_auth_block(
            provider_config_url="https://YOUR-IDP.example.test/.well-known/openid-configuration"
        ),
    )

    result = run_doctor(cfg_file)

    assert result.exit_code == 0
    assert "Placeholder warning:" in result.output
    assert "YOUR-" in result.output
    assert "example.com" in result.output
    assert "example.test" in result.output


def test_doctor_does_not_create_task_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LCB_OIDC_CLIENT_ID", OIDC_CLIENT_ID)
    monkeypatch.setenv("LCB_OIDC_CLIENT_SECRET", OIDC_CLIENT_SECRET)
    task_dir = tmp_path / "tasks-not-created"
    cfg_file = write_config(
        tmp_path,
        public_base_url="https://lcb.example.test",
        task_dir=task_dir,
        auth_block=oidc_auth_block(),
    )

    result = run_doctor(cfg_file)

    assert result.exit_code == 0
    assert not task_dir.exists()
