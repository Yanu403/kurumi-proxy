# kurumi-proxy

Standalone local OpenAI-compatible proxy for CodeBuddy CLI.

## Features

- `GET /health`
- `GET /v1/models`
- `POST /v1/chat/completions`
- Optional downstream bearer-token protection for `/v1/*`
- Async subprocess wrapper around `codebuddy`
- OpenAI-style non-streaming chat completion response
- OpenAI-style streaming response compatibility
- Multi-key CodeBuddy credential routing with fallback on quota/rate-limit/auth errors
- Local SQLite usage/quota tracker by model and connection
- RTK-lite compression for large tool/tool_result payloads

The proxy stays standalone: no Hermes imports and no Hermes-specific runtime paths.

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set credentials in your shell or `.env`:

```bash
export CODEBUDDY_API_KEY="your-codebuddy-key"
```

Do not commit real credentials. `.env` is ignored by git.

On first startup, if `CODEBUDDY_API_KEY` is set and the local credential database is empty, the proxy seeds one connection named `env-default`. Additional keys should be added through the admin API.

## Configuration

- `KURUMI_PROXY_API_KEY`: optional bearer token required by `/v1/*` endpoints when set.
- `CODEBUDDY_API_KEY`: seed/default upstream CodeBuddy key for real chat calls.
- `CODEBUDDY_BIN`: CodeBuddy executable path or command name. Default: `codebuddy`.
- `CODEBUDDY_MODEL`: default upstream model. Default: `default-model`.
- `CODEBUDDY_TIMEOUT_SECONDS`: CLI timeout in seconds. Default: `180`.
- `KURUMI_PROXY_MAX_OUTPUT_TOKENS`: default `max_tokens` value when omitted by clients. Default: `8192`.
- `KURUMI_PROXY_DB_PATH`: local SQLite database for credentials and usage. Default: `runtime/kurumi_proxy.sqlite3`.
- `KURUMI_PROXY_ROUTING_STRATEGY`: credential selection strategy, `fill-first` or `round-robin`. Default: `fill-first`.
- `KURUMI_PROXY_STICKY_ROUND_ROBIN_LIMIT`: consecutive-use limit before rotating in round-robin mode. Default: `3`.
- `KURUMI_PROXY_RTK_ENABLED`: enable RTK-lite payload compression. Default: `true`.
- `KURUMI_PROXY_RTK_MIN_BYTES`: minimum tool payload size before compression. Default: `2000`.
- `KURUMI_PROXY_RTK_MAX_BYTES`: maximum raw payload bytes considered by RTK-lite. Default: `200000`.
- `KURUMI_PROXY_RTK_HEAD_LINES`: head lines preserved when truncating large payloads. Default: `120`.
- `KURUMI_PROXY_RTK_TAIL_LINES`: tail lines preserved when truncating large payloads. Default: `80`.

## Run

```bash
uvicorn kurumi_proxy.main:app --host 127.0.0.1 --port 8785
```

## Usage

Health and models do not require `CODEBUDDY_API_KEY`:

```bash
curl http://127.0.0.1:8785/health
curl http://127.0.0.1:8785/v1/models
```

Chat completion:

```bash
curl http://127.0.0.1:8785/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "default-model",
    "messages": [
      {"role": "system", "content": "Be concise."},
      {"role": "user", "content": "Write a haiku about proxies."}
    ]
  }'
```

If `KURUMI_PROXY_API_KEY` is set, include it on `/v1/*` requests:

```bash
curl http://127.0.0.1:8785/v1/models \
  -H "Authorization: Bearer $KURUMI_PROXY_API_KEY"
```

## Multi-key routing

Add CodeBuddy access keys through the local admin API. The API never returns raw upstream keys.

```bash
curl http://127.0.0.1:8785/admin/connections \
  -H "Authorization: Bearer $KURUMI_PROXY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name":"codebuddy-2","api_key":"REDACTED","priority":20}'
```

Useful admin endpoints:

- `GET /admin/connections`: safe credential metadata, cooldowns, model locks, last errors.
- `POST /admin/connections`: add a CodeBuddy key.
- `PATCH /admin/connections/{id}`: update name, priority, active flag, or replace key.
- `DELETE /admin/connections/{id}`: soft-disable a key.
- `POST /admin/connections/{id}/reset`: clear cooldowns, model locks, and last error.
- `GET /admin/usage?days=7`: local request/token/error summary.
- `GET /admin/quota`: local usage estimate and per-connection state.

When an upstream key returns a retryable quota/credit/rate-limit/auth/transient error, Kurumi Proxy locks that connection temporarily and retries the next available key. Actual CodeBuddy credit balance is not exposed by this proxy yet; `/admin/quota` reports local estimates and lock state.

## RTK-lite token saver

RTK-lite only touches large tool outputs and tool-result blocks. It preserves small messages and error-looking content, keeps head/tail lines, deduplicates repeated lines, and records saved bytes into usage history.

## CodeBuddy invocation

The proxy wraps CodeBuddy CLI like this:

```bash
codebuddy -p --tools "" --model <model> --output-format text --input-format text
```

The prompt is sent through stdin, not argv. This avoids Linux argument-length failures on long prompts. `CODEBUDDY_API_KEY` is passed through to the subprocess environment from the selected connection. Secrets are not logged and stderr returned in API errors is redacted.

## Tests

```bash
pytest -q
```

Tests mock provider behavior and do not call the real CodeBuddy API.
