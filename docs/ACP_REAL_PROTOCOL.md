# CodeBuddy ACP — Verified Protocol Reference

This document is **ground truth** captured from live probes against a real
`codebuddy` daemon (CLI version 2.106.3) on 2026-06-15. The previous ACP
backend (commit 847148f) was written without verifying any of this and is
incompatible with the real daemon.

**DO NOT GUESS.** Every fact in this document was reproduced via curl.

---

## 1. Spawning the daemon

```bash
codebuddy --acp --acp-transport streamable-http -y
```

- Mode `--serve` is a Web UI HTTP server, **NOT ACP**. Do not use it.
- The default `--acp-transport` is `stdio`. We need `streamable-http`.
- The `--port` flag is **ignored** in `--acp-transport streamable-http`;
  the daemon auto-assigns a random ephemeral port.
- The daemon prints exactly one line on stdout:

  ```
  ACP streamable-http endpoint: http://127.0.0.1:<PORT>/api/v1/acp
  ```

  This must be parsed to learn the port. There is no other discovery API.
- `-y` (alias of `--dangerously-skip-permissions`) lets us skip permission
  prompts when the agent uses tools. Keep it on for daemon mode.

Optional but useful flags:
- `--add-dir <abs path>` — additional roots the agent may operate on.
- `--system-prompt <text>` or `--system-prompt-file <path>`.
- `--mcp-config <fileOrString>` — MCP servers, JSON.

The daemon exits on its own when the parent process closes its stdio.
Always launch it as a child of a long-lived supervisor and pipe stdout
so we can read the endpoint line.

---

## 2. Connection handshake

### 2.1 `POST /api/v1/acp/connect`

```
POST http://127.0.0.1:<PORT>/api/v1/acp/connect
Content-Type: application/json
{}
```

Response (200):

```json
{
  "connectionId": "7a9f06fd-551b-4449-8221-28e0321f8c6d",
  "sessionToken": "8iSxWrcI7sJizlZrXgyF8am0nX_GE7a7h8fWe_bDY0U"
}
```

`connectionId` and `sessionToken` are required headers on every later request.

### 2.2 All JSON-RPC traffic goes to one endpoint

```
POST http://127.0.0.1:<PORT>/api/v1/acp
Content-Type: application/json
Accept: application/json, text/event-stream
acp-connection-id: <connectionId>
acp-session-token: <sessionToken>
```

Both `Accept` types **must** be present. Sending only `application/json`
returns:

```
{"jsonrpc":"2.0","error":{"code":-32000,"message":"Not Acceptable: Client must accept both application/json and text/event-stream"},"id":null}
```

The response body is **always** `text/event-stream` (SSE). Each event is:

```
event: message
data: <json>
\n
```

The first line of any successful response is `:ok` (an SSE comment) — this
is a keep-alive sentinel, ignore it.

### 2.3 Headers Codex got wrong (for reference)

The previous backend tried to send these on ACP traffic. Don't:
- `x-codebuddy-request: 1` — only used on Web UI admin endpoints.
- `Authorization: Bearer ck_*` — never goes to ACP. Auth is via
  `authenticate` JSON-RPC method (see §4) or pre-warm via the CLI.

---

## 3. JSON-RPC methods

All requests use JSON-RPC 2.0:

```json
{"jsonrpc":"2.0","id":<int>,"method":"<method>","params":{...}}
```

Responses arrive as SSE `data:` events. Some methods return one result
event; `session/prompt` streams many `session/update` notifications then
finally a result event with `stopReason`.

### 3.1 `initialize` (verified)

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {
    "protocolVersion": 1,
    "clientCapabilities": {
      "fs": {"readTextFile": false, "writeTextFile": false}
    }
  }
}
```

Result:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocolVersion": 1,
    "agentCapabilities": {
      "promptCapabilities": {"image": true, "embeddedContext": true},
      "mcpCapabilities": {"http": true, "sse": true},
      "loadSession": true,
      "delegateToolsSupport": true
    },
    "authMethods": [
      {"id": "iOA", "name": "Login with iOA", "description": null},
      {"id": "external", "name": "Login with Google/Github", "description": null},
      {"id": "internal", "name": "Login with WeChat", "description": null},
      {"id": "selfhosted", "name": "Login with Enterprise Domain", "description": null}
    ]
  }
}
```

### 3.2 `authenticate` (NOT YET FULLY MAPPED)

If the daemon hasn't inherited credentials from the CLI's
`~/.codebuddy/local_storage/`, `session/new` returns:

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "error": {"code": -32000, "message": "Authentication required", "data": {"category": "auth"}}
}
```

We must first call `authenticate`. Calling with `methodId: "external"`
returns an SSE notification:

```
data: {"jsonrpc":"2.0","method":"_codebuddy.ai/authUrl","params":{"authUrl":"https://www.codebuddy.ai/login?platform=CLI&state=<uuid>","provider":"external"}}
```

That URL needs a real browser to complete OAuth. **For Kurumi Proxy we
should treat fresh-spawn auth as a deployment prerequisite**: ensure the
operator has logged in once via `codebuddy` interactively; the daemon
inherits the session from `~/.codebuddy/local_storage/`.

If `session/new` returns the auth-required error, surface it to the
HTTP caller as `503 Service Unavailable` with a clear message:
"CodeBuddy daemon not authenticated — run `codebuddy` interactively
once to log in".

### 3.3 `session/new` (verified)

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "session/new",
  "params": {"cwd": "/tmp", "mcpServers": []}
}
```

When the daemon is authenticated, the result event returns
`{sessionId, models, modes, configOptions}`. Many `session/update`
notifications arrive **before** the result (config options, available
commands, workspace info). Buffer them or ignore them — the consumer
only needs `sessionId`.

### 3.4 `session/prompt` (verified)

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "session/prompt",
  "params": {
    "sessionId": "<id from session/new>",
    "prompt": [{"type": "text", "text": "Reply with ONE word: PONG"}]
  }
}
```

This streams many `session/update` notifications, then a final result
event with `{stopReason}`. Update kinds we observed:

- `sessionUpdate=session_info_update` — phase tracker (`idle`,
  `preparing`, `model_requesting`, `model_streaming`).
- `sessionUpdate=usage_update` — `{used, size}` token counts.
- `sessionUpdate=agent_thought_chunk` — `{content: {type:"text", text}}`.
- `sessionUpdate=agent_message_chunk` — `{content: {type:"text", text}}`.
- `sessionUpdate=tool_call` — initial tool call announcement.
- `sessionUpdate=tool_call_update` — tool call result/progress.

The previous translator expected `agent_message_delta` and `tool_use`;
both wrong. Use the names above.

### 3.5 `stopReason` values

Confirmed values seen in the wild:
- `end_turn` — normal completion.
- `refusal` — agent refused, often because of upstream auth/quota.
  `_meta.codebuddy.ai/errorMessage` carries the upstream message.
  Surface as HTTP 502 with the message text.
- `max_tokens` — context budget hit.
- `cancelled` — client cancelled.
- `tool_use` — agent wants the client to execute a tool. (We don't
  use this path because we surface tool calls directly to the OpenAI
  client.)

---

## 4. SSE parsing

```python
def parse_sse(stream: Iterator[bytes]) -> Iterator[dict]:
    buf = b""
    for chunk in stream:
        buf += chunk
        while b"\n\n" in buf:
            block, buf = buf.split(b"\n\n", 1)
            event = None
            data_lines = []
            for line in block.split(b"\n"):
                if line.startswith(b":"):
                    continue           # SSE comment, e.g. ":ok"
                if line.startswith(b"event:"):
                    event = line[6:].strip().decode()
                elif line.startswith(b"data:"):
                    data_lines.append(line[5:].lstrip().decode())
            if data_lines:
                payload = "".join(data_lines)
                yield {"event": event or "message", "data": json.loads(payload)}
```

---

## 5. Lifecycle for Kurumi Proxy

Single persistent daemon per proxy process. On startup:

1. Spawn `codebuddy --acp --acp-transport streamable-http -y` as a child
   process. Capture stdout line-by-line until we see
   `ACP streamable-http endpoint: http://127.0.0.1:<PORT>/api/v1/acp`.
   If that line doesn't appear within 10s, kill and raise.
2. `POST /api/v1/acp/connect` once → save `connectionId` + `sessionToken`.
3. `initialize` once → confirm `protocolVersion=1`.
4. For each chat completion: fresh `session/new` (cheap, ~200ms) →
   `session/prompt` → translate stream events → close session implicitly
   when stream ends.

On shutdown: `SIGTERM` the child process. The daemon exits cleanly.

Health check: re-issue `initialize` (cheap, no side effects) every 60s
or on incoming request after >60s idle. If it errors, restart the
daemon.

There is no `/api/health` endpoint. Don't probe one.

---

## 6. What about `codebuddy daemon start`?

There is a separate `codebuddy daemon start --port` subcommand for a
"daemon supervisor". On 2.106.3 it exits immediately with status
`stopped` — undocumented and not usable yet. Ignore it; use the
`--acp --acp-transport streamable-http` path documented above.

---

## 7. Wire samples

See `docs/wire-samples/`:
- `02_real_initialize.sse` — verified initialize response.
- `03_real_session_new_authed.sse` — session/new with full result.
- `04_real_session_prompt_pong.sse` — session/prompt streaming PONG.

(These will be captured fresh during the rewrite if not already
present.)
