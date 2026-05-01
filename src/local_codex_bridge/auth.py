from __future__ import annotations

import os

from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from .config import BridgeConfig


def build_auth_provider(config: BridgeConfig) -> StaticTokenVerifier | None:
    """Build the FastMCP auth provider for the configured auth mode.

    Slice 1 supports explicit local no-auth and static bearer auth only. OAuth/OIDC
    proxy support is intentionally deferred to a later implementation slice.
    """
    auth = config.auth

    if auth.mode in {"auto", "disabled"}:
        return None

    if auth.mode == "static_bearer":
        token = os.environ.get(auth.token_env, "").strip()
        if not token:
            # BridgeConfig validation should catch this before callers get here,
            # but keep this guard close to token use as defense in depth.
            raise ValueError(f"auth.mode='static_bearer' requires non-empty env var {auth.token_env}")

        return StaticTokenVerifier(
            tokens={
                token: {
                    "client_id": auth.client_id,
                    "scopes": auth.token_scopes,
                }
            },
            required_scopes=auth.required_scopes,
        )

    raise ValueError(f"Unsupported auth mode: {auth.mode}")
