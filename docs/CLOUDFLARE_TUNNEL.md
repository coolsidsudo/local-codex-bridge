# Cloudflare Tunnel operational guide

Cloudflare Tunnel is an external deployment/access layer for Local Codex Bridge. The bridge itself still runs locally and serves MCP on its configured host and port, normally:

```text
http://127.0.0.1:8765/mcp
```

`cloudflared` forwards a public HTTPS hostname to the local bridge origin. Keep that boundary clear: Local Codex Bridge does not depend on Cloudflare, `cloudflared`, ngrok, or any tunnel provider package at Python runtime.

## Security expectations

- Cloudflare Tunnel is transport only; it is not the Local Codex Bridge security boundary.
- For public ChatGPT work, configure LCB `auth.mode = "oidc_proxy"`; LCB auth is the security boundary.
- Do **not** expose Local Codex Bridge unauthenticated on the public internet.
- `static_bearer` auth is for local/internal/test clients only and is not the recommended public ChatGPT-compatible mode.
- Do **not** commit tunnel URLs, Cloudflare credentials, service tokens, certs, tokens, or tunnel credential JSON files.
- Treat the tunnel hostname as operational configuration for a specific environment, not as project source code.

## What auth values go where

For `auth.mode = "oidc_proxy"`:

- `server.public_base_url`: your real HTTPS Cloudflare/ngrok/other tunnel domain, without `/mcp`.
- ChatGPT connector URL: `{public_base_url}/mcp`.
- IdP redirect URI: `{public_base_url}/auth/callback`.
- Env vars: OIDC client ID and client secret.

## Setup outline

This outline uses a locally managed named tunnel. Adjust commands for your operating system and Cloudflare account policy.

1. Install `cloudflared`.

   ```bash
   cloudflared --version
   ```

2. Authenticate `cloudflared` with your Cloudflare account.

   ```bash
   cloudflared tunnel login
   ```

3. Create a named tunnel.

   ```bash
   cloudflared tunnel create local-codex-bridge
   ```

   Save the generated tunnel UUID. `cloudflared` also creates a credentials JSON file under your local `.cloudflared` directory.

4. Create a DNS route / public hostname for the tunnel.

   ```bash
   cloudflared tunnel route dns TUNNEL_UUID local-codex-bridge.example.com
   ```

5. Configure ingress so the public hostname forwards to the local bridge origin, normally `http://127.0.0.1:8765`.

6. Start Local Codex Bridge locally in a separate terminal and confirm that the local endpoint responds.

   ```bash
   curl -i -H "Accept: text/event-stream" http://127.0.0.1:8765/mcp
   ```

7. Run the tunnel.

   ```bash
   cloudflared tunnel --config /Users/<you>/.cloudflared/config.yml run TUNNEL_UUID
   ```

8. Configure LCB `auth.mode = "oidc_proxy"`, then use the resulting HTTPS URL plus `/mcp` as the ChatGPT custom MCP connector URL. Use the HTTPS hostname without `/mcp` as `server.public_base_url`, and register `{public_base_url}/auth/callback` as the IdP redirect URI.

   ```text
   https://local-codex-bridge.example.com/mcp
   ```

   `example.com` and `YOUR-...` values are placeholders and do not exist; replace them with real values.

9. Test remote reachability.

   ```bash
   curl -i -H "Accept: text/event-stream" https://local-codex-bridge.example.com/mcp
   ```

   Plain `curl` is not a full MCP client, so `400`, `406`, or another MCP protocol error can still prove that the HTTPS hostname reaches the bridge. A real MCP client performs the complete protocol/session flow.

## Example `cloudflared` config

The following is an example only. Use placeholders in docs and repositories; do not commit real tunnel UUIDs, hostnames, account credentials, tokens, certificates, or credential files.

```yaml
# /Users/<you>/.cloudflared/config.yml
tunnel: TUNNEL_UUID
credentials-file: /Users/<you>/.cloudflared/TUNNEL_UUID.json

ingress:
  - hostname: local-codex-bridge.example.com
    service: http://127.0.0.1:8765
  - service: http_status:404
```

Run it with:

```bash
cloudflared tunnel --config /Users/<you>/.cloudflared/config.yml run TUNNEL_UUID
```

## Troubleshooting

### Bridge not running

If the tunnel returns a Cloudflare error or the connector cannot reach the server, verify that Local Codex Bridge is running locally first:

```bash
curl -i -H "Accept: text/event-stream" http://127.0.0.1:8765/mcp
```

Start the bridge before starting or testing the tunnel.

### Wrong local port

The default local origin is `http://127.0.0.1:8765`, but profiles or launch commands may use another port. Make the `cloudflared` ingress `service` match the bridge's configured host and port.

### Missing `/mcp` path

The ChatGPT custom MCP connector URL should include `/mcp`:

```text
https://local-codex-bridge.example.com/mcp
```

Using only the hostname may reach Cloudflare but not the MCP endpoint.

### Cloudflare Access blocks the connector

Cloudflare Access / Zero Trust protection can be useful, but it is not a replacement for LCB auth. The connector must be able to satisfy the configured auth policy. Do not disable LCB authentication for public exposure. Static bearer is not the recommended public ChatGPT-compatible mode; use `auth.mode = "oidc_proxy"` for public connector use.

### ChatGPT developer MCP `FORBIDDEN`

Errors such as:

```text
FORBIDDEN: This conversation does not support developer MCPs
```

are usually ChatGPT platform/conversation gating. Treat them separately from Cloudflare Tunnel and bridge runtime health unless repository evidence proves otherwise.

### `curl` returns `400` or `406`

Plain `curl` is not a complete MCP client. An HTTP `400`, `406`, or similar JSON-RPC/MCP protocol error can be normal if it shows that the request reached Local Codex Bridge. Use bridge logs and the ChatGPT connector behavior for end-to-end validation.
