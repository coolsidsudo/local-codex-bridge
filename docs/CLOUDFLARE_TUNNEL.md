# Cloudflare Tunnel operational guide

Cloudflare Tunnel is an external deployment/access layer for Local Codex Bridge. The bridge itself still runs locally and serves MCP on its configured host and port, normally:

```text
http://127.0.0.1:8765/mcp
```

`cloudflared` forwards a public HTTPS hostname to the local bridge origin. Keep that boundary clear: Local Codex Bridge does not depend on Cloudflare, `cloudflared`, ngrok, or any tunnel provider package at Python runtime.

## Security expectations

- Cloudflare Tunnel is transport only; it is not the Local Codex Bridge security boundary.
- Keep the current Cloudflare hostname disabled / unused for real work until LCB auth is configured.
- Do **not** connect ChatGPT to a public tunnel until LCB auth is configured.
- Do **not** expose Local Codex Bridge unauthenticated on the public internet.
- Slice 1 `static_bearer` auth is for local/internal/test clients and does not complete the public ChatGPT-compatible auth story.
- Public deployment should use the planned OAuth/OIDC proxy mode in a later slice.
- Do **not** commit tunnel URLs, Cloudflare credentials, service tokens, certs, tokens, or tunnel credential JSON files.
- Treat the tunnel hostname as operational configuration for a specific environment, not as project source code.

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

8. After LCB OAuth/OIDC proxy auth is implemented and configured in a later slice, use the resulting HTTPS URL plus `/mcp` as the ChatGPT custom MCP connector URL. Do not use this public endpoint for real ChatGPT work before then.

   ```text
   https://local-codex-bridge.example.com/mcp
   ```

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

Cloudflare Access / Zero Trust protection can be useful, but it is not a replacement for LCB auth. The connector must be able to satisfy the configured auth policy. Do not disable LCB authentication for public exposure. Static bearer is not the planned public ChatGPT-compatible mode; use the later OAuth/OIDC proxy mode for public connector use.

### ChatGPT developer MCP `FORBIDDEN`

Errors such as:

```text
FORBIDDEN: This conversation does not support developer MCPs
```

are usually ChatGPT platform/conversation gating. Treat them separately from Cloudflare Tunnel and bridge runtime health unless repository evidence proves otherwise.

### `curl` returns `400` or `406`

Plain `curl` is not a complete MCP client. An HTTP `400`, `406`, or similar JSON-RPC/MCP protocol error can be normal if it shows that the request reached Local Codex Bridge. Use bridge logs and the ChatGPT connector behavior for end-to-end validation.
