from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


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
    public_base_url: str | None = None
    task_dir: Path = Path("~/.local-codex-bridge/tasks")
    codex_bin: str = "codex"
    default_model: str | None = "gpt-5.5"
    default_codex_args: list[str] = Field(default_factory=lambda: ["--json"])

    @field_validator("public_base_url", mode="before")
    @classmethod
    def empty_public_base_url_is_none(cls, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return str(value).strip()

    @field_validator("task_dir")
    @classmethod
    def expand_task_dir(cls, value: Path) -> Path:
        return Path(value).expanduser().resolve()

    @property
    def is_loopback(self) -> bool:
        return self.host.strip().lower() in LOOPBACK_HOSTS


class AuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["auto", "disabled", "static_bearer"] = "auto"
    token_env: str = "LCB_AUTH_TOKEN"
    client_id: str = "local-codex-bridge-static"
    required_scopes: list[str] = Field(default_factory=lambda: ["lcb:read"])
    token_scopes: list[str] = Field(default_factory=lambda: ["lcb:read", "lcb:write"])

    @field_validator("token_env", "client_id")
    @classmethod
    def non_empty_string(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("required_scopes", "token_scopes")
    @classmethod
    def normalize_scopes(cls, value: list[str]) -> list[str]:
        normalized = [scope.strip() for scope in value]
        if not normalized:
            raise ValueError("must not be empty")
        if any(not scope for scope in normalized):
            raise ValueError("must not contain blank scope strings")
        return normalized


class BridgeConfig(BaseModel):
    server: ServerConfig
    projects: dict[str, ProjectConfig]
    auth: AuthConfig = Field(default_factory=AuthConfig)

    @model_validator(mode="after")
    def validate_auth_exposure(self) -> "BridgeConfig":
        public_base_url = self.server.public_base_url
        mode = self.auth.mode

        if mode in {"auto", "disabled"}:
            if public_base_url:
                raise ValueError(
                    f"auth.mode='{mode}' is only allowed without server.public_base_url; "
                    "configure auth.mode='static_bearer' for local/internal bearer auth "
                    "or a future OAuth/OIDC mode before public deployment"
                )
            if not self.server.is_loopback:
                raise ValueError(
                    f"auth.mode='{mode}' is only allowed on loopback hosts "
                    "(127.0.0.1, localhost, or ::1); configure auth before binding publicly"
                )

        if mode == "static_bearer" and not os.environ.get(self.auth.token_env, "").strip():
            raise ValueError(
                "auth.mode='static_bearer' requires non-empty env var "
                f"{self.auth.token_env}"
            )

        return self

    @classmethod
    def _reject_token_literals(cls, raw: dict[str, Any]) -> None:
        auth = raw.get("auth")
        if not isinstance(auth, dict):
            return
        literal_keys = {"token", "token_value", "bearer_token", "static_token"}
        configured = literal_keys.intersection(auth)
        if configured:
            keys = ", ".join(sorted(configured))
            raise ValueError(
                "auth token literal fields are not supported in TOML; "
                f"remove {keys} and use token_env instead"
            )

    @classmethod
    def load(cls, path: str | Path) -> "BridgeConfig":
        config_path = Path(path).expanduser().resolve()
        with config_path.open("rb") as f:
            raw: dict[str, Any] = tomllib.load(f)
        cls._reject_token_literals(raw)
        cfg = cls.model_validate(raw)
        cfg.server.task_dir.mkdir(parents=True, exist_ok=True)
        return cfg
