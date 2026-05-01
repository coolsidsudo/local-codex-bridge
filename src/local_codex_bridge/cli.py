from __future__ import annotations

from pathlib import Path

import typer
from rich import print

from .config import BridgeConfig
from .server import build_mcp
from .task_runner import TaskRunner

app = typer.Typer(help="Local Codex Bridge")


@app.command()
def serve(
    config: Path = typer.Option(
        Path("~/.local-codex-bridge/config.toml"),
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
def status(
    project_id: str,
    config: Path = typer.Option(Path("~/.local-codex-bridge/config.toml"), "--config", "-c"),
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
    config: Path = typer.Option(Path("~/.local-codex-bridge/config.toml"), "--config", "-c"),
) -> None:
    """Create a task record without starting Codex."""
    cfg = BridgeConfig.load(config)
    runner = TaskRunner(cfg)
    prompt = prompt_file.read_text(encoding="utf-8")
    print(runner.start_codex_task(project_id, prompt, model=model, dry_run=True))


if __name__ == "__main__":
    app()
