from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class ProjectConfig(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    name: str
    path: Path
    default_model: str | None = None
    verification: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("path")
    @classmethod
    def expand_project_path(cls, value: Path) -> Path:
        return Path(value).expanduser().resolve()


class ServerConfig(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    host: str = "127.0.0.1"
    port: int = 8765
    public_base_url: str | None = None
    task_dir: Path = Path("~/.local-codex-bridge/tasks")
    codex_bin: str = "codex"
    default_model: str | None = "gpt-5.5"
    default_codex_args: list[str] = Field(default_factory=lambda: ["--json"])

    @field_validator("public_base_url", mode="before")
    @classmethod
    def normalize_public_base_url(cls, value: Any) -> str | None:
        if value is None:
            return None

        url = str(value).strip()
        if not url:
            return None
        if not url.startswith("https://"):
            raise ValueError("server.public_base_url must start with https://")

        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc or not parsed.hostname:
            raise ValueError("server.public_base_url must be a public HTTPS origin/base URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("server.public_base_url must not include username or password")
        if "/mcp" in parsed.path:
            raise ValueError("server.public_base_url must not include /mcp")
        if parsed.path not in {"", "/"}:
            raise ValueError("server.public_base_url must not include a non-root path")
        if parsed.query:
            raise ValueError("server.public_base_url must not include a query string")
        if parsed.fragment:
            raise ValueError("server.public_base_url must not include a fragment")

        return f"https://{parsed.netloc}"

    @field_validator("task_dir")
    @classmethod
    def expand_task_dir(cls, value: Path) -> Path:
        return Path(value).expanduser().resolve()

    @property
    def is_loopback(self) -> bool:
        return self.host.strip().lower() in LOOPBACK_HOSTS


class AuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    mode: Literal["auto", "disabled", "static_bearer", "oidc_proxy"] = "auto"
    token_env: str = "LCB_AUTH_TOKEN"
    client_id: str = "local-codex-bridge-static"
    required_scopes: list[str] = Field(default_factory=lambda: ["lcb:read"])
    token_scopes: list[str] = Field(default_factory=lambda: ["lcb:read", "lcb:write"])
    provider_config_url: str | None = None
    oidc_scopes: list[str] = Field(default_factory=lambda: ["openid"])
    client_id_env: str = "LCB_OIDC_CLIENT_ID"
    client_secret_env: str = "LCB_OIDC_CLIENT_SECRET"

    @field_validator("token_env", "client_id", "client_id_env", "client_secret_env")
    @classmethod
    def non_empty_string(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("provider_config_url", mode="before")
    @classmethod
    def normalize_provider_config_url(cls, value: Any) -> str | None:
        if value is None:
            return None
        url = str(value).strip()
        if not url:
            return None
        if not url.startswith("https://"):
            raise ValueError("provider_config_url must start with https://")
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc or not parsed.hostname:
            raise ValueError("provider_config_url must be an HTTPS URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("provider_config_url must not include username or password")
        return url

    @field_validator("required_scopes", "token_scopes", "oidc_scopes")
    @classmethod
    def normalize_scopes(cls, value: list[str]) -> list[str]:
        normalized = [scope.strip() for scope in value]
        if not normalized:
            raise ValueError("must not be empty")
        if any(not scope for scope in normalized):
            raise ValueError("must not contain blank scope strings")
        return normalized


class BridgeConfig(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    server: ServerConfig
    projects: dict[str, ProjectConfig]
    auth: AuthConfig = Field(default_factory=AuthConfig)

    @model_validator(mode="after")
    def validate_auth_exposure(self, info: ValidationInfo) -> "BridgeConfig":
        public_base_url = self.server.public_base_url
        mode = self.auth.mode
        skip_runtime_env_checks = bool(
            info.context and info.context.get("skip_runtime_env_checks")
        )

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

        if (
            mode == "static_bearer"
            and not skip_runtime_env_checks
            and not os.environ.get(self.auth.token_env, "").strip()
        ):
            raise ValueError(
                "auth.mode='static_bearer' requires non-empty env var "
                f"{self.auth.token_env}"
            )

        if mode == "oidc_proxy":
            if not public_base_url:
                raise ValueError(
                    "auth.mode='oidc_proxy' requires server.public_base_url "
                    "set to the public HTTPS origin/base URL"
                )
            if not self.auth.provider_config_url:
                raise ValueError(
                    "auth.mode='oidc_proxy' requires provider_config_url "
                    "set to an HTTPS OpenID configuration URL"
                )
            if (
                not skip_runtime_env_checks
                and not os.environ.get(self.auth.client_id_env, "").strip()
            ):
                raise ValueError(
                    "auth.mode='oidc_proxy' requires non-empty env var "
                    f"{self.auth.client_id_env}"
                )
            if (
                not skip_runtime_env_checks
                and not os.environ.get(self.auth.client_secret_env, "").strip()
            ):
                raise ValueError(
                    "auth.mode='oidc_proxy' requires non-empty env var "
                    f"{self.auth.client_secret_env}"
                )

        return self

    @classmethod
    def _reject_token_literals(cls, raw: dict[str, Any]) -> None:
        auth = raw.get("auth")
        if not isinstance(auth, dict):
            return
        literal_keys = {"token", "token_value", "bearer_token", "static_token"}
        if auth.get("mode") == "oidc_proxy":
            literal_keys.update(
                {"client_id", "client_secret", "oidc_client_secret", "oauth_client_secret"}
            )
        else:
            literal_keys.update({"client_secret", "oidc_client_secret", "oauth_client_secret"})

        configured = literal_keys.intersection(auth)
        if configured:
            keys = ", ".join(sorted(configured))
            raise ValueError(
                "auth credential literal fields are not supported in TOML; "
                f"remove {keys} and use token_env or env-var indirection instead"
            )

    @classmethod
    def _load(
        cls,
        path: str | Path,
        *,
        create_task_dir: bool,
        skip_runtime_env_checks: bool,
    ) -> "BridgeConfig":
        config_path = Path(path).expanduser().resolve()
        with config_path.open("rb") as f:
            raw: dict[str, Any] = tomllib.load(f)
        cls._reject_token_literals(raw)
        cfg = cls.model_validate(
            raw,
            context={"skip_runtime_env_checks": skip_runtime_env_checks},
        )
        if create_task_dir:
            cfg.server.task_dir.mkdir(parents=True, exist_ok=True)
        return cfg

    @classmethod
    def load(cls, path: str | Path) -> "BridgeConfig":
        return cls._load(path, create_task_dir=True, skip_runtime_env_checks=False)

    @classmethod
    def load_for_doctor(cls, path: str | Path) -> "BridgeConfig":
        """Load config for diagnostics without checking secret env var presence."""
        return cls._load(path, create_task_dir=False, skip_runtime_env_checks=True)
