# kurumi-proxy

Standalone local OpenAI-compatible proxy for CodeBuddy CLI.

## Features

- `GET /health`
- `GET /v1/models`
- `POST /v1/chat/completions`
- Optional downstream bearer-token protection for `/v1/*`
- Async subprocess wrapper around `codebuddy`
- OpenAI-style non-streaming chat completion response

Streaming requests currently return `501 Not Implemented`.

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

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `KURUMI_PROXY_API_KEY` | unset | Optional bearer token required by `/v1/*` endpoints when set. |
| `CODEBUDDY_API_KEY` | unset | Required only for real CodeBuddy chat calls. |
| `CODEBUDDY_BIN` | `codebuddy` | CodeBuddy executable path or command name. |
| `CODEBUDDY_MODEL` | `default-model` | Default upstream model. |
| `CODEBUDDY_TIMEOUT_SECONDS` | `180` | CLI timeout in seconds. |
| `KURUMI_PROXY_MAX_OUTPUT_TOKENS` | `8192` | Default `max_tokens` value when omitted by clients. |

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

## CodeBuddy Invocation

The proxy wraps CodeBuddy CLI like this:

```bash
codebuddy -p --tools "" --model <model> --output-format text <prompt>
```

`CODEBUDDY_API_KEY` is passed through to the subprocess environment when present. Secrets are not logged and stderr returned in API errors is redacted.

## Tests

```bash
pytest -q
```

Tests mock provider behavior and do not call the real CodeBuddy API.
