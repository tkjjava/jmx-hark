# hark-bridge

<p align="center">
  <img alt="version" src="https://img.shields.io/badge/version-0.1.0-blue">
  <img alt="python" src="https://img.shields.io/badge/python-3.11%2B-3776AB">
  <img alt="tests" src="https://img.shields.io/badge/tests-59%20passed-brightgreen">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-green">
  <img alt="no registration" src="https://img.shields.io/badge/registration-none-critical">
</p>

<p align="center">
  <b>Expose a Hark account pool as a standard OpenAI-compatible API</b><br>
  <sub>Author: Junmoxiao (君莫笑) · Eternal Orchid (永恒之兰)</sub>
</p>

<p align="center">
  <a href="README.md">中文文档</a> · <b>English</b>
</p>

---

Expose a Hark account pool as a standard OpenAI-compatible API.

**This project contains no signup capability.** Sessions must be imported after you log in
yourself in a real browser.

```
OpenAI client ──▶ hark-bridge  :8787/v1  ──▶ account pool (cookie sessions) ──▶ hark.com
```

## Why a bridge is needed

Hark's chat API is not OpenAI-shaped:

| Step | Hark | OpenAI |
|---|---|---|
| Send | `POST /api/messages/send` returns `{success, messageId}` immediately — no body | single call returns the answer |
| Receive | poll `GET /api/messages/history` for the assistant message | inline return or SSE |
| Auth | httpOnly cookie (Better Auth) | `Authorization: Bearer` |
| Model | server picks, no selector | client specifies |

This bridge hides that difference so downstream tools only ever see a normal OpenAI provider.

## Hard boundaries

This project does protocol adaptation and session management only. It explicitly does
**not** include:

- **No signup.** There is no account-creation, bulk-registration, or email-verification code.
- **No Cloudflare evasion.** No TLS fingerprint spoofing, no challenge solving, no CAPTCHA services.
- **No account sharing.** Designed for a single user managing their own accounts.

Sessions must be imported by you, after logging in with a real browser.

## Features

| | |
|---|---|
| **OpenAI compatible** | `/v1/chat/completions` (incl. SSE streaming), `/v1/models` |
| **Strict session checks** | `get-session` is the only source of truth; preference cookies are rejected |
| **Token rotation** | Captures `Set-Cookie` and writes it back — never misses a renewal |
| **No false logouts** | HTTP 200 returning HTML is an interception page, not a dead session; credentials are kept |
| **Three dispatch gates** | state / probe freshness / session expiry — any failure blocks the request |
| **Encrypted storage** | SQLite + Fernet; a missing key raises immediately, never falls back to plaintext |
| **Doctor** | `python -m hark_bridge.doctor` verifies your environment item by item |
| **Zero signup code** | No automated account creation of any kind |

## Install

```bash
git clone https://github.com/tkjjava/jmx-hark.git
cd jmx-hark
python -m pip install -r requirements.txt
```

Requires Python 3.11+.

## Configure

Sessions are encrypted at rest. Generate a key first:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

```bash
# Windows PowerShell
$env:HARK_BRIDGE_KEY = "<the key above>"
# Linux / macOS
export HARK_BRIDGE_KEY="<the key above>"
```

> The service refuses to start without this key. It will never degrade to plaintext storage.

| Variable | Default | Meaning |
|---|---|---|
| `HARK_BRIDGE_KEY` | required | Fernet key for session encryption |
| `HARK_BRIDGE_DB` | `./data/accounts.db` | account database |
| `HARK_BRIDGE_HOST` | `127.0.0.1` | bind address (local only by default) |
| `HARK_BRIDGE_PORT` | `8787` | listen port |
| `HARK_BRIDGE_API_KEYS` | empty | client keys, comma-separated; empty = no auth |
| `HARK_BASE` | `https://hark.com` | upstream base URL |
| `HARK_REPLY_MODE` | `history` | `history` / `search` / `none` |
| `HARK_REPLY_TIMEOUT` | `120` | reply wait timeout (seconds) |
| `HARK_BRIDGE_COOLDOWN` | `60` | cooldown for a failed account (seconds) |

Endpoint paths can be overridden if upstream changes:

```bash
HARK_PATH_SEND_MESSAGE=/api/messages/send
HARK_PATH_LIST_CONVERSATIONS=/api/conversations
```

## Import a session

### Option A — Chrome extension (recommended)

1. Open `chrome://extensions` and enable Developer mode
2. Load unpacked → select the `extension/` directory
3. Log in at `https://hark.com`
4. Click the extension icon, fill in an account label, click import

The extension reads session cookies, pushes them to the local service, which verifies
them against `get-session` and stores them encrypted.

### Option B — Console page

1. Log in at `https://hark.com`
2. Open DevTools → Network → copy the `Cookie` request header
3. Open <http://127.0.0.1:8787/console>, paste, submit

Three formats are accepted: raw `Cookie` header, JSON object, and one `name=value` per line.

> Preference cookies (like `theme=dark`) are rejected — they are not credentials.

## Run

```bash
# self-check first: key, database, upstream reachability, endpoint paths
python -m hark_bridge.doctor

# then start
python -m hark_bridge
```

| Endpoint | Purpose |
|---|---|
| `POST /v1/chat/completions` | OpenAI-compatible, supports `stream: true` |
| `GET /v1/models` | list available models |
| `GET /healthz` | health check |
| `GET /console` | session import console (local only) |
| `GET /admin/accounts` | pool status (redacted) |

## Client usage

### curl

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <key from HARK_BRIDGE_API_KEYS>" \
  -d '{"model":"hark-1","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

### Python

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="sk-...")

response = client.chat.completions.create(
    model="hark-1",
    messages=[{"role": "user", "content": "Hello"}],
)
print(response.choices[0].message.content)
```

Streaming:

```python
stream = client.chat.completions.create(
    model="hark-1",
    messages=[{"role": "user", "content": "Tell me a story"}],
    stream=True,
)
for chunk in stream:
    delta = chunk.choices[0].delta.content
    if delta:
        print(delta, end="", flush=True)
```

### Any OpenAI-compatible client

| Field | Value |
|---|---|
| Base URL | `http://127.0.0.1:8787/v1` |
| API Key | a key from `HARK_BRIDGE_API_KEYS` (anything if unset) |
| Model | `hark-1` |

Works with Cherry Studio, ChatBox, Open WebUI, LibreChat, Continue, Cline, and similar tools.

## Account states

| State | Meaning | Dispatchable |
|---|---|---|
| `session_valid` | probe passed, session not expired | ✅ |
| `session_expiring` | expires within 48h, re-import soon | ✅ |
| `session_expired` | expired | ❌ |
| `unverified` | never passed a probe | ❌ |
| `needs_interactive_verification` | upstream demands human verification | ❌ terminal |
| `blocked` | blocked upstream | ❌ terminal |
| `credentials_rejected` | credentials rejected | ❌ terminal |
| `rate_limited` | throttled | ❌ backoff |

Terminal states are never retried automatically — re-import the session to reset them.

### Why the gates are this strict

A dead account that keeps retrying amplifies the upstream's risk signals against it,
turning a merely expired account into a blocked one. Better to dispatch on fewer accounts
than to keep hammering a bad one.

## Architecture

```
hark_bridge/
  endpoints.py   upstream endpoint table (env-overridable)
  session.py     get-session probe, token rotation, definitive-vs-uncertain
  state.py       8-state machine, dispatch gates
  store.py       SQLite + Fernet
  upstream.py    gated calls, shared connection pool, cooldowns
  reader.py      reply polling, triggeredByMessageId matching
  pool.py        dispatch, round-robin, cooldown, OpenAI shaping
  app.py         FastAPI routes
  doctor.py      environment self-check
```

## Known limitations

- Hark **chooses the model server-side** (Claude Opus / Sonnet / Haiku). There is no model
  selector, so the `model` field is only a label — any alias routes to the same upstream.
- Reply text is obtained by polling `history`; `isStreaming: true` means still generating.
  Accumulated text is chunked into small pieces for a typewriter effect — this is not true
  upstream streaming.
- This is a reverse-engineered private web API and may change without notice. Override
  endpoint paths with `HARK_PATH_*` environment variables when it does.
- Sessions expire; re-import after logging in again.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `missing HARK_BRIDGE_KEY` | env var not set | set it (see Configure) |
| `503 NoAccountAvailable` | pool empty or nothing dispatchable | import a session; check `/admin/accounts` |
| `502 all_accounts_failed` | every account failed | look at the `note` field |
| Reply empty but `ok: true` | polling missed the body | try `HARK_REPLY_MODE=search` |
| No typewriter effect when streaming | upstream is poll-based, not real SSE | expected behaviour |

Full guide: [docs/USAGE.md](docs/USAGE.md) (Chinese) · Protocol details: [docs/PROTOCOL.md](docs/PROTOCOL.md) (Chinese)

## Security notes

1. **Do not bind to `0.0.0.0`** unless you understand the risk. Local-only is the default for a reason.
2. **Never commit the key or the database.** `.gitignore` already covers `data/`, `*.db`, `.env`.
3. Anyone with `HARK_BRIDGE_KEY` can decrypt the account store.
4. The database holds live session credentials — treat it like a password file.

## License

MIT © 2026 **tkjjava (君莫笑)** · **Eternal Orchid (永恒之兰)**

## Disclaimer

Not affiliated with, endorsed by, or authorized by Hark. This project automates access to a
third party's private web API. You are responsible for confirming and complying with that
service's terms of use and applicable law. Provided for interoperability and educational purposes.

---

<p align="center">
  <a href="README.md">中文文档</a> · <b>English</b>
</p>
