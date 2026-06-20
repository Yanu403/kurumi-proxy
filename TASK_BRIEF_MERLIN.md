# TASK BRIEF: Add MerlinProvider to kurumi-proxy

## Context

kurumi-proxy is an OpenAI-compatible proxy at `/root/projects/kurumi-proxy/`. It currently has one provider (CodeBuddy via subprocess/ACP daemon). We need to add a second provider: **Merlin AI** (getmerlin.in), a browser extension that proxies LLM requests through their backend.

The user has a Merlin Pro subscription. We captured the real API wire format via CloakBrowser HAR capture. All ground truth is in `docs/MERLIN_PROTOCOL.md` and `docs/wire-samples/`.

## What to build

### 1. Refactor to multi-provider architecture

Currently `main.py` hardcodes `CodeBuddyProvider`. Refactor to support multiple providers via a `provider/model` prefix convention:

- `merlin/gpt-5.5` → MerlinProvider, model `gpt-5.5`
- `codebuddy/gpt-5.5` → CodeBuddyProvider, model `gpt-5.5`  
- `gpt-5.5` (no prefix) → default provider from config

**Key files to modify:**
- `kurumi_proxy/providers/base.py` — formalize `BaseProvider` abstract class with `async def complete(...)` and `async def stream(...)` methods
- `kurumi_proxy/main.py` — add provider registry, model prefix parsing, dispatch logic
- `kurumi_proxy/config.py` — add `default_provider` setting
- `kurumi_proxy/models.py` — no changes needed (OpenAI models stay the same)

**DO NOT break existing CodeBuddyProvider behavior.** It must continue working exactly as before.

### 2. Implement MerlinProvider

New file: `kurumi_proxy/providers/merlin.py`

**Auth mechanism:**
- Firebase anonymous auth via REST API (no browser needed)
- `POST https://identitytoolkit.googleapis.com/v1/accounts:signUp?key=AIzaSyAvCgtQ4XbmlQGIynDT-v_M8eLaXrKmtiM` with `{"returnSecureToken":true}`
- Returns `idToken` (JWT, expires ~1h) and `refreshToken`
- Refresh: `POST https://securetoken.googleapis.com/v1/token?key=AIzaSyAvCgtQ4XbmlQGIynDT-v_M8eLaXrKmtiM` with `grant_type=refresh_token`
- Token auto-refresh before expiry

**Chat endpoint:**
- `POST https://www.getmerlin.in/arcane/api/v2/thread/unified`
- Headers: `Authorization: Bearer <idToken>`, `Content-Type: application/json`, `Accept: text/event-stream`, `x-merlin-version: web-merlin`, `x-request-timestamp: <ISO-8601>`
- Body: see `docs/MERLIN_PROTOCOL.md` for exact format
- Response: SSE stream (parse as `text/event-stream`)

**Model list:**
- Expose models from `merlin_constants.json` CDN: `https://cdn.jsdelivr.net/gh/foyer-work/cdn-files@latest/merlin_constants.json`
- Fetch on startup and cache
- Model IDs: `claude-4.5-haiku`, `gpt-5.5`, `gemini-3.1-pro`, `deepseek-v4-pro`, etc.

**SSE parsing:**
- Since we don't have raw SSE samples yet, implement best-effort SSE parser that:
  - Reads `data:` lines
  - Tries to parse as JSON
  - Extracts content from common patterns (`choices[0].delta.content`, `text`, `content`)
  - Falls back to raw text if parsing fails
- Log first 5 SSE events at DEBUG level to help debug format issues

**Provider config env vars:**
- `MERLIN_FIREBASE_API_KEY` — default: `AIzaSyAvCgtQ4XbmlQGIynDT-v_M8eLaXrKmtiM`
- `MERLIN_FIREBASE_PROJECT` — default: `foyer-work`
- `MERLIN_DEFAULT_MODEL` — default: `gemini-2.5-flash-lite`
- `MERLIN_BASE_URL` — default: `https://www.getmerlin.in`

### 3. Update `/v1/models` endpoint

Should return combined list from all registered providers:
```json
{
  "object": "list",
  "data": [
    {"id": "merlin/gpt-5.5", "object": "model", "owned_by": "merlin"},
    {"id": "merlin/claude-4.8-opus", "object": "model", "owned_by": "merlin"},
    {"id": "codebuddy/gpt-5.5", "object": "model", "owned_by": "codebuddy"},
    ...
  ]
}
```

### 4. Admin endpoints

- `GET /admin/providers` — list registered providers and their status
- Existing `/admin/connections` should still work for CodeBuddy

### 5. Tests

- `tests/test_merlin_provider.py` — unit tests for MerlinProvider:
  - Mock HTTP calls to Firebase and Merlin API
  - Test token acquisition and refresh
  - Test request translation (OpenAI → Merlin format)
  - Test SSE parsing
  - Test error handling (expired token, rate limit, network error)
- Update `tests/test_app.py` — add tests for multi-provider dispatch
- **All existing 66 tests must continue passing**

### 6. Update README.md

Add Merlin section: what it is, how to configure, env vars, model list.

## File structure after implementation

```
kurumi_proxy/
├── __init__.py
├── config.py           # + MERLIN_* config vars
├── main.py             # + provider registry, prefix dispatch
├── models.py           # unchanged
├── db.py               # unchanged
├── router.py           # unchanged  
├── rtk.py              # unchanged
└── providers/
    ├── __init__.py
    ├── base.py          # formalized BaseProvider ABC
    ├── codebuddy.py     # unchanged
    ├── codebuddy_acp/   # unchanged
    └── merlin.py        # NEW — MerlinProvider
tests/
    ├── test_merlin_provider.py  # NEW
    ├── test_app.py              # + multi-provider tests
    └── (all existing tests unchanged)
docs/
    ├── MERLIN_PROTOCOL.md       # already written (ground truth)
    └── wire-samples/            # already captured
```

## Constraints

- Python 3.11+, FastAPI, httpx (already in requirements)
- No new heavy dependencies (httpx is already available)
- Standalone — no Hermes imports
- All existing tests must pass
- Commit messages: conventional commits format
- Work in `/root/projects/kurumi-proxy/`
- venv at `.venv/` — activate before running tests

## Verification

After implementation:
1. `git diff --stat` — confirm files changed
2. `.venv/bin/python -m pytest -q --timeout=20` — all tests pass
3. Smoke test: start server, send a real chat request to Merlin:
   ```bash
   curl -X POST http://127.0.0.1:8785/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{"model":"merlin/gemini-2.5-flash-lite","messages":[{"role":"user","content":"say hi"}]}'
   ```
4. Check `/v1/models` returns both codebuddy and merlin models
5. `git log --oneline -3` — clean commit history
