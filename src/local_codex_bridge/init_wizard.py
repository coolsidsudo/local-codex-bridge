from __future__ import annotations

import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

import click
import typer
from rich import print

from .config import BridgeConfig

AuthMode = Literal["auto", "oidc_proxy", "static_bearer"]

DEFAULT_TASK_DIR = "~/.local-codex-bridge/tasks"
DEFAULT_MODEL = "gpt-5.5"
DEFAULT_CLIENT_ID_ENV = "LCB_OIDC_CLIENT_ID"
DEFAULT_CLIENT_SECRET_ENV = "LCB_OIDC_CLIENT_SECRET"
DEFAULT_TOKEN_ENV = "LCB_AUTH_TOKEN"
ENV_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ENV_VAR_ERROR = (
    "Env var name must contain only letters, numbers, and underscores, "
    "and must not start with a number."
)


@dataclass(frozen=True)
class InitConfig:
    auth_mode: AuthMode
    project_id: str
    project_name: str
    project_path: str
    public_base_url: str | None = None
    provider_config_url: str | None = None
    client_id_env: str = DEFAULT_CLIENT_ID_ENV
    client_secret_env: str = DEFAULT_CLIENT_SECRET_ENV
    token_env: str = DEFAULT_TOKEN_ENV


def run_init_wizard(config: Path, *, dry_run: bool, force: bool) -> None:
    """Run the interactive init wizard and write or print generated TOML."""
    config_path = config.expanduser().resolve()

    if config_path.exists() and not force and not dry_run:
        print(
            f"[red]Config already exists:[/red] {config_path}\n"
            "Use --dry-run to preview generated TOML or --force to overwrite with a backup."
        )
        raise typer.Exit(1)

    if not dry_run:
        print_security_summary()

    init_config = prompt_for_config(prompt_to_stderr=dry_run)
    toml_text = render_config_toml(init_config)
    validate_generated_toml(toml_text)

    if dry_run:
        typer.echo(toml_text, nl=False)
        return

    backup_path: Path | None = None
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if config_path.exists() and force:
        backup_path = backup_existing_config(config_path)

    config_path.write_text(toml_text, encoding="utf-8")
    try:
        BridgeConfig.load_for_doctor(config_path)
    except Exception as exc:  # pragma: no cover - defensive after pre-validation
        print(f"[red]Generated config was written but failed validation:[/red] {exc}")
        print(f"[yellow]Inspect the config at:[/yellow] {config_path}")
        raise typer.Exit(1) from exc

    print(f"[green]Wrote config:[/green] {config_path}")
    if backup_path is not None:
        print(f"[yellow]Backed up previous config:[/yellow] {backup_path}")
    print_next_steps(config_path, init_config)


def print_security_summary() -> None:
    print("[bold]Local Codex Bridge init[/bold]")
    print("This wizard writes config only. It does not:")
    print("- start MCP")
    print("- run Codex")
    print("- contact an OIDC provider")
    print("- collect secrets")


def prompt_for_config(*, prompt_to_stderr: bool = False) -> InitConfig:
    auth_mode = prompt_auth_mode(prompt_to_stderr=prompt_to_stderr)
    public_base_url: str | None = None
    provider_config_url: str | None = None
    client_id_env = DEFAULT_CLIENT_ID_ENV
    client_secret_env = DEFAULT_CLIENT_SECRET_ENV
    token_env = DEFAULT_TOKEN_ENV

    if auth_mode == "oidc_proxy":
        public_base_url = prompt_non_empty(
            "Public HTTPS origin (without /mcp)",
            prompt_to_stderr=prompt_to_stderr,
        )
        provider_config_url = prompt_non_empty(
            "OIDC provider config URL", prompt_to_stderr=prompt_to_stderr
        )
        client_id_env = prompt_env_var_name(
            "OIDC client ID env var", DEFAULT_CLIENT_ID_ENV, prompt_to_stderr=prompt_to_stderr
        )
        client_secret_env = prompt_env_var_name(
            "OIDC client secret env var",
            DEFAULT_CLIENT_SECRET_ENV,
            prompt_to_stderr=prompt_to_stderr,
        )
    elif auth_mode == "static_bearer":
        token_env = prompt_env_var_name(
            "Bearer token env var", DEFAULT_TOKEN_ENV, prompt_to_stderr=prompt_to_stderr
        )

    default_project_id = default_project_id_from_cwd()
    project_id = prompt_project_id(default_project_id, prompt_to_stderr=prompt_to_stderr)
    default_name = title_from_project_id(project_id)
    project_name = prompt_non_empty("Project display name", default_name, prompt_to_stderr=prompt_to_stderr)
    project_path = prompt_non_empty("Project path", default_project_path(), prompt_to_stderr=prompt_to_stderr)

    return InitConfig(
        auth_mode=auth_mode,
        public_base_url=public_base_url,
        provider_config_url=provider_config_url,
        client_id_env=client_id_env,
        client_secret_env=client_secret_env,
        token_env=token_env,
        project_id=project_id,
        project_name=project_name,
        project_path=project_path,
    )


def prompt_auth_mode(*, prompt_to_stderr: bool = False) -> AuthMode:
    echo_prompt_text("Authentication mode", prompt_to_stderr=prompt_to_stderr)
    echo_prompt_text("1. local-only development (auto) [default]", prompt_to_stderr=prompt_to_stderr)
    echo_prompt_text("2. public ChatGPT connector (oidc_proxy)", prompt_to_stderr=prompt_to_stderr)
    echo_prompt_text("3. internal/test bearer (static_bearer)", prompt_to_stderr=prompt_to_stderr)
    while True:
        choice = prompt_value("Choose auth mode", "1", prompt_to_stderr=prompt_to_stderr).strip()
        if choice in {"1", "auto"}:
            return "auto"
        if choice in {"2", "oidc_proxy"}:
            return "oidc_proxy"
        if choice in {"3", "static_bearer"}:
            return "static_bearer"
        prompt_error("Choose 1, 2, or 3.", prompt_to_stderr=prompt_to_stderr)


def echo_prompt_text(message: str, *, prompt_to_stderr: bool) -> None:
    click.echo(message, err=prompt_to_stderr)


def prompt_value(label: str, default: str | None, *, prompt_to_stderr: bool) -> str:
    if not prompt_to_stderr:
        return str(click.prompt(label, default=default))

    suffix = f" [{default}]: " if default is not None else ": "
    click.echo(f"{label}{suffix}", nl=False, err=True)
    value = sys.stdin.readline()
    if value == "":
        raise click.Abort()
    value = value.rstrip("\r\n")
    return default if value == "" and default is not None else value


def prompt_error(message: str, *, prompt_to_stderr: bool) -> None:
    click.echo(message, err=prompt_to_stderr)


def prompt_non_empty(
    label: str,
    default: str | None = None,
    *,
    prompt_to_stderr: bool = False,
) -> str:
    while True:
        value = prompt_value(label, default, prompt_to_stderr=prompt_to_stderr).strip()
        if value:
            return value
        prompt_error(f"{label} must not be empty.", prompt_to_stderr=prompt_to_stderr)


def prompt_env_var_name(
    label: str,
    default: str,
    *,
    prompt_to_stderr: bool = False,
) -> str:
    while True:
        value = prompt_non_empty(label, default, prompt_to_stderr=prompt_to_stderr)
        if ENV_VAR_RE.fullmatch(value):
            return value
        prompt_error(ENV_VAR_ERROR, prompt_to_stderr=prompt_to_stderr)


def prompt_project_id(default: str, *, prompt_to_stderr: bool = False) -> str:
    while True:
        value = prompt_non_empty(
            "Project id", default, prompt_to_stderr=prompt_to_stderr
        )
        if re.fullmatch(r"[A-Za-z0-9_-]+", value):
            return value
        prompt_error(
            "Project id may contain only letters, numbers, underscores, and hyphens.",
            prompt_to_stderr=prompt_to_stderr,
        )


def default_project_id_from_cwd() -> str:
    name = Path.cwd().name or "my_project"
    sanitized = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_-")
    return sanitized or "my_project"


def default_project_path() -> str:
    return str(Path.cwd())


def title_from_project_id(project_id: str) -> str:
    words = re.split(r"[_-]+", project_id.strip())
    titled = " ".join(word.capitalize() for word in words if word)
    return titled or "My Project"


def toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\b", "\\b")
    escaped = escaped.replace("\t", "\\t")
    escaped = escaped.replace("\n", "\\n")
    escaped = escaped.replace("\f", "\\f")
    escaped = escaped.replace("\r", "\\r")
    return f'"{escaped}"'


def render_config_toml(config: InitConfig) -> str:
    lines = [
        "[server]",
        'host = "127.0.0.1"',
        "port = 8765",
    ]
    if config.auth_mode == "oidc_proxy":
        lines.append(f"public_base_url = {toml_string(config.public_base_url or '')}")
    lines.extend(
        [
            f"task_dir = {toml_string(DEFAULT_TASK_DIR)}",
            'codex_bin = "codex"',
            f"default_model = {toml_string(DEFAULT_MODEL)}",
            'default_codex_args = ["--json"]',
            "",
            "[auth]",
            f"mode = {toml_string(config.auth_mode)}",
        ]
    )

    if config.auth_mode == "oidc_proxy":
        lines.extend(
            [
                f"provider_config_url = {toml_string(config.provider_config_url or '')}",
                f"client_id_env = {toml_string(config.client_id_env)}",
                f"client_secret_env = {toml_string(config.client_secret_env)}",
            ]
        )
    elif config.auth_mode == "static_bearer":
        lines.extend(
            [
                f"token_env = {toml_string(config.token_env)}",
                'client_id = "local-codex-bridge-static"',
                'required_scopes = ["lcb:read"]',
                'token_scopes = ["lcb:read", "lcb:write"]',
            ]
        )

    project_header = f"[projects.{config.project_id}]"
    verification_header = f"[projects.{config.project_id}.verification]"
    lines.extend(
        [
            "",
            project_header,
            f"name = {toml_string(config.project_name)}",
            f"path = {toml_string(config.project_path)}",
            f"default_model = {toml_string(DEFAULT_MODEL)}",
            "",
            verification_header,
            'git_status = ["git", "status", "--short", "--branch"]',
            "",
        ]
    )
    return "\n".join(lines)


def validate_generated_toml(toml_text: str) -> None:
    with tempfile.TemporaryDirectory(prefix="lcb-init-") as tmpdir:
        temp_config = Path(tmpdir) / "config.toml"
        temp_config.write_text(toml_text, encoding="utf-8")
        try:
            BridgeConfig.load_for_doctor(temp_config)
        except Exception as exc:
            print(f"[red]Generated config failed validation:[/red] {exc}")
            raise typer.Exit(1) from exc


def backup_existing_config(config_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = config_path.with_name(f"{config_path.name}.bak.{timestamp}")
    candidate = base
    suffix = 1
    while candidate.exists():
        candidate = config_path.with_name(f"{config_path.name}.bak.{timestamp}.{suffix}")
        suffix += 1
        if suffix > 100:
            raise RuntimeError("could not create a unique backup path")
    shutil.copy2(config_path, candidate)
    return candidate


def print_next_steps(config_path: Path, config: InitConfig) -> None:
    print("[cyan]Next step:[/cyan]")
    print(f"local-codex-bridge doctor --config {config_path}")
    if config.auth_mode == "oidc_proxy":
        print("[cyan]Set OIDC credentials before serving, using your real values:[/cyan]")
        print(f'export {config.client_id_env}="<your-client-id>"')
        print(f'export {config.client_secret_env}="<your-client-secret>"')
    elif config.auth_mode == "static_bearer":
        print("[cyan]Set the bearer token before serving, using your real value:[/cyan]")
        print(f'export {config.token_env}="<use-a-long-random-value>"')
