# 面向 Codex 的 Burp MCP stdio bridge

[English](README.md)

这个项目通过一个轻量的 stdio bridge，让 Codex 可以使用 Burp Suite 的 MCP 工具。

Burp 的 MCP 扩展在 `http://127.0.0.1:9876/` 暴露的是 legacy HTTP+SSE endpoint。Codex 的 URL 型 MCP 配置期望的是 Streamable HTTP，因此直接把这个 URL 配进 Codex 时，Burp 工具通常无法可靠加载。这个 bridge 让 Codex 继续使用原生 `stdio` MCP transport，并在内部把请求转换到 Burp 的 legacy SSE session。

## 文件

- `burpsuite_stdio_bridge.py` - Codex 使用的 stdio MCP bridge。
- `burpsuite_stdio_bridge.json` - bridge 的运行时配置。

## Codex 配置

`~/.codex/config.toml` 中的 MCP 配置应类似下面这样：

```toml
[mcp_servers.burpsuite_stdio]
type = "stdio"
command = "/Users/aa8j/tools/burpMCPtoCodex/burpsuite_stdio_bridge.py"
```

使用下面的命令确认配置：

```bash
codex mcp get burpsuite_stdio
codex mcp list
```

预期结果：

```text
burpsuite_stdio
  enabled: true
  transport: stdio
  command: /Users/aa8j/tools/burpMCPtoCodex/burpsuite_stdio_bridge.py
```

修改 MCP 配置后，需要重新打开一个 Codex 窗口。已经打开的 Codex 会话不会动态获得新配置的 MCP 工具。

## Burp 要求

1. 启动 Burp Suite。
2. 确认 Burp MCP 扩展已启用。
3. 确认 MCP endpoint 正在监听 `127.0.0.1:9876`。

快速检查：

```bash
curl -sS --no-buffer --max-time 5 -H 'Accept: text/event-stream' http://127.0.0.1:9876/
```

可用的 endpoint 会输出类似下面的 `endpoint` 事件：

```text
event: endpoint
data: ?sessionId=...
```

## Bridge 配置

`burpsuite_stdio_bridge.json`：

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

配置项说明：

- `upstream` - Burp MCP legacy SSE URL。
- `endpoint_timeout` - 等待 Burp 返回 session POST endpoint 的秒数。
- `post_timeout` - 向 Burp POST JSON-RPC 请求时的等待秒数。
- `rpc_timeout` - 等待匹配 SSE 响应的秒数。
- `reconnect_attempts` - 请求或 session 失败后的重试次数。
- `quiet` - 为 `true` 时抑制 bridge 写到 stderr 的日志。

如果 Burp 工具执行较慢，优先调大 `rpc_timeout`。如果是通过 Burp 发送的 HTTP 请求本身较慢，同时调大 `post_timeout` 和 `rpc_timeout`。

## 手动冒烟测试

运行 bridge：

```bash
./burpsuite_stdio_bridge.py
```

粘贴下面几行 JSON-RPC：

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"manual-test","version":"0.1"}}}
{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"base64_encode","arguments":{"content":"codex-test"}}}
```

预期行为：

- `initialize` 返回 `serverInfo.name = "burp-suite"`。
- `tools/list` 返回 Burp 工具，例如 `send_http1_request`、`get_proxy_http_history`、`create_repeater_tab` 和 `base64_encode`。
- `base64_encode` 返回 `Y29kZXgtdGVzdA==`。

## Burp 侧已知限制

- `set_project_options` 和 `set_user_options` 需要先在 Burp 的 MCP tab 中启用配置编辑工具。
- `get_active_editor_contents` 和 `set_active_editor_contents` 需要 Burp 当前存在活动的、可编辑的 request/response 编辑器。
- Collaborator 相关工具要求当前 Burp 环境可用 Burp Collaborator。

## 排障

如果 Codex 看不到工具：

1. 运行 `codex mcp get burpsuite_stdio`，确认显示 `transport: stdio`。
2. 修改配置后重新打开 Codex 窗口。
3. 确认 Burp 正在监听 `127.0.0.1:9876`。
4. 临时把 `burpsuite_stdio_bridge.json` 中的 `"quiet"` 改成 `false`，重启 Codex 查看 bridge 日志。

如果请求超时：

1. 调大 `rpc_timeout`。
2. 对通过 Burp 发送的慢请求，调大 `post_timeout`。
3. 检查 Burp 的 MCP 扩展是否仍然启用且响应正常。

如果手动测试可用，但 Codex 中不可用，重启 Codex 窗口，让它重新拉起 stdio bridge。
