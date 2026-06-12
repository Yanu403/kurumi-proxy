# Kurumi Proxy Multi-Key Router + Quota Tracker Spec

## Background

Project: `/root/projects/kurumi-proxy`

Current service is a standalone FastAPI OpenAI-compatible proxy wrapping CodeBuddy CLI.

Current critical behavior:
- `POST /v1/chat/completions` translates OpenAI messages into a prompt and runs CodeBuddy CLI.
- CodeBuddy CLI must receive large prompts through stdin, not argv.
- Current upstream credential comes from single env var `CODEBUDDY_API_KEY`.
- `KURUMI_PROXY_API_KEY` protects `/v1/*` endpoints when set.
- Service runs via systemd: `kurumi-proxy.service` on `127.0.0.1:8785`.

User goal:
- Reduce wasted credits and prevent single-key exhaustion.
- Make Kurumi Proxy more like 9router:
  - multiple CodeBuddy credentials/connections
  - automatic fallback when one key hits quota/credit/rate-limit/auth problems
  - quota/usage tracker
  - RTK-like token saver to reduce tool-result/log payload cost

Reference patterns from 9router:
- Provider connections are records with active flag, priority, last error, cooldown/model lock, lastUsedAt/consecutiveUseCount.
- Routing strategy can be `fill-first` or `round-robin` with sticky count.
- On provider failure, mark the account/model unavailable and retry next credential.
- Usage is logged per request and aggregated by day/model/account/API key.
- RTK compresses large tool-result payloads before upstream call, preserving errors.

## Requirements

### 1. Multi credential storage

Add lightweight local SQLite storage under project runtime directory. Keep standalone, no Hermes imports.

Suggested default path:
- `runtime/kurumi_proxy.sqlite3`
- env override: `KURUMI_PROXY_DB_PATH`

Tables minimum:

`connections`
- `id` TEXT primary key
- `provider` TEXT default `codebuddy`
- `name` TEXT
- `api_key` TEXT NOT NULL (stored local; never returned by API)
- `priority` INTEGER default 100
- `is_active` INTEGER default 1
- `last_used_at` TEXT nullable
- `consecutive_use_count` INTEGER default 0
- `cooldown_until` TEXT nullable
- `model_locks` TEXT JSON default `{}` where key is model and value ISO timestamp
- `backoff_level` INTEGER default 0
- `last_error` TEXT nullable
- `last_error_at` TEXT nullable
- `created_at` TEXT
- `updated_at` TEXT

`usage_history`
- id integer primary key autoincrement
- timestamp TEXT
- provider TEXT
- model TEXT
- connection_id TEXT nullable
- connection_name TEXT nullable
- api_key_name TEXT nullable (downstream key fingerprint/name only, not upstream secret)
- endpoint TEXT
- prompt_tokens INTEGER
- completion_tokens INTEGER
- total_tokens INTEGER
- status TEXT (`success`/`error`)
- error TEXT nullable
- duration_ms INTEGER nullable
- rtk_before_bytes INTEGER nullable
- rtk_after_bytes INTEGER nullable
- rtk_saved_bytes INTEGER nullable

Use stdlib sqlite3, no heavy dependencies.

Migration behavior:
- On app startup or first DB use, create tables if missing.
- If env `CODEBUDDY_API_KEY` exists and no connections exist, seed a connection from it named `env-default`. Do not print the value.

### 2. Connection management endpoints

Protected by same `/v1` bearer auth or an admin dependency using `KURUMI_PROXY_API_KEY`.

Add endpoints:

- `GET /admin/connections`
  - returns safe metadata only: id, provider, name, priority, is_active, last_used_at, consecutive_use_count, cooldown_until, model_locks, last_error, last_error_at, created_at, updated_at, usage summary if easy.
  - NEVER return `api_key`.

- `POST /admin/connections`
  - body: `{ "name": str, "api_key": str, "priority": int = 100, "is_active": bool = true }`
  - stores a new CodeBuddy key.
  - returns safe metadata.

- `PATCH /admin/connections/{id}`
  - allow update `name`, `priority`, `is_active`, and optionally replace `api_key`.

- `DELETE /admin/connections/{id}`
  - soft-delete/deactivate preferred: set `is_active=0`.

- `POST /admin/connections/{id}/reset`
  - clears cooldown/model locks/backoff/last_error.

Optional if quick:
- `POST /admin/connections/test`
  - body contains api_key, runs CodeBuddy `Reply exactly: OK` with timeout, does not store.

### 3. Router selection + fallback

Replace single-key provider behavior with a router:

- Select active connection available for requested model.
- Model unavailable if:
  - `cooldown_until` in future
  - `model_locks[model]` in future
  - `model_locks["__all"]` in future
- Strategy env:
  - `KURUMI_PROXY_ROUTING_STRATEGY=fill-first|round-robin` default `fill-first`
  - `KURUMI_PROXY_STICKY_ROUND_ROBIN_LIMIT=3`
- fill-first: lowest priority first, then least recently used.
- round-robin: least recently used, with sticky count similar to 9router.

Fallback loop in `/v1/chat/completions`:
- Try selected connection.
- On success:
  - clear expired/active lock for that model if any
  - record usage success
  - return response
- On retryable failure:
  - mark connection unavailable/locked
  - retry next available connection
- If all fail: return last upstream error with clear message and retry-after if available.

Retryable failure detection for CodeBuddy CLI errors:
- Match stderr/stdout/error message lowercased for:
  - `quota`, `credit`, `insufficient`, `balance`, `rate limit`, `limit exceeded`, `429`, `too many requests`, `usage_limit`, `exhausted`, `unauthorized`, `invalid api key`, `401`, `403`, `overloaded`, `temporarily unavailable`
- For quota/credit/auth: lock `__all` for longer default (e.g. 24h for exhausted/credit; 1h for auth invalid unless manually reset).
- For rate limit/overload: exponential backoff starting 60s max 15m, model-specific lock.
- For unknown transient provider error: 2m model-specific lock and fallback.

Important: Do not retry infinite; one pass over currently available connections is enough.

### 4. Provider changes

CodeBuddyProvider should accept an explicit upstream API key per call, preferably via a `CodeBuddyConnection` object.

Preserve critical CLI invocation:

```bash
codebuddy -p --tools "" --model <model> --output-format text --input-format text
```

and pass prompt through stdin.

This invocation is text-only and cannot produce OpenAI-compatible `tool_calls`. Until real tool-call support is implemented, `/v1/chat/completions` must reject requests that contain a non-empty `tools` array with an OpenAI-style `400 invalid_request_error` instead of silently discarding the fields.

Never log full API keys or prompts.

### 5. Usage / quota tracker endpoints

Add endpoints:

- `GET /admin/usage?days=7`
  - summarize requests/tokens/errors by day, model, connection.

- `GET /admin/quota`
  - since CodeBuddy may not expose a credit balance endpoint, report local tracker:
    - active connections
    - estimated prompt/completion/total tokens used today/7d/all time
    - request count
    - last success/error
    - current cooldown/model locks
    - note field: `credit_balance_known: false` unless actual upstream balance endpoint discovered.

If CodeBuddy exposes a credit/balance command/API, use it. If not, implement local estimated tracking and state that actual credit balance is unknown.

### 6. RTK-lite token saver

Implement a conservative Python RTK-lite preprocessor for OpenAI-style message payload before building prompt.

Scope:
- Compress only large tool outputs / tool result style messages.
- Preserve error traces (`is_error=true`, content containing traceback/error should not be aggressively removed).

Cases:
- OpenAI: `{role: "tool", content: "...large..."}`
- OpenAI content array with text blocks
- Claude-style content blocks `{type: "tool_result", content: ...}`

Env:
- `KURUMI_PROXY_RTK_ENABLED=true|false` default `true`
- `KURUMI_PROXY_RTK_MIN_BYTES=2000`
- `KURUMI_PROXY_RTK_MAX_BYTES=200000`
- `KURUMI_PROXY_RTK_HEAD_LINES=120`
- `KURUMI_PROXY_RTK_TAIL_LINES=80`

Compression algorithm can be simple/safe:
- If text under min bytes: unchanged.
- If looks like JSON lines/log/build output/search/grep output, keep head + tail and add marker:
  `[kurumi-proxy rtk-lite: truncated <N> chars, preserved head/tail]`
- Deduplicate repeated identical lines when many repeats.
- Never output empty.
- Never return longer text than input.
- Return stats bytesBefore/bytesAfter/saved.

Apply to a copy of request messages before prompt building.

### 7. Tests

Add/adjust tests. Use mocks; do not need real CodeBuddy key.

Coverage:
- DB initializes and env key seeds default connection.
- `/admin/connections` never returns raw api_key.
- creating connection stores key and returns metadata.
- router selects fill-first by priority.
- fallback retries second key when first mocked provider raises quota/rate error.
- all connections unavailable returns provider error.
- usage history is written for success/error.
- RTK-lite compresses large tool output and preserves small/error content.
- existing streaming and non-streaming chat tests still pass.
- CodeBuddyProvider still passes prompt via stdin, not argv.

### 8. Docs

Update README and `.env.example`:
- describe multi-key setup
- show secure key add via admin endpoint using local file/hidden input, not Telegram paste
- describe `/admin/quota` and `/admin/usage`
- document RTK-lite settings
- state actual CodeBuddy balance may not be available; local usage is tracked.

### 9. Verification commands

After implementation:

```bash
cd /root/projects/kurumi-proxy
.venv/bin/python -m pytest -q
systemctl restart kurumi-proxy
systemctl status kurumi-proxy --no-pager
curl -sS http://127.0.0.1:8785/health
curl -sS http://127.0.0.1:8785/admin/quota -H "Authorization: Bearer $KURUMI_PROXY_API_KEY"
curl -sS http://127.0.0.1:8785/v1/chat/completions \
  -H "Authorization: Bearer $KURUMI_PROXY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-5.5","messages":[{"role":"user","content":"Reply exactly: KURUMI_MULTIKEY_OK"}],"stream":false}'
```

Do not print secrets.
