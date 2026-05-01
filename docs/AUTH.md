# Authentication

Local Codex Bridge can start local Codex tasks and inspect configured repositories, so authentication is required before any public or persistent endpoint is used. A tunnel is only transport; it is not the security boundary. LCB auth is the security boundary.

Auth is configured in `config.toml` under `[auth]`.

## Recommended public mode: `oidc_proxy`

For public ChatGPT custom MCP use, use FastMCP's built-in OIDC proxy mode:

```toml
[server]
public_base_url = "https://YOUR-REAL-TUNNEL-OR-DOMAIN"

[auth]
mode = "oidc_proxy"
provider_config_url = "https://YOUR-IDP/.well-known/openid-configuration"
client_id_env = "LCB_OIDC_CLIENT_ID"
client_secret_env = "LCB_OIDC_CLIENT_SECRET"
```

Set the OIDC client credentials in the environment before starting the server:

```bash
export LCB_OIDC_CLIENT_ID="your-client-id"
export LCB_OIDC_CLIENT_SECRET="your-client-secret"
local-codex-bridge serve --config ~/.local-codex-bridge/config.toml
```

Do not put OIDC client IDs or client secrets directly in TOML. LCB intentionally uses env-var indirection for credentials and will not print env-derived values in startup output or validation errors.

### What values go where

- `server.public_base_url`: your real HTTPS tunnel/domain, without `/mcp`.
- ChatGPT connector URL: `{public_base_url}/mcp`.
- IdP redirect URI: `{public_base_url}/auth/callback`.
- Env vars: OIDC client ID and OIDC client secret.

`example.com` domains and `YOUR-...` values in this repository are placeholders. They do not exist; replace them with your real tunnel/domain and identity provider values.

`server.public_base_url` is a public HTTPS origin/base only. It must start with `https://`, must not include `/mcp`, must not include a non-root path, query string, or fragment, and is normalized by removing a trailing `/`.

FastMCP's OIDC proxy publishes the OAuth/OIDC support endpoints and uses `/auth/callback` by default. Local Codex Bridge does not implement a native OAuth server.

### Check your setup

Run doctor before starting the server:

```bash
local-codex-bridge doctor --config ~/.local-codex-bridge/config.toml
```

Doctor validates the config without starting MCP, running Codex, constructing FastMCP `OIDCProxy`, or fetching provider metadata. It prints the ChatGPT connector URL, IdP redirect URI, provider config URL, and whether required credential environment variables are set. It prints environment variable names only, never bearer tokens, OIDC client IDs, or OIDC client secrets.

## Local development modes

### `auto` default

```toml
[auth]
mode = "auto"
```

`auto` permits no-auth mode only when the bridge is bound to loopback (`127.0.0.1`, `localhost`, or `::1`) and `server.public_base_url` is absent or empty. If you set a public URL or bind to a non-loopback host, startup fails closed.

### `disabled`

```toml
[auth]
mode = "disabled"
```

`disabled` is explicit no-auth mode. It has the same safety boundary as `auto`: loopback only, and no `server.public_base_url`. Use it only for private local development.

### `static_bearer`

```toml
[auth]
mode = "static_bearer"
token_env = "LCB_AUTH_TOKEN"
client_id = "local-codex-bridge-static"
required_scopes = ["lcb:read"]
token_scopes = ["lcb:read", "lcb:write"]
```

Set the token in the environment before starting the server:

```bash
export LCB_AUTH_TOKEN="use-a-long-random-value"
local-codex-bridge serve --config ~/.local-codex-bridge/config.toml
```

Do not put token literal values in TOML. LCB intentionally supports only env-var indirection for this mode and will not print token values in startup output or validation errors. Unknown `[auth]` fields, empty scope lists, and blank scope strings are rejected.

`static_bearer` is for local, internal, and automated test clients that can send a standard `Authorization: Bearer ...` header. It is not the recommended public ChatGPT custom MCP path.

## Public and tunnel deployments

Do not connect ChatGPT to a public tunnel until LCB auth is configured. Cloudflare Tunnel, ngrok, or any other tunnel forwards HTTPS traffic to the local bridge; LCB itself must enforce authentication.

Query-string tokens are rejected as a design direction. They are easy to leak through logs, browser history, and shared URLs, and they are not the MCP-compatible public target.

Public ChatGPT-compatible deployment should use `auth.mode = "oidc_proxy"` with a real OIDC provider. Local Codex Bridge remains general-purpose and project-agnostic; downstream installations should be configuration only, not forked or project-specific bridge code.
