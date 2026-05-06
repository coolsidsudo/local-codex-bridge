from __future__ import annotations

import os
from pathlib import Path

import typer
from rich import print

from .config import BridgeConfig
from .init_wizard import run_init_wizard
from .server import build_mcp
from .task_runner import TaskRunner

app = typer.Typer(help="Local Codex Bridge")
DEFAULT_CONFIG = Path("~/.local-codex-bridge/config.toml")


def _env_status(name: str) -> tuple[str, bool]:
    is_set = bool(os.environ.get(name, "").strip())
    return ("set" if is_set else "missing", is_set)


def _contains_placeholder(*values: str | None) -> bool:
    placeholders = ("YOUR-", "example.com", "example.test")
    return any(
        marker in value
        for value in values
        if value is not None
        for marker in placeholders
    )


def _print_env_status(label: str, env_name: str) -> bool:
    status, is_set = _env_status(env_name)
    color = "green" if is_set else "red"
    print(f"[cyan]{label}:[/cyan] {env_name} ([{color}]{status}[/{color}])")
    return is_set


@app.command("init")
def init_config(
    config: Path = typer.Option(
        DEFAULT_CONFIG,
        "--config",
        "-c",
        help="Path to write bridge config TOML.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print generated TOML without writing files.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite an existing config after creating a timestamped backup.",
    ),
) -> None:
    """Interactively create a Local Codex Bridge config file."""
    run_init_wizard(config, dry_run=dry_run, force=force)


@app.command()
def serve(
    config: Path = typer.Option(
        DEFAULT_CONFIG,
        "--config",
        "-c",
        help="Path to bridge config TOML.",
    ),
) -> None:
    """Start the MCP server over streamable HTTP."""
    cfg = BridgeConfig.load(config)
    mcp = build_mcp(cfg)
    print(f"[green]Starting Local Codex Bridge[/green] on {cfg.server.host}:{cfg.server.port}")
    print(f"[cyan]Auth mode:[/cyan] {cfg.auth.mode}")
    if cfg.server.public_base_url:
        print(f"[cyan]Public base URL:[/cyan] {cfg.server.public_base_url}")
    if cfg.auth.mode in {"auto", "disabled"}:
        print("[yellow]No authentication is enabled; this mode is for loopback local/private development only.[/yellow]")
    else:
        print("[green]Authentication is enabled for the MCP endpoint.[/green]")
    # FastMCP v2 supports streamable HTTP transport.
    # If your installed FastMCP version uses a different API, run:
    #   python -c "import fastmcp; print(fastmcp.__version__)"
    mcp.run(transport="streamable-http", host=cfg.server.host, port=cfg.server.port)


@app.command()
def doctor(
    config: Path = typer.Option(
        DEFAULT_CONFIG,
        "--config",
        "-c",
        help="Path to bridge config TOML.",
    ),
) -> None:
    """Check auth/setup config without starting the MCP server."""
    config_path = config.expanduser().resolve()
    cfg = BridgeConfig.load_for_doctor(config_path)
    auth = cfg.auth
    public_base_url = cfg.server.public_base_url
    ok = True

    print("[bold]Local Codex Bridge doctor[/bold]")
    print(f"[cyan]Config:[/cyan] {config_path}")
    print(f"[cyan]Auth mode:[/cyan] {auth.mode}")
    print(f"[cyan]Server bind:[/cyan] {cfg.server.host}:{cfg.server.port}")
    print(f"[cyan]Public base URL:[/cyan] {public_base_url or '(not configured)'}")

    if auth.mode == "oidc_proxy":
        print(f"[cyan]ChatGPT connector URL:[/cyan] {public_base_url}/mcp")
        print(f"[cyan]IdP redirect URI:[/cyan] {public_base_url}/auth/callback")
        print(f"[cyan]Provider config URL:[/cyan] {auth.provider_config_url}")
        print(f"[cyan]OIDC scopes:[/cyan] {' '.join(auth.oidc_scopes)}")
        ok = _print_env_status("OIDC client ID env var", auth.client_id_env) and ok
        ok = _print_env_status("OIDC client secret env var", auth.client_secret_env) and ok
        if _contains_placeholder(
            public_base_url,
            auth.provider_config_url,
            auth.client_id_env,
            auth.client_secret_env,
        ):
            print(
                "[yellow]Placeholder warning:[/yellow] replace YOUR-..., "
                "example.com, or example.test values before public use."
            )
    elif auth.mode == "static_bearer":
        ok = _print_env_status("Bearer token env var", auth.token_env) and ok
        print(
            "[yellow]Static bearer is for local/internal/test clients only; "
            "oidc_proxy is the recommended public ChatGPT path.[/yellow]"
        )
    else:
        local_only_ok = cfg.server.is_loopback and public_base_url is None
        color = "green" if local_only_ok else "red"
        print(f"[cyan]No-auth local-only status:[/cyan] [{color}]{local_only_ok}[/{color}]")
        print("[yellow]Do not expose auto/disabled auth mode publicly.[/yellow]")
        ok = local_only_ok and ok

    raise typer.Exit(0 if ok else 1)


@app.command()
def status(
    project_id: str,
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
) -> None:
    """Print project git status using the same config as the MCP server."""
    cfg = BridgeConfig.load(config)
    runner = TaskRunner(cfg)
    print(runner.project_status(project_id))


@app.command()
def dry_run_task(
    project_id: str,
    prompt_file: Path,
    model: str | None = None,
    review_contract: bool = typer.Option(
        False,
        "--review-contract",
        help="Append the Local Codex Bridge review contract footer to the task prompt.",
    ),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
) -> None:
    """Create a task record without starting Codex."""
    cfg = BridgeConfig.load(config)
    runner = TaskRunner(cfg)
    prompt = prompt_file.read_text(encoding="utf-8")
    print(
        runner.start_codex_task(
            project_id,
            prompt,
            model=model,
            dry_run=True,
            review_contract=review_contract,
        )
    )


if __name__ == "__main__":
    app()
