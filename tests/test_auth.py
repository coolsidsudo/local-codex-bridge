from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from fastmcp.utilities.tests import run_server_async
from typer.testing import CliRunner

from local_codex_bridge.auth import build_auth_provider
from local_codex_bridge.cli import app
from local_codex_bridge.config import AuthConfig, BridgeConfig, ProjectConfig, ServerConfig
from local_codex_bridge.server import build_mcp


SECRET_TOKEN = "super-secret-test-token"
WHITESPACE_TOKEN = "   	  "


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_project(tmp_path: Path) -> Path:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    return project_dir


def write_config(
    tmp_path: Path,
    *,
    host: str = "127.0.0.1",
    public_base_url: str | None = None,
    auth_block: str | None = None,
) -> Path:
    project_dir = make_project(tmp_path)
    public_line = f'public_base_url = "{public_base_url}"\n' if public_base_url is not None else ""
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        f"""
[server]
host = "{host}"
port = 8765
{public_line}task_dir = "{tmp_path / 'tasks'}"
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


def test_default_loopback_config_has_no_auth_provider(tmp_path: Path) -> None:
    cfg = BridgeConfig.load(write_config(tmp_path))

    assert cfg.auth.mode == "auto"
    assert build_auth_provider(cfg) is None


def test_public_base_url_with_auto_fails(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as exc_info:
        BridgeConfig.load(write_config(tmp_path, public_base_url="https://lcb.example.test"))

    message = str(exc_info.value)
    assert "public_base_url" in message
    assert SECRET_TOKEN not in message


def test_public_base_url_with_disabled_fails(tmp_path: Path) -> None:
    auth_block = '[auth]\nmode = "disabled"\n'

    with pytest.raises(ValueError) as exc_info:
        BridgeConfig.load(
            write_config(
                tmp_path,
                public_base_url="https://lcb.example.test",
                auth_block=auth_block,
            )
        )

    assert "disabled" in str(exc_info.value)


def test_non_loopback_with_auto_fails(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as exc_info:
        BridgeConfig.load(write_config(tmp_path, host="0.0.0.0"))

    assert "loopback" in str(exc_info.value)


def test_non_loopback_with_disabled_fails(tmp_path: Path) -> None:
    auth_block = '[auth]\nmode = "disabled"\n'

    with pytest.raises(ValueError) as exc_info:
        BridgeConfig.load(write_config(tmp_path, host="0.0.0.0", auth_block=auth_block))

    assert "loopback" in str(exc_info.value)


def test_unknown_auth_field_fails_validation(tmp_path: Path) -> None:
    auth_block = '[auth]\nmode = "auto"\nunexpected = "value"\n'

    with pytest.raises(ValueError) as exc_info:
        BridgeConfig.load(write_config(tmp_path, auth_block=auth_block))

    message = str(exc_info.value)
    assert "unexpected" in message
    assert SECRET_TOKEN not in message


def test_blank_required_scopes_entry_fails() -> None:
    with pytest.raises(ValueError) as exc_info:
        AuthConfig(required_scopes=["lcb:read", "  "])

    assert "blank scope" in str(exc_info.value)


def test_empty_required_scopes_fails() -> None:
    with pytest.raises(ValueError) as exc_info:
        AuthConfig(required_scopes=[])

    assert "must not be empty" in str(exc_info.value)


def test_blank_token_scopes_entry_fails() -> None:
    with pytest.raises(ValueError) as exc_info:
        AuthConfig(token_scopes=["lcb:read", "\t"])

    assert "blank scope" in str(exc_info.value)


def test_scope_strings_are_stripped() -> None:
    auth = AuthConfig(required_scopes=[" lcb:read "], token_scopes=[" lcb:write "])

    assert auth.required_scopes == ["lcb:read"]
    assert auth.token_scopes == ["lcb:write"]



def test_token_literal_in_toml_fails_without_leaking_value(tmp_path: Path) -> None:
    auth_block = f'[auth]\nmode = "static_bearer"\ntoken = "{SECRET_TOKEN}"\n'

    with pytest.raises(ValueError) as exc_info:
        BridgeConfig.load(write_config(tmp_path, auth_block=auth_block))

    message = str(exc_info.value)
    assert "token_env" in message
    assert SECRET_TOKEN not in message

def test_static_bearer_without_env_var_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LCB_AUTH_TOKEN", raising=False)
    auth_block = '[auth]\nmode = "static_bearer"\ntoken_env = "LCB_AUTH_TOKEN"\n'

    with pytest.raises(ValueError) as exc_info:
        BridgeConfig.load(write_config(tmp_path, auth_block=auth_block))

    message = str(exc_info.value)
    assert "LCB_AUTH_TOKEN" in message
    assert SECRET_TOKEN not in message



def test_static_bearer_whitespace_only_env_var_fails_without_leaking_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LCB_AUTH_TOKEN", WHITESPACE_TOKEN)
    auth_block = '[auth]\nmode = "static_bearer"\ntoken_env = "LCB_AUTH_TOKEN"\n'

    with pytest.raises(ValueError) as exc_info:
        BridgeConfig.load(write_config(tmp_path, auth_block=auth_block))

    message = str(exc_info.value)
    assert "LCB_AUTH_TOKEN" in message
    assert WHITESPACE_TOKEN not in message

def test_static_bearer_with_env_builds_fastmcp_with_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LCB_AUTH_TOKEN", SECRET_TOKEN)
    auth_block = '[auth]\nmode = "static_bearer"\ntoken_env = "LCB_AUTH_TOKEN"\n'
    cfg = BridgeConfig.load(write_config(tmp_path, auth_block=auth_block))

    mcp = build_mcp(cfg)

    assert mcp.auth is not None


def test_static_token_verifier_accepts_configured_token_and_rejects_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LCB_AUTH_TOKEN", SECRET_TOKEN)
    cfg = BridgeConfig(
        server=ServerConfig(task_dir=tmp_path / "tasks"),
        auth=AuthConfig(mode="static_bearer", token_env="LCB_AUTH_TOKEN"),
        projects={"demo": ProjectConfig(name="Demo", path=make_project(tmp_path))},
    )
    provider = build_auth_provider(cfg)
    assert provider is not None

    valid = asyncio.run(provider.verify_token(SECRET_TOKEN))
    invalid = asyncio.run(provider.verify_token("not-the-token"))

    assert valid is not None
    assert valid.client_id == "local-codex-bridge-static"
    assert invalid is None


def test_static_token_verifier_rejects_scope_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LCB_AUTH_TOKEN", SECRET_TOKEN)
    cfg = BridgeConfig(
        server=ServerConfig(task_dir=tmp_path / "tasks"),
        auth=AuthConfig(
            mode="static_bearer",
            token_env="LCB_AUTH_TOKEN",
            required_scopes=["lcb:admin"],
            token_scopes=["lcb:read"],
        ),
        projects={"demo": ProjectConfig(name="Demo", path=make_project(tmp_path))},
    )
    provider = build_auth_provider(cfg)
    assert provider is not None

    assert asyncio.run(provider.verify_token(SECRET_TOKEN)) is None


def test_static_bearer_startup_output_does_not_include_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LCB_AUTH_TOKEN", SECRET_TOKEN)
    auth_block = '[auth]\nmode = "static_bearer"\ntoken_env = "LCB_AUTH_TOKEN"\n'
    cfg_file = write_config(tmp_path, auth_block=auth_block)

    class DummyMCP:
        def run(self, **_: object) -> None:
            return None

    monkeypatch.setattr("local_codex_bridge.cli.build_mcp", lambda cfg: DummyMCP())
    result = CliRunner().invoke(app, ["serve", "--config", str(cfg_file)])

    assert result.exit_code == 0
    assert "Auth mode:" in result.output
    assert "static_bearer" in result.output
    assert SECRET_TOKEN not in result.output


@pytest.mark.anyio
async def test_static_bearer_http_endpoint_rejects_unauthenticated_and_accepts_valid_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LCB_AUTH_TOKEN", SECRET_TOKEN)
    cfg = BridgeConfig(
        server=ServerConfig(task_dir=tmp_path / "tasks"),
        auth=AuthConfig(mode="static_bearer", token_env="LCB_AUTH_TOKEN"),
        projects={"demo": ProjectConfig(name="Demo", path=make_project(tmp_path))},
    )
    mcp = build_mcp(cfg)

    async with run_server_async(mcp, transport="streamable-http", path="/mcp") as url:
        async with httpx.AsyncClient() as http_client:
            response = await http_client.post(
                url,
                headers={"Accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            )

        assert response.status_code == 401
        assert response.headers.get("www-authenticate", "").startswith("Bearer")
        assert SECRET_TOKEN not in response.text

        async with Client(url, auth=SECRET_TOKEN) as client:
            tools = await client.list_tools()

    assert any(tool.name == "list_projects" for tool in tools)
