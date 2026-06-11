# Kurumi Proxy — TASK BRIEF

Build a fresh standalone Python project named `kurumi-proxy`.

## Context

Old projects were archived and must not be reused/imported:
- `/root/projects/_graveyard/.../kurumi-gateway`
- `/root/projects/_graveyard/.../notion-ai-gateway`

This project must start from zero and must be independent from Hermes. No imports from Hermes, no hardcoded `/usr/local/lib/hermes-agent` paths.

CodeBuddy CLI is installed on this VPS:
- command: `codebuddy`
- version observed: `2.105.2`
- auth is via environment variable `CODEBUDDY_API_KEY`.

Useful CodeBuddy docs observed:
- `codebuddy -p "prompt"` prints a one-shot response and exits.
- `--output-format` can be `text`, `json`, or `stream-json`.
- `--tools ""` disables tools; use this for pure chat-completion proxy mode.
- `--model <model>` supports model ids such as `default-model`, `gpt-5.5`, `gpt-5.4`, `gemini-3.1-pro`, `gemini-3.0-flash`, etc.
- CodeBuddy own API key is not an OpenAI Chat Completions endpoint; proxy should wrap the CLI initially.

## Goal

Create an OpenAI-compatible local proxy that exposes CodeBuddy as an upstream provider.

Primary endpoint:
- `POST /v1/chat/completions`

Additional endpoints:
- `GET /health`
- `GET /v1/models`

## Tech Stack

- Python 3.11+
- FastAPI + Uvicorn
- Pydantic Settings / python-dotenv
- pytest + httpx for tests
- CLI runner using `asyncio.create_subprocess_exec`

## Files expected

- `README.md`
- `LICENSE` (MIT)
- `.gitignore`
- `.env.example`
- `requirements.txt`
- `kurumi_proxy/`
  - `__init__.py`
  - `config.py`
  - `main.py`
  - `models.py`
  - `providers/`
    - `__init__.py`
    - `base.py`
    - `codebuddy.py`
- `tests/`
  - test config/model serialization/provider command behavior/app endpoints with mocked provider

## Behavior

### Configuration

Environment variables:
- `KURUMI_PROXY_API_KEY` optional downstream bearer token. If set, `/v1/*` requires `Authorization: Bearer <value>`.
- `CODEBUDDY_API_KEY` required for real upstream calls, but tests must not require it.
- `CODEBUDDY_BIN` default `codebuddy`.
- `CODEBUDDY_MODEL` default `default-model`.
- `CODEBUDDY_TIMEOUT_SECONDS` default 180.
- `KURUMI_PROXY_MAX_OUTPUT_TOKENS` default 8192.

### Chat completions

Accept a reasonable subset of OpenAI chat completions request:
- `model`, `messages`, `stream`, `temperature`, `max_tokens`

For now:
- non-streaming must work.
- streaming can return `501 Not Implemented` with clear JSON error OR implement basic fake SSE. Prefer explicit 501 if faster/reliable.

Translate messages into a deterministic text prompt for CodeBuddy CLI. Preserve roles:

```
System:
...

Conversation:
User: ...
Assistant: ...

User:
<latest user content>
```

Support content as string and simple list blocks containing `{type:"text", text:"..."}`; ignore unsupported multimodal blocks with a clear note in prompt.

Call CodeBuddy CLI with:

```
codebuddy -p --tools "" --model <model> --output-format text <prompt>
```

Important:
- Pass `CODEBUDDY_API_KEY` through env when set.
- Do not log secrets.
- Time out cleanly.
- Return OpenAI-compatible response shape:
  - `id`, `object`, `created`, `model`, `choices[0].message.role/content`, `finish_reason`, `usage` with best-effort token estimates.

### Error handling

- Missing `CODEBUDDY_API_KEY` on real provider call should produce HTTP 503 with message explaining credential missing.
- CodeBuddy command missing should produce HTTP 503.
- Nonzero CLI exit should produce HTTP 502 with stderr summarized, secrets redacted.
- Timeout should produce HTTP 504.

### Tests

Tests should not call real CodeBuddy API. Use monkeypatch/mock provider runner.

At minimum:
- `GET /health` ok
- `GET /v1/models` includes CodeBuddy model ids
- `POST /v1/chat/completions` returns valid OpenAI shape using mocked CodeBuddy provider
- downstream API key enforcement
- prompt conversion preserves role context

## Verification commands

Use a venv:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pytest -q
uvicorn kurumi_proxy.main:app --host 127.0.0.1 --port 8785
```

Smoke test without `CODEBUDDY_API_KEY` should show health/models work and real chat returns credential-missing 503.

## Quality bar

- Clean project, no old gateway imports.
- Typed, simple code.
- README includes setup, secure key handling, CodeBuddy usage, and sample curl.
- No credentials in repository.
