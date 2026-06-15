# Kurumi-Proxy: ACP Tool-Call Support (Design Spec)

Goal: replace the current text-only `subprocess` CodeBuddy adapter with a persistent
**ACP (Agent Client Protocol) bridge** to a long-running CodeBuddy CLI daemon, so
OpenAI-compatible clients can use **tool_calls**, **streaming reasoning_content**,
and **multi-turn tool round-trips** through Kurumi-Proxy.

## Background

Today `kurumi_proxy/providers/codebuddy.py`:
- Spawns `codebuddy -p --tools "" --output-format text --input-format text` per request
- Pipes a flattened text prompt
- Returns the final stdout as `result.text`
- `main.py` rejects any request with `tools` (HTTP 400 `unsupported_tool_calls`)
- `stream=true` is faked: a single `delta.content` chunk plus `[DONE]`

This blocks Hermes-style clients that require tool_calls (which is most OpenAI-compatible
agentic frameworks). We confirmed CodeBuddy CLI v2.1.x has a hidden but well-documented
HTTP server (`codebuddy --serve`) exposing both Agent runs (`POST /api/v1/runs`) and a
**JSON-RPC ACP** endpoint that emits `tool_call`, `tool_call_update`, `agent_message_chunk`,
`agent_thought_chunk`, `interruption_request`, `session_end`, etc.

ACP is the cleanest mapping target for OpenAI tool_calls because it already carries
structured tool events, permission gates, and reasoning chunks separately from text.

The full OpenAPI spec for the local CodeBuddy CLI server is checked in at
`docs/cb_cli_openapi.json` — refer to it for exact schemas (paths, request/response,
SSE event shapes). When in doubt, consult that file rather than guessing.

## CodeBuddy CLI Daemon Reference

### Launching

```bash
codebuddy --serve --port <port> --host 127.0.0.1 -y
```

- `-y` / `--dangerously-skip-permissions` is recommended so tool_call permission gates
  don't deadlock the kurumi-proxy bridge. Permission control belongs to the OpenAI
  client (via `tool_choice`), not the daemon.
- The daemon binds an HTTP server. All `/api/v1/*` endpoints require header
  `x-codebuddy-request: 1`.
- Auth comes from env var `CODEBUDDY_API_KEY` (the `ck_` access token). The daemon
  inherits the env from its parent process.
- The daemon takes 2-3 seconds to be ready. Health-check via
  `GET /api/v1/health` — `{"status":"UP","components":{"eg":{"status":"UP"}}}`.

### Key endpoints

- `POST /api/v1/acp/connect` — returns `{ connectionId, sessionToken }`. Header gate
  `x-codebuddy-request: 1`. Subsequent calls send `acp-connection-id: <connectionId>`.
- `GET /api/v1/acp` (SSE) — long-poll subscription for ALL session updates on this
  connection. Carries newline-delimited JSON-RPC notifications.
- `POST /api/v1/acp` — JSON-RPC 2.0 request channel (initialize, session/new,
  session/prompt, etc.). Header gate `x-codebuddy-request: 1` + `acp-connection-id: <id>`.
  **CRITICAL**: must send `Accept: application/json, text/event-stream` and
  `Content-Type: application/json`. Otherwise responds `Not Acceptable: Client must
  accept both application/json and text/event-stream`. **Each POST returns its own
  short SSE stream** carrying both intermediate `notifications` and the final
  `result`/`error` (correlated by JSON-RPC `id`). Parse this stream
  inline — do not rely solely on the long-poll `GET /api/v1/acp` for prompt traffic.
- `DELETE /api/v1/acp` — close connection.
- `POST /api/v1/runs` (with body `{ text, sender }`) — simpler "send one chat" endpoint
  that auto-streams via `GET /api/v1/runs/{runId}/stream`. Use this as a fallback if
  ACP turns out brittle.
- `GET /api/v1/auth/status` and `GET /api/v1/info` — useful for diagnostics.

### Verified handshake (do these on session start)

```
POST /api/v1/acp  body={"jsonrpc":"2.0","id":1,"method":"initialize",
                        "params":{"protocolVersion":1,"clientCapabilities":{}}}
→ result.agentCapabilities, authMethods, protocolVersion

POST /api/v1/acp  body={"jsonrpc":"2.0","id":2,"method":"session/new",
                        "params":{"cwd":"/tmp","mcpServers":[]}}
→ stream of session/update notifications (config_option_update, available_commands_update,
   usage_update) followed by result.{sessionId, models, modes, configOptions}
```

### Verified prompt shape

```
POST /api/v1/acp  body={"jsonrpc":"2.0","id":3,"method":"session/prompt",
                        "params":{"sessionId":"<sid>",
                                  "prompt":[{"type":"text","text":"..."}]}}
```

`prompt` is an array of content blocks; each block's discriminator is the top-level
`type` field — NOT nested under `content`. Allowed types: `text`, `image`, `audio`,
`resource_link`, `resource`. The wire shape was confirmed with a 401 round-trip:
unauthenticated calls produce a final `result.stopReason="refusal"` with the
upstream error embedded in `_meta`. Authenticated calls produce streaming
`agent_message_chunk` and friends, terminating in `result.stopReason="end_turn"`
(or other terminal reasons).

### ACP `session/update` event types we care about

Carried inside `notifications.params.update.sessionUpdate` (consult
`docs/cb_cli_openapi.json` for the authoritative schema):

| sessionUpdate              | OpenAI mapping                                                        |
|----------------------------|-----------------------------------------------------------------------|
| `agent_message_chunk`      | `delta.content` text chunk                                            |
| `agent_thought_chunk`      | `delta.reasoning_content` (passthrough; some clients ignore)          |
| `tool_call`                | New `delta.tool_calls[index]` with `id`, `function.name`, init args    |
| `tool_call_update`         | Append to the same index's `function.arguments`; on `completed`/`failed` carry result text into a synthesized `role:tool` message in the **final non-stream** response when `tool_choice="none"`, or surface `finish_reason=tool_calls` to the caller |
| `interruption_request`     | If `tool_choice="none"`: deny automatically. Otherwise: stop the run, return `finish_reason=tool_calls`, let the caller respond. |
| `session_end`              | `finish_reason`: `end_turn`→`stop`, `cancelled`→`stop`, `refusal`→`content_filter`. |
| `model_update`             | log only; do not surface                                              |
| `session_info_update`      | log only                                                              |

Two-turn flow with built-in CodeBuddy tools (preferred default mode):

1. Caller sends OpenAI request with no `tools` (or with `tool_choice="none"`).
2. Kurumi-Proxy lets CodeBuddy run with all built-in tools (`Read`, `Write`, `Edit`,
   `Bash`, etc.). All tool_call events are still translated into OpenAI deltas, so
   the client sees a transcript of what was done. Final answer arrives in
   `agent_message_chunk` events.
3. `session_end{stopReason: end_turn}` → `finish_reason="stop"`.

Multi-turn flow with caller-defined tools (advanced; can come second):

1. Caller passes `tools=[...]` with `tool_choice="auto"`. Kurumi-Proxy translates
   these to ACP "client tools" via the protocol's `availableCommands` /
   tool registration — and disables CodeBuddy's built-in matching tools.
2. When the model wants to invoke a caller tool, ACP emits `tool_call` then
   `interruption_request`. Kurumi-Proxy responds to the OpenAI side with
   `finish_reason="tool_calls"` and stops emitting deltas.
3. Caller submits a follow-up request including the tool result as
   `role:"tool", tool_call_id:...`. Kurumi-Proxy resumes the same ACP session,
   sending a `session/prompt` continuation that carries the tool result.
4. Loop continues until `session_end`.

If the spec for caller-defined tools turns out to be too entangled inside ACP for
v1, ship Mode 1 first and document Mode 2 as TODO. The killer feature is just
"tool_calls work end-to-end".

## Implementation Plan

### Layout

```
kurumi_proxy/
  providers/
    base.py                        # existing; extend ProviderResult to carry tool_calls + reasoning + finish_reason
    codebuddy.py                   # KEEP existing subprocess provider as fallback for env without --serve
    codebuddy_acp/
      __init__.py
      daemon.py                    # CodeBuddyDaemonManager: spawn, healthcheck, restart, port discovery
      client.py                    # ACPClient: connect, JSON-RPC POST, single shared SSE stream demuxer
      session.py                   # ACPSession: per-request session lifecycle on top of ACPClient
      translator.py                # OpenAI request -> ACP prompt; ACP events -> OpenAI deltas/non-stream message
      tool_call_helper.py          # ensureToolCallIds, fixMissingToolResponses (ported from 9router)
  main.py                          # route /v1/chat/completions through the new provider when ACP is enabled
  config.py                        # new settings
```

### New settings (config.py / .env.example)

```
KURUMI_PROXY_BACKEND=acp           # "acp" | "subprocess" (default acp; fallback if daemon disabled)
KURUMI_PROXY_ACP_HOST=127.0.0.1
KURUMI_PROXY_ACP_PORT=0            # 0 = ephemeral (let OS pick); store the chosen port in runtime
KURUMI_PROXY_ACP_STARTUP_TIMEOUT=15
KURUMI_PROXY_ACP_REQUEST_TIMEOUT=300
KURUMI_PROXY_ACP_HEALTHCHECK_INTERVAL=30
KURUMI_PROXY_ACP_DAEMON_RESTART_BACKOFF=2
KURUMI_PROXY_ACP_PERMISSION_MODE=bypassPermissions   # daemon launches with this; -y is the default
KURUMI_PROXY_ACP_BUILTIN_TOOLS=default               # "default" | "" (none) | comma list (forwarded to --tools)
KURUMI_PROXY_ACP_INCLUDE_REASONING=true
```

### Daemon manager (`daemon.py`)

- `start()`: spawn `codebuddy --serve --port <port> --host 127.0.0.1 -y --tools <BUILTIN>` as a child
  of the kurumi-proxy process. Pipe stdout/stderr to a ring buffer for diagnostics. Inherit `CODEBUDDY_API_KEY`.
- Wait until `GET /api/v1/health` returns `200` with `status: "UP"`, or until startup timeout.
- Background asyncio task pings `/api/v1/health` every `HEALTHCHECK_INTERVAL` seconds.
  On failure, kill the child, sleep `RESTART_BACKOFF`, restart. Track `boot_count` and
  expose via `/admin/acp/status`.
- `stop()`: SIGTERM, then SIGKILL after grace period.
- Graceful FastAPI lifecycle hook: start daemon during app `startup`, stop during `shutdown`.
- Race-safe: requests arriving during a restart get queued briefly (max 5s) then 503.

### ACP client (`client.py`)

- One `aiohttp.ClientSession` reused for the daemon lifetime.
- Single shared `GET /api/v1/acp` SSE consumer task, demultiplexes `notifications`
  by `params.sessionId` into per-session asyncio Queues.
- `request(method, params)` — POST a JSON-RPC envelope to `/api/v1/acp`, await result.
  ID rotation, response future map, timeout enforcement.
- Handles re-connection: when the SSE stream drops, reconnect; when `connect()` fails
  three times in a row, surface a `ProviderUnavailableError`.

### ACP session (`session.py`)

- `async with ACPSession(client) as s:` opens a fresh ACP session via JSON-RPC
  `session/new` (with `cwd=temp dir or process cwd`, `mcpServers=[]`).
- `s.prompt(blocks)` sends `session/prompt` with `prompt: [{type:"content", content:{...}}]`.
- `s.stream()` yields `SessionEvent` records pulled from the queue until `session_end`.
- `s.continue_with_tool_results([{tool_call_id, content}, ...])` sends a follow-up
  prompt carrying tool_result blocks (Mode 2).

### Translator (`translator.py`)

- `openai_to_acp_prompt(messages, tools, tool_choice)`:
  - Hoist system messages into the session's system prompt block.
  - Map remaining messages to ACP content blocks. `assistant` with `tool_calls` and
    `tool` messages collapse into ACP `tool_use` / `tool_result` blocks (use
    `tool_call_helper.ensureToolCallIds` to normalize).
  - If caller passed `tools`: include them as ACP `availableCommands` (Mode 2).
- `iter_openai_chunks(events, *, model, completion_id, created, settings)`:
  - Async generator that consumes ACPSession events and emits OpenAI SSE strings.
  - Carries an internal `tool_call_index` counter and a per-tool_call argument buffer
    so streaming arguments are valid JSON when reassembled.
- `collect_openai_completion(events, *, model, completion_id, created, settings)`:
  - Non-streaming variant returning a `ChatCompletionResponse` with full message,
    tool_calls, finish_reason, and usage (collapsed from the trailing `result` event).

### Updates to `main.py`

- New helper `chat_completions_acp(...)` that the router calls when
  `settings.kurumi_proxy_backend == "acp"`.
- Drop `reject_unsupported_tool_calls` when the backend is acp; route both text-only
  and tools requests through ACP.
- Continue to record usage via `store.record_usage(...)`. Take token counts from the
  ACP `result` event when available; otherwise estimate from text.
- Add `/admin/acp/status` GET endpoint returning daemon pid, port, boot_count,
  last_health_ok timestamp, last_error.

### Tests (`tests/`)

- `test_acp_translator.py`: unit tests against synthetic ACP event streams.
  - text-only response → single OpenAI delta + `finish_reason=stop`.
  - assistant tool_use chain → deltas with `tool_calls` accumulated and
    `finish_reason=tool_calls` (Mode 2) or follow-up `role:tool` synthesis (Mode 1).
  - reasoning chunks pass through to `delta.reasoning_content` when enabled.
  - `interruption_request` with `tool_choice="none"` auto-denied.
  - `session_end` mapping: end_turn/cancelled/refusal.
- `test_acp_daemon.py`: integration test with monkeypatched `codebuddy` shim
  (a small Python script in `tests/fixtures/fake_codebuddy_serve.py` that mimics
  the relevant endpoints and SSE events). Skip on CI when fixture not present.
- Keep existing tests green; the subprocess provider must still pass them.

### Backward compatibility

- The old subprocess provider stays as `KURUMI_PROXY_BACKEND=subprocess`.
- Existing tests that monkeypatch `provider_factory` keep passing — the new
  ACP provider exposes `complete()` with the same `ProviderResult` shape but with
  optional `tool_calls`, `reasoning_content`, and `finish_reason` set.
- Bump `kurumi_proxy/__init__.py` version to `0.2.0`.

## Acceptance Criteria

1. With `KURUMI_PROXY_BACKEND=acp` and a valid `CODEBUDDY_API_KEY`:
   - `POST /v1/chat/completions` with `messages=[{role:"user", content:"PONG"}]`
     returns `choices[0].message.content="PONG"` (or the model's natural reply)
     and `finish_reason="stop"`.
   - Same request with `stream=true` emits SSE deltas matching OpenAI Chat
     Completion stream shape, ending in `data: [DONE]`.
   - Request with `tools=[get_weather]` and `tool_choice="auto"` and
     "What's the weather in Tokyo?" returns either:
     - `finish_reason="tool_calls"` with one `tool_calls` entry whose
       `function.name="get_weather"` and `function.arguments` is valid JSON
       containing `"city": "Tokyo"` (or similar), OR
     - if Mode 2 is deferred, a graceful 501 explaining "client-defined tools not yet
       implemented; retry without `tools`".
2. Daemon survives 100 sequential requests without restart.
3. If the daemon dies mid-request, the in-flight request fails with a clear 502
   error and the next request triggers a daemon restart automatically.
4. `python -m pytest -q` passes.
5. `KURUMI_PROXY_BACKEND=subprocess` still works exactly like today.

## Out of scope for this PR

- MCP integration through `--mcp-config` (note in README under "Future work").
- Multi-account routing changes (existing `CredentialRouter` is reused; daemon uses
  one `CODEBUDDY_API_KEY` at a time — pool rotation is deferred).
- Image/audio inputs.

## References

- `docs/cb_cli_openapi.json` — full CodeBuddy CLI HTTP API (this is your bible).
- `docs/wire-samples/01_session_prompt_refusal.sse` — real captured transcript of
  initialize + session/new + session/prompt that ended in `refusal` because the
  CodeBuddy access token was invalid. Shows the SSE framing, `event: message`
  prefix, JSON-RPC envelopes, and the `_meta` fields.
- `/tmp/9router-ref/open-sse/translator/index.js` — translator registry pattern.
- `/tmp/9router-ref/open-sse/translator/helpers/toolCallHelper.js` — id normalization.
- `/tmp/9router-ref/open-sse/executors/qoder.js` — example of an executor that
  unwraps a non-OpenAI SSE envelope.
- `/tmp/9router-ref/open-sse/handlers/chatCore/streamingHandler.js` — orchestration
  of translate-translate streaming.

## Auth note for testing

The current `CODEBUDDY_API_KEY` in `.env` returns 401 (`token-length:61` is detected
but rejected). Implementation work does NOT require a working token — write the
plumbing against a `tests/fixtures/fake_codebuddy_serve.py` that emits canned SSE
transcripts. The user will plug a fresh `ck_` token before final smoke testing.
Treat 401 / `stopReason="refusal"` from the daemon as a normal upstream error and
surface it as HTTP 502 with a clear "CodeBuddy authentication required — refresh
CODEBUDDY_API_KEY" message.

