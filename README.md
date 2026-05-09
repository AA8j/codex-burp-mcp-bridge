# Burp MCP stdio bridge for Codex

[中文文档](README.zh-CN.md)

This project lets Codex use Burp Suite's MCP tools through a small stdio bridge.

Burp's MCP extension exposes a legacy HTTP+SSE endpoint on `http://127.0.0.1:9876/`. Codex's URL-based MCP configuration expects Streamable HTTP, so direct URL configuration does not load the Burp tools reliably. The bridge keeps Codex on its native `stdio` MCP transport and translates requests to Burp's legacy SSE session internally.

## Files

- `codex_burp_mcp_bridge.py` - stdio MCP bridge used by Codex.
- `codex_burp_mcp_bridge.json` - runtime configuration for the bridge.

## Codex configuration

The MCP entry should look like this in `~/.codex/config.toml`:

```toml
[mcp_servers.burp_mcp]
type = "stdio"
command = "/absolute/path/to/codex-burp-mcp-bridge/codex_burp_mcp_bridge.py"
```

Verify it with:

```bash
codex mcp get burp_mcp
codex mcp list
```

Expected result:

```text
burp_mcp
  enabled: true
  transport: stdio
  command: /absolute/path/to/codex-burp-mcp-bridge/codex_burp_mcp_bridge.py
```

Open a new Codex window after changing MCP configuration. Existing Codex sessions do not dynamically gain newly configured MCP tools.

## Burp requirements

1. Start Burp Suite.
2. Make sure the Burp MCP extension is enabled.
3. Confirm the MCP endpoint is listening on `127.0.0.1:9876`.

Quick check:

```bash
curl -sS --no-buffer --max-time 5 -H 'Accept: text/event-stream' http://127.0.0.1:9876/
```

A working endpoint emits an `endpoint` event similar to:

```text
event: endpoint
data: ?sessionId=...
```

## Bridge configuration

`codex_burp_mcp_bridge.json`:

```json
{
  "upstream": "http://127.0.0.1:9876/",
  "endpoint_timeout": 10.0,
  "post_timeout": 120.0,
  "rpc_timeout": 180.0,
  "reconnect_attempts": 1,
  "quiet": true
}
```

Options:

- `upstream` - Burp MCP legacy SSE URL.
- `endpoint_timeout` - seconds to wait for Burp to announce the session POST endpoint.
- `post_timeout` - seconds to wait while posting JSON-RPC requests to Burp.
- `rpc_timeout` - seconds to wait for the matching SSE response.
- `reconnect_attempts` - retry count after a failed request/session.
- `quiet` - suppress bridge logs on stderr when true.

For slow Burp tools, increase `rpc_timeout` first. For slow HTTP requests made through Burp, increase both `post_timeout` and `rpc_timeout`.

## Manual smoke test

Run the bridge:

```bash
./codex_burp_mcp_bridge.py
```

Paste these JSON-RPC lines:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"manual-test","version":"0.1"}}}
{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"base64_encode","arguments":{"content":"codex-test"}}}
```

Expected behavior:

- `initialize` returns `serverInfo.name = "burp-suite"`.
- `tools/list` returns Burp tools such as `send_http1_request`, `get_proxy_http_history`, `create_repeater_tab`, and `base64_encode`.
- `base64_encode` returns `Y29kZXgtdGVzdA==`.

## Known Burp-side limits

- `set_project_options` and `set_user_options` require enabling config-editing tools in Burp's MCP tab.
- `get_active_editor_contents` and `set_active_editor_contents` require an active editable Burp request/response editor.
- Collaborator tools require Burp Collaborator to be available in the current Burp setup.

## Troubleshooting

If Codex cannot see the tools:

1. Run `codex mcp get burp_mcp` and confirm `transport: stdio`.
2. Open a new Codex window after config changes.
3. Confirm Burp is listening on `127.0.0.1:9876`.
4. Temporarily set `"quiet": false` in `codex_burp_mcp_bridge.json` and restart Codex to see bridge logs.

If requests time out:

1. Increase `rpc_timeout`.
2. Increase `post_timeout` for slow requests sent through Burp.
3. Check whether Burp's MCP extension is still enabled and responsive.

If a request works manually but not in Codex, restart the Codex window so it respawns the stdio bridge.
