# Merlin AI (getmerlin.in) — API Protocol Reference

## Overview

Merlin AI is a browser extension + web app that proxies LLM requests through
their backend. The web app is a Next.js SPA at `www.getmerlin.in`. The chat
API lives under `/arcane/api/`.

Authentication is via **Firebase Auth** (project: `foyer-work`).
Both anonymous and authenticated (Pro) users can call the chat API.

## Authentication

### Firebase Anonymous Auth
```bash
curl -X POST 'https://identitytoolkit.googleapis.com/v1/accounts:signUp?key=AIzaSyAvCgtQ4XbmlQGIynDT-v_M8eLaXrKmtiM' \
  -H 'Content-Type: application/json' \
  -d '{"returnSecureToken":true}'
```
Response:
```json
{
  "idToken": "<firebase-id-token>",
  "refreshToken": "<refresh-token>",
  "expiresIn": "3600",
  "localId": "<user-id>"
}
```

### Firebase Token Refresh
```bash
curl -X POST 'https://securetoken.googleapis.com/v1/token?key=AIzaSyAvCgtQ4XbmlQGIynDT-v_M8eLaXrKmtiM' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d 'grant_type=refresh_token&refresh_token=<refresh-token>'
```
Response:
```json
{
  "access_token": "<new-id-token>",
  "refresh_token": "<new-refresh-token>",
  "expires_in": "3600",
  "token_type": "Bearer"
}
```

### Token Format
Firebase JWT with these claims:
- `iss`: `https://securetoken.google.com/foyer-work`
- `aud`: `foyer-work`
- `provider_id`: `anonymous` (for anon) or `password` (for email auth)
- `role`: `paid` (Pro users only)
- `planName`: `Merlin Pro` (Pro users only)
- `userPlan`: `PRO` (Pro users only)
- `exp`: ~1 hour from `iat`

## Chat Endpoint

### Request
```
POST https://www.getmerlin.in/arcane/api/v2/thread/unified
Authorization: Bearer <firebase-id-token>
Content-Type: application/json
Accept: text/event-stream
x-merlin-version: web-merlin
x-request-timestamp: <ISO-8601 timestamp with timezone>
```

Body:
```json
{
  "attachments": [],
  "chatId": "<uuid-v4>",
  "language": "AUTO",
  "message": {
    "childId": "<uuid-v4>",
    "content": "the user message",
    "context": "",
    "id": "<uuid-v4>",
    "parentId": "root"
  },
  "mode": "UNIFIED_CHAT",
  "model": "gemini-2.5-flash-lite",
  "metadata": {
    "noTask": true,
    "isWebpageChat": false,
    "deepResearch": false,
    "webAccess": true,
    "proFinderMode": false,
    "mcpConfig": {"isEnabled": false},
    "merlinMagic": false
  }
}
```

### Response
SSE (text/event-stream). Format TBD — needs capture of raw SSE chunks.
The chat response appeared as a complete message in the UI, suggesting
the SSE delivers the full text incrementally (similar to OpenAI streaming).

## Available Models

Source: `merlin_constants.json` from CDN.

### Text LLMs (from CDN config, June 2026)
| Model ID | Display Name | Query Cost | Paid Only |
|----------|-------------|------------|-----------|
| `claude-4.5-haiku` | Claude Haiku 4.5 | 5 | No |
| `claude-4.6-sonnet` | Claude Sonnet 4.6 | 100 | Yes |
| `claude-4.6-opus` | Claude Opus 4.6 | 200 | Yes (archived) |
| `claude-4.7-opus` | Claude Opus 4.7 | 400 | Yes (archived) |
| `claude-4.8-opus` | Claude Opus 4.8 | 400 | Yes |
| `gpt-5.2` | GPT 5.2 | ? | ? |
| `gpt-5.4` | GPT 5.4 | ? | ? |
| `gpt-5.5` | GPT 5.5 | ? | ? |
| `gemini-2.5-flash-lite` | Gemini 2.5 Flash Lite | ? | No |
| `gemini-3.0-pro` | Gemini 3.0 Pro | ? | ? |
| `gemini-3.1-pro` | Gemini 3.1 Pro | ? | ? |
| `gemini-3.1-flash-lite` | Gemini 3.1 Flash Lite | ? | ? |
| `gemini-2.5-flash-thinking` | Gemini 2.5 Flash Thinking | ? | ? |
| `kimi-k2.5-thinking` | Kimi K2.5 Thinking | ? | ? |
| `grok-4.3` | Grok 4.3 | ? | ? |
| `deepseek-v4-pro` | DeepSeek V4 Pro | ? | ? |
| `glm-5` | GLM 5 | ? | ? |
| `minimax-m2.5` | MiniMax M2.5 | ? | ? |

## Other API Endpoints

- `GET /arcane/api/v1/user/survey/survey-events` — survey events
- `GET /arcane/api/v1/user/folders/get-pinned-chats` — pinned chats
- `POST /arcane/api/v1/user/history` — chat history (body: `{"limit":15,"page":1}`)
- `POST /arcane/api/v1/user/survey/update-survey-events` — update survey

## Rate Limits

- Anonymous: 29 queries total (not per day)
- Pro: unlimited basic models, premium models cost more query credits

## Key Config

- Firebase project: `foyer-work`
- Firebase API key: `AIzaSyAvCgtQ4XbmlQGIynDT-v_M8eLaXrKmtiM`
- CDN config: `cdn.jsdelivr.net/gh/foyer-work/cdn-files@latest/merlin_constants.json`
- CDN models: `cdn.jsdelivr.net/gh/foyer-work/cdn-files@latest/merlin_config.json`

## Limitations

- Anonymous tokens expire after 1 hour — must refresh
- Anonymous tier has 29 query limit total (not renewable)
- Pro features (deep research, premium models) require authenticated session
- Session cookies from browser cannot be reused from different IP/fingerprint
  (Auth.js encryption + Cloudflare cf_clearance are TLS-bound)
- The `cf_clearance` Cloudflare cookie is TLS-fingerprint-specific

## Open Questions

- [ ] Exact SSE response format (need raw chunk capture)
- [ ] Whether non-Pro models work with anonymous tokens (tested: gemini-2.5-flash-lite works)
- [ ] Tool/function calling support
- [ ] Multi-turn conversation support (parentId chain)
- [ ] Streaming vs non-streaming behavior
