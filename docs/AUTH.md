# Authentication

Local Codex Bridge can start local Codex tasks and inspect configured repositories, so authentication is required before any public or persistent endpoint is used. A tunnel is only transport; it is not the security boundary.

## Slice 1 auth modes

Auth is configured in `config.toml` under `[auth]`.

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

`static_bearer` is for local, internal, and automated test clients that can send a standard `Authorization: Bearer ...` header. It is not the recommended public ChatGPT custom MCP path and does not complete the public ChatGPT-compatible auth story.

## Public and tunnel deployments

Do not connect ChatGPT to a public tunnel until LCB auth is configured. Cloudflare Tunnel, ngrok, or any other tunnel forwards HTTPS traffic to the local bridge; LCB itself must enforce authentication.

Query-string tokens are rejected as a design direction. They are easy to leak through logs, browser history, and shared URLs, and they are not the MCP-compatible public target.

## Planned public ChatGPT-compatible mode

A later slice should add OAuth/OIDC proxy support using FastMCP built-in auth. That is the intended public deployment mode for ChatGPT custom MCP connectors. Local Codex Bridge should remain general-purpose and project-agnostic; downstream installations should be configuration only, not forked or project-specific bridge code.
