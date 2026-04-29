from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


class ProjectConfig(BaseModel):
    name: str
    path: Path
    default_model: str | None = None
    verification: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("path")
    @classmethod
    def expand_project_path(cls, value: Path) -> Path:
        return Path(value).expanduser().resolve()


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    task_dir: Path = Path("~/.local-codex-bridge/tasks")
    codex_bin: str = "codex"
    default_model: str | None = "gpt-5.5"
    default_codex_args: list[str] = Field(default_factory=lambda: ["--json"])

    @field_validator("task_dir")
    @classmethod
    def expand_task_dir(cls, value: Path) -> Path:
        return Path(value).expanduser().resolve()


class BridgeConfig(BaseModel):
    server: ServerConfig
    projects: dict[str, ProjectConfig]

    @classmethod
    def load(cls, path: str | Path) -> "BridgeConfig":
        config_path = Path(path).expanduser().resolve()
        with config_path.open("rb") as f:
            raw: dict[str, Any] = tomllib.load(f)
        cfg = cls.model_validate(raw)
        cfg.server.task_dir.mkdir(parents=True, exist_ok=True)
        return cfg
