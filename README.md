# LinkedIn Automation Suite — Backend

Two services that share one Supabase database:

| Service | Stack | Port | Role |
|---|---|---|---|
| [`automation-extensions`](./automation-extensions) | Node.js, Express, TypeScript, OpenAI | 3005 | API gateway for the frontend & extension, AI orchestration (Ceevee, Cara, Cindy replies), escalations, follow-ups, knowledge base, sessions, SSE events |
| [`automation-python`](./automation-python) | Python, FastAPI, Playwright | 8000 | LinkedIn scraping engine (profile, posts, comments) with SSE progress, Cindy inbox cron (Playwright) |

## Routing rules

- LinkedIn scraping and browser automation → **Python** only.
- Bot orchestration, AI, DB state, sessions → **Node**.
- The frontend only talks to Node; Node proxies to Python when scraping or the Cindy engine is needed.

## Node API (`/api/...`)

| Route | Purpose |
|---|---|
| `auth` | Manual LinkedIn cookie login / status / logout |
| `bots` | `GET` session status · `PATCH /:collection/update-session` (Chrome extension, `cindy_config` only) · `GET/PATCH /cron-intervals` |
| `scrape`, `scrape-stream` | Profile + posts scrape (JSON / SSE) via Python |
| `ceevee`, `ceevee-stream` | Prospect intelligence report (JSON / SSE) |
| `cara/*` | `analyze` (persona), `personas`, `simulate`, `generate` (manual / chat-aware / script-improver), `chat-logs` |
| `cindy/*` | `inbox` (fetch unread / AI reply + send), `inbox/cron` (Python cron proxy), `generate`, `conversations` |
| `knowledge-base` | CRUD for Cindy + global knowledge entries |
| `escalation` | Human escalation queue (list, create, resolve, reminders, delete) |
| `follow-up` | Records, rules, templates, and `POST /cron` (`run-now`, `manual-send`) |
| `session-alert`, `notification`, `events`, `cron-control` | Session alerts, notification status, SSE stream, cron snapshot |
| `debug` | LinkedIn session diagnostics |

## Safety features

- Session validation before every scrape (login / authwall / checkpoint detection).
- Session expiry → `bot_sessions.status = expired`, `session_alerts` row, SSE `session-expired`, Cindy cron suspends.
- Session renewal (extension, manual login, or Supabase Realtime watcher) → alert resolved, SSE `session-renewed`, cron resumes.
- Duplicate message prevention (`processed_messages` + `mark_message_processed` RPC), per-conversation locks, single cron thread, network-failure suspension.
- Pricing / discounts / contracts / meetings / out-of-KB questions → escalated to a human, never answered automatically.
