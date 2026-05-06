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
OIDC_CLIENT_ID = "oidc-client-id-secret-value"
OIDC_CLIENT_SECRET = "oidc-client-secret-value"


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




def oidc_auth_block(
    *,
    provider_config_url: str | None = "https://idp.example.test/.well-known/openid-configuration",
    oidc_scopes: list[str] | None = None,
    client_id_env: str = "LCB_OIDC_CLIENT_ID",
    client_secret_env: str = "LCB_OIDC_CLIENT_SECRET",
) -> str:
    provider_line = (
        f'provider_config_url = "{provider_config_url}"\n'
        if provider_config_url is not None
        else ""
    )
    oidc_scopes_line = (
        "oidc_scopes = ["
        + ", ".join(f'"{scope}"' for scope in oidc_scopes)
        + "]\n"
        if oidc_scopes is not None
        else ""
    )
    return (
        '[auth]\n'
        'mode = "oidc_proxy"\n'
        f'{provider_line}'
        f'{oidc_scopes_line}'
        f'client_id_env = "{client_id_env}"\n'
        f'client_secret_env = "{client_secret_env}"\n'
    )


def set_oidc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LCB_OIDC_CLIENT_ID", OIDC_CLIENT_ID)
    monkeypatch.setenv("LCB_OIDC_CLIENT_SECRET", OIDC_CLIENT_SECRET)


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


def test_default_oidc_scopes_are_openid_only() -> None:
    assert AuthConfig().oidc_scopes == ["openid"]


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


def test_empty_oidc_scopes_fails() -> None:
    with pytest.raises(ValueError) as exc_info:
        AuthConfig(oidc_scopes=[])

    assert "must not be empty" in str(exc_info.value)


def test_blank_oidc_scopes_entry_fails() -> None:
    with pytest.raises(ValueError) as exc_info:
        AuthConfig(oidc_scopes=["openid", "\t"])

    assert "blank scope" in str(exc_info.value)


def test_scope_strings_are_stripped() -> None:
    auth = AuthConfig(
        required_scopes=[" lcb:read "],
        token_scopes=[" lcb:write "],
        oidc_scopes=[" openid ", " email "],
    )

    assert auth.required_scopes == ["lcb:read"]
    assert auth.token_scopes == ["lcb:write"]
    assert auth.oidc_scopes == ["openid", "email"]


def test_oidc_toml_configured_scopes_are_stripped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_oidc_env(monkeypatch)

    cfg = BridgeConfig.load(
        write_config(
            tmp_path,
            public_base_url="https://lcb.example.test",
            auth_block=oidc_auth_block(oidc_scopes=[" openid ", " email ", " profile "]),
        )
    )

    assert cfg.auth.oidc_scopes == ["openid", "email", "profile"]



def test_token_literal_in_toml_fails_without_leaking_value(tmp_path: Path) -> None:
    auth_block = f'[auth]\nmode = "static_bearer"\ntoken = "{SECRET_TOKEN}"\n'

    with pytest.raises(ValueError) as exc_info:
        BridgeConfig.load(write_config(tmp_path, auth_block=auth_block))

    message = str(exc_info.value)
    assert "token_env" in message
    assert SECRET_TOKEN not in message



def test_public_base_url_trailing_slash_is_normalized_for_oidc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_oidc_env(monkeypatch)

    cfg = BridgeConfig.load(
        write_config(
            tmp_path,
            public_base_url="https://lcb.example.test/",
            auth_block=oidc_auth_block(),
        )
    )

    assert cfg.server.public_base_url == "https://lcb.example.test"


@pytest.mark.parametrize(
    ("public_base_url", "expected"),
    [
        ("http://lcb.example.test", "https://"),
        ("https://lcb.example.test/mcp", "/mcp"),
        ("https://lcb.example.test/nested", "non-root path"),
        ("https://lcb.example.test?x=1", "query string"),
        ("https://lcb.example.test#frag", "fragment"),
        ("https://user:pass@lcb.example.test", "username or password"),
    ],
)
def test_oidc_public_base_url_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    public_base_url: str,
    expected: str,
) -> None:
    set_oidc_env(monkeypatch)

    with pytest.raises(ValueError) as exc_info:
        BridgeConfig.load(
            write_config(
                tmp_path,
                public_base_url=public_base_url,
                auth_block=oidc_auth_block(),
            )
        )

    assert expected in str(exc_info.value)
    assert OIDC_CLIENT_ID not in str(exc_info.value)
    assert OIDC_CLIENT_SECRET not in str(exc_info.value)
    assert "user:pass" not in str(exc_info.value)


def test_oidc_without_public_base_url_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_oidc_env(monkeypatch)

    with pytest.raises(ValueError) as exc_info:
        BridgeConfig.load(write_config(tmp_path, auth_block=oidc_auth_block()))

    assert "public_base_url" in str(exc_info.value)


@pytest.mark.parametrize(
    ("provider_config_url", "expected"),
    [
        (None, "provider_config_url"),
        ("", "provider_config_url"),
        ("http://idp.example.test/.well-known/openid-configuration", "https://"),
        (
            "https://user:pass@idp.example.test/.well-known/openid-configuration",
            "username or password",
        ),
    ],
)
def test_oidc_provider_config_url_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_config_url: str | None,
    expected: str,
) -> None:
    set_oidc_env(monkeypatch)

    with pytest.raises(ValueError) as exc_info:
        BridgeConfig.load(
            write_config(
                tmp_path,
                public_base_url="https://lcb.example.test",
                auth_block=oidc_auth_block(provider_config_url=provider_config_url),
            )
        )

    assert expected in str(exc_info.value)
    assert OIDC_CLIENT_ID not in str(exc_info.value)
    assert OIDC_CLIENT_SECRET not in str(exc_info.value)
    assert "user:pass" not in str(exc_info.value)


def test_oidc_without_client_id_env_var_fails_without_leaking_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LCB_OIDC_CLIENT_ID", raising=False)
    monkeypatch.setenv("LCB_OIDC_CLIENT_SECRET", OIDC_CLIENT_SECRET)

    with pytest.raises(ValueError) as exc_info:
        BridgeConfig.load(
            write_config(
                tmp_path,
                public_base_url="https://lcb.example.test",
                auth_block=oidc_auth_block(),
            )
        )

    message = str(exc_info.value)
    assert "LCB_OIDC_CLIENT_ID" in message
    assert OIDC_CLIENT_SECRET not in message


def test_oidc_whitespace_client_secret_env_var_fails_without_leaking_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LCB_OIDC_CLIENT_ID", OIDC_CLIENT_ID)
    monkeypatch.setenv("LCB_OIDC_CLIENT_SECRET", WHITESPACE_TOKEN)

    with pytest.raises(ValueError) as exc_info:
        BridgeConfig.load(
            write_config(
                tmp_path,
                public_base_url="https://lcb.example.test",
                auth_block=oidc_auth_block(),
            )
        )

    message = str(exc_info.value)
    assert "LCB_OIDC_CLIENT_SECRET" in message
    assert OIDC_CLIENT_ID not in message
    assert WHITESPACE_TOKEN not in message


@pytest.mark.parametrize("literal_key", ["client_id", "client_secret", "oidc_client_secret", "oauth_client_secret"])
def test_oidc_credential_literal_in_toml_fails_without_leaking_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    literal_key: str,
) -> None:
    set_oidc_env(monkeypatch)
    literal_value = "literal-credential-secret-value"
    auth_block = oidc_auth_block() + f'{literal_key} = "{literal_value}"\n'

    with pytest.raises(ValueError) as exc_info:
        BridgeConfig.load(
            write_config(
                tmp_path,
                public_base_url="https://lcb.example.test",
                auth_block=auth_block,
            )
        )

    message = str(exc_info.value)
    assert literal_key in message
    assert literal_value not in message


def test_oidc_provider_construction_passes_default_scopes_to_fastmcp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LCB_OIDC_CLIENT_ID", f"  {OIDC_CLIENT_ID}  ")
    monkeypatch.setenv("LCB_OIDC_CLIENT_SECRET", f"\t{OIDC_CLIENT_SECRET}\n")
    captured: dict[str, object] = {}

    class DummyOIDCProxy:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("local_codex_bridge.auth.OIDCProxy", DummyOIDCProxy)
    cfg = BridgeConfig.load(
        write_config(
            tmp_path,
            public_base_url="https://lcb.example.test/",
            auth_block=oidc_auth_block(),
        )
    )

    provider = build_auth_provider(cfg)

    assert isinstance(provider, DummyOIDCProxy)
    assert captured == {
        "config_url": "https://idp.example.test/.well-known/openid-configuration",
        "client_id": OIDC_CLIENT_ID,
        "client_secret": OIDC_CLIENT_SECRET,
        "base_url": "https://lcb.example.test",
        "required_scopes": ["openid"],
    }
    assert "redirect_path" not in captured
    assert "upstream_authorization_endpoint" not in captured
    assert "upstream_token_endpoint" not in captured


def test_oidc_provider_construction_passes_configured_scopes_to_fastmcp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_oidc_env(monkeypatch)
    captured: dict[str, object] = {}

    class DummyOIDCProxy:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("local_codex_bridge.auth.OIDCProxy", DummyOIDCProxy)
    cfg = BridgeConfig.load(
        write_config(
            tmp_path,
            public_base_url="https://lcb.example.test",
            auth_block=oidc_auth_block(oidc_scopes=["openid", "email", "profile"]),
        )
    )

    provider = build_auth_provider(cfg)

    assert isinstance(provider, DummyOIDCProxy)
    assert captured["required_scopes"] == ["openid", "email", "profile"]


def test_oidc_startup_output_does_not_include_env_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_oidc_env(monkeypatch)
    cfg_file = write_config(
        tmp_path,
        public_base_url="https://lcb.example.test/",
        auth_block=oidc_auth_block(),
    )

    class DummyMCP:
        def run(self, **_: object) -> None:
            return None

    monkeypatch.setattr("local_codex_bridge.cli.build_mcp", lambda cfg: DummyMCP())
    result = CliRunner().invoke(app, ["serve", "--config", str(cfg_file)])

    assert result.exit_code == 0
    assert "Auth mode:" in result.output
    assert "oidc_proxy" in result.output
    assert "https://lcb.example.test" in result.output
    assert OIDC_CLIENT_ID not in result.output
    assert OIDC_CLIENT_SECRET not in result.output


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


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("oidc_scopes", "expected_scopes"),
    [
        (None, ["openid"]),
        (["openid", "email", "profile"], ["openid", "email", "profile"]),
    ],
)
async def test_oidc_openid_configuration_compatibility_route_returns_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    oidc_scopes: list[str] | None,
    expected_scopes: list[str],
) -> None:
    set_oidc_env(monkeypatch)
    monkeypatch.setattr("local_codex_bridge.server.build_auth_provider", lambda _cfg: None)
    auth_kwargs = {
        "mode": "oidc_proxy",
        "provider_config_url": "https://idp.example.test/.well-known/openid-configuration",
    }
    if oidc_scopes is not None:
        auth_kwargs["oidc_scopes"] = oidc_scopes
    cfg = BridgeConfig(
        server=ServerConfig(
            public_base_url="https://lcb.example.test",
            task_dir=tmp_path / "tasks",
        ),
        auth=AuthConfig(**auth_kwargs),
        projects={"demo": ProjectConfig(name="Demo", path=make_project(tmp_path))},
    )
    mcp = build_mcp(cfg)

    async with run_server_async(mcp, transport="streamable-http", path="/mcp") as url:
        origin = url.removesuffix("/mcp")
        async with httpx.AsyncClient() as http_client:
            response = await http_client.get(f"{origin}/.well-known/openid-configuration")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=3600"
    assert response.json() == {
        "issuer": "https://lcb.example.test/",
        "authorization_endpoint": "https://lcb.example.test/authorize",
        "token_endpoint": "https://lcb.example.test/token",
        "registration_endpoint": "https://lcb.example.test/register",
        "scopes_supported": expected_scopes,
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": [
            "client_secret_post",
            "client_secret_basic",
        ],
        "code_challenge_methods_supported": ["S256"],
        "client_id_metadata_document_supported": True,
    }
    assert "jwks_uri" not in response.json()


@pytest.mark.anyio
async def test_default_no_auth_does_not_expose_openid_configuration_route(
    tmp_path: Path,
) -> None:
    cfg = BridgeConfig(
        server=ServerConfig(task_dir=tmp_path / "tasks"),
        projects={"demo": ProjectConfig(name="Demo", path=make_project(tmp_path))},
    )
    mcp = build_mcp(cfg)

    async with run_server_async(mcp, transport="streamable-http", path="/mcp") as url:
        origin = url.removesuffix("/mcp")
        async with httpx.AsyncClient() as http_client:
            response = await http_client.get(f"{origin}/.well-known/openid-configuration")

    assert response.status_code == 404

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

    tool_names = {tool.name for tool in tools}
    assert "list_projects" in tool_names
    start_codex_task = next(tool for tool in tools if tool.name == "start_codex_task")
    start_codex_task_schema = getattr(start_codex_task, "parameters", None)
    if start_codex_task_schema is None:
        start_codex_task_schema = start_codex_task.inputSchema
    assert start_codex_task_schema["properties"]["review_contract"] == {
        "default": False,
        "type": "boolean",
    }
    assert "get_review_package" in tool_names
    assert "run_verification_bundle" in tool_names
    verification_bundle = next(tool for tool in tools if tool.name == "run_verification_bundle")
    verification_bundle_schema = getattr(verification_bundle, "parameters", None)
    if verification_bundle_schema is None:
        verification_bundle_schema = verification_bundle.inputSchema
    assert verification_bundle_schema["properties"]["timeout_per_command"] == {
        "default": 600,
        "type": "integer",
    }
    assert verification_bundle_schema["properties"]["stop_on_fail"] == {
        "default": False,
        "type": "boolean",
    }
    assert "get_changed_file_diff" in tool_names
    assert "get_changed_file_text" in tool_names
    assert "git_get_branch_status" in tool_names
    assert "git_create_work_branch" in tool_names
    assert "get_acceptance_readiness" in tool_names
    acceptance_readiness = next(tool for tool in tools if tool.name == "get_acceptance_readiness")
    acceptance_readiness_schema = getattr(acceptance_readiness, "parameters", None)
    if acceptance_readiness_schema is None:
        acceptance_readiness_schema = acceptance_readiness.inputSchema
    assert acceptance_readiness_schema["properties"]["remote"] == {
        "default": "origin",
        "type": "string",
    }
    assert "github_create_pr" in tool_names
    assert "github_get_pr_status" in tool_names
    assert "github_merge_pr" in tool_names
    github_merge_pr = next(tool for tool in tools if tool.name == "github_merge_pr")
    github_merge_pr_schema = getattr(github_merge_pr, "parameters", None)
    if github_merge_pr_schema is None:
        github_merge_pr_schema = github_merge_pr.inputSchema
    assert github_merge_pr_schema["properties"]["merge_method"] == {
        "default": "squash",
        "type": "string",
    }
    assert github_merge_pr_schema["properties"]["delete_branch"] == {
        "default": False,
        "type": "boolean",
    }
    assert github_merge_pr_schema["properties"]["expected_head_sha"]["default"] is None
    assert "get_pr_sync_readiness" in tool_names
    pr_sync_readiness = next(tool for tool in tools if tool.name == "get_pr_sync_readiness")
    pr_sync_readiness_schema = getattr(pr_sync_readiness, "parameters", None)
    if pr_sync_readiness_schema is None:
        pr_sync_readiness_schema = pr_sync_readiness.inputSchema
    assert pr_sync_readiness_schema["properties"]["target_branch"] == {
        "default": "main",
        "type": "string",
    }
    assert pr_sync_readiness_schema["properties"]["remote"] == {
        "default": "origin",
        "type": "string",
    }
    assert "git_sync_local_branch_to_origin" in tool_names
    git_sync = next(tool for tool in tools if tool.name == "git_sync_local_branch_to_origin")
    git_sync_schema = getattr(git_sync, "parameters", None)
    if git_sync_schema is None:
        git_sync_schema = git_sync.inputSchema
    assert git_sync_schema["properties"]["target_branch"] == {
        "default": "main",
        "type": "string",
    }
    assert git_sync_schema["properties"]["remote"] == {
        "default": "origin",
        "type": "string",
    }
