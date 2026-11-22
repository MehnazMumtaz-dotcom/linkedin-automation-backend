# LinkedIn Automation Suite — Python Engine

FastAPI + Playwright service that performs all LinkedIn browser work.

## Endpoints

| Method & path | Purpose |
|---|---|
| `POST /scrape` | Scrape a profile (or a list of `postUrls`) and return JSON |
| `POST /scrape-stream` | Same as `/scrape`, streamed as SSE: `init → browser → profile → posts → comments → done`, then a final `result` event |
| `GET /cindy/inbox/cron` | Cindy cron status, logs, processed count |
| `POST /cindy/inbox/cron` | `start`, `stop`, `send_followup`, `reset-processed`, `clear-logs`, or any other action = one manual tick |

Request body for the scrape endpoints: `{ "cookieString": "li_at=...; JSESSIONID=...", "profileUrl": "https://www.linkedin.com/in/<name>/", "postUrls": [], "limit": 10 }`.

## What is scraped

Name, headline, location, about, open-to-work, profile picture, experience, education,
skills, accomplishments (certifications, awards, publications…), interests, contact info,
posts (text, reactions, comments, reposts, URLs, media) and the prospect's comments.

## Cindy engine (`cindy_inbox.py`)

Background thread that, on every tick: validates the LinkedIn session, reads unread
conversations, generates knowledge-base replies with OpenAI, escalates sensitive topics,
sends replies, registers/sends follow-ups, persists history, deduplicates messages, and
suspends on session loss or repeated network failures. Running state is persisted to
`bot_settings.data.cindy_cron` so the cron auto-resumes after a restart.

## Run

```bash
pip install -r requirements.txt
playwright install chromium
uvicorn api:app --host 0.0.0.0 --port 8000
```

Environment: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `OPENAI_API_KEY`, optional
`NODE_BACKEND_URL` (for SSE bridge notifications) — see `.env.example`.
