from __future__ import annotations

import os

from fastmcp.server.auth.oidc_proxy import OIDCProxy
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from .config import BridgeConfig


def build_auth_provider(config: BridgeConfig) -> StaticTokenVerifier | OIDCProxy | None:
    """Build the FastMCP auth provider for the configured auth mode."""
    auth = config.auth

    if auth.mode in {"auto", "disabled"}:
        return None

    if auth.mode == "static_bearer":
        token = os.environ.get(auth.token_env, "").strip()
        if not token:
            # BridgeConfig validation should catch this before callers get here,
            # but keep this guard close to token use as defense in depth.
            raise ValueError(
                f"auth.mode='static_bearer' requires non-empty env var {auth.token_env}"
            )

        return StaticTokenVerifier(
            tokens={
                token: {
                    "client_id": auth.client_id,
                    "scopes": auth.token_scopes,
                }
            },
            required_scopes=auth.required_scopes,
        )

    if auth.mode == "oidc_proxy":
        client_id = os.environ.get(auth.client_id_env, "").strip()
        client_secret = os.environ.get(auth.client_secret_env, "").strip()
        if not client_id:
            raise ValueError(
                f"auth.mode='oidc_proxy' requires non-empty env var {auth.client_id_env}"
            )
        if not client_secret:
            raise ValueError(
                f"auth.mode='oidc_proxy' requires non-empty env var {auth.client_secret_env}"
            )
        if not auth.provider_config_url or not config.server.public_base_url:
            raise ValueError("auth.mode='oidc_proxy' requires OIDC config and public base URL")

        return OIDCProxy(
            config_url=auth.provider_config_url,
            client_id=client_id,
            client_secret=client_secret,
            base_url=config.server.public_base_url,
            required_scopes=auth.oidc_scopes,
        )

    raise ValueError(f"Unsupported auth mode: {auth.mode}")
