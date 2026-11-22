"""
Cindy Inbox Bot — Python Playwright implementation
===================================================
Replaces the Node.js raw-fetch cron with a Playwright browser context so that
LinkedIn session cookies are managed by the browser (just like the Scraper bot).

Key difference vs Node.js:
  Node.js made raw HTTP fetches with cookie headers — LinkedIn would expire /
  invalidate the session silently.
  Python uses a Playwright browser context, loads cookies into it, navigates to
  the messaging page (warm-up), then runs all LinkedIn API calls via
  page.evaluate() with credentials:'include'.  The browser's own cookie jar
  handles any Set-Cookie refresh responses automatically.

All state (running flag, log, processed IDs) lives in module-level dicts so a
single Python process can serve concurrent FastAPI requests while the cron runs
in a background daemon thread.
"""

import asyncio
import json
import os
import random
import re
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Optional

import requests

# ─── Bootstrap: load .env — try multiple locations ───────────────────────────
_here = os.path.dirname(os.path.abspath(__file__))
_env_candidates = [
    os.path.join(_here, ".env"),
    os.path.join(_here, "..", "automation-extensions", ".env"),
    os.path.join(_here, "..", ".env"),
]
try:
    from dotenv import load_dotenv
    for _env_path in _env_candidates:
        if os.path.exists(_env_path):
            load_dotenv(_env_path, override=False)
            print(f"[cindy][env] Loaded .env from {_env_path}", file=sys.stderr, flush=True)
            break
    else:
        print(f"[cindy][env] No .env file found in: {_env_candidates}", file=sys.stderr, flush=True)
except ImportError:
    print("[cindy][env] python-dotenv not installed — relying on system env vars", file=sys.stderr, flush=True)

# Log whether key env vars are present (without exposing values)
_supabase_url_set = bool(os.environ.get("SUPABASE_URL"))
_openai_key_set = bool(os.environ.get("OPENAI_API_KEY"))
print(f"[cindy][env] SUPABASE_URL={'SET' if _supabase_url_set else 'MISSING'}, OPENAI_API_KEY={'SET' if _openai_key_set else 'MISSING'}", file=sys.stderr, flush=True)

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# ─── Constants ────────────────────────────────────────────────────────────────

MY_PROFILE_URN = "urn:li:fsd_profile:ACoAAGUt7KsBAHMQRodG7z6z8EjY3JaOMFu5TmM"

LINKEDIN_INBOX_URL = (
    "https://www.linkedin.com/voyager/api/voyagerMessagingGraphQL/graphql"
    "?queryId=messengerConversations.0d5e6781bbee71c3e51c8843c6519f48"
    "&variables=(mailboxUrn:urn%3Ali%3Afsd_profile%3AACoAAGUt7KsBAHMQRodG7z6z8EjY3JaOMFu5TmM)"
)
LINKEDIN_SEND_URL = (
    "https://www.linkedin.com/voyager/api/voyagerMessagingDashMessengerMessages"
    "?action=createMessage"
)
# Use /feed/ for warm-up — NOT /messaging/ which auto-opens the first unread
# thread and marks it as read before we even fetch the inbox API.
WARMUP_URL = "https://www.linkedin.com/feed/"

AUTH_PATHS = ["/login", "/authwall", "/checkpoint", "/challenge", "/platform-change", "/uas/"]

# ─── Follow-up schedule: hours after reply before each check-in ───────────────
FOLLOWUP_HOURS = [2 / 60, 5 / 60, 10 / 60, 20 / 60, 30 / 60]

DEFAULT_FOLLOWUP_TEMPLATES = {
    1: "Hi {{user_name}}, just a friendly reminder about my previous message. Would love to hear your thoughts!",
    2: "Hi {{user_name}}, following up again — I wanted to make sure my message didn't get lost. Looking forward to connecting!",
    3: "Hi {{user_name}}, reaching out one more time. I sent a message {{days_waiting}} day(s) ago and haven't heard back. Happy to help whenever you're ready!",
    4: "Hi {{user_name}}, I know you're busy — just wanted to check in once more. Feel free to reach out anytime.",
    5: "Hi {{user_name}}, this will be my last follow-up. If you ever want to reconnect, don't hesitate to reach out. Wishing you all the best!",
}

_CHROME_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
]

_VIEWPORT_PROFILES = [
    {"w": 1920, "h": 1080, "d": 1.0},
    {"w": 1440, "h": 900,  "d": 1.0},
    {"w": 1536, "h": 864,  "d": 1.25},
    {"w": 2560, "h": 1440, "d": 1.5},
]

_LI_CLIENT_VERSIONS = [
    "1.13.42791", "1.13.42890", "1.13.43012", "1.13.43150", "1.13.43299",
]

# ─── Module-level state (shared across the FastAPI process) ───────────────────

_state: dict = {
    "running": False,
    "tick_running": False,
    "tick_started_at": None,
    "last_run": None,
    "log": [],
    "processing_convs": set(),
    "processed_message_ids": set(),
    "session_suspended": False,
    "network_errors": 0,
}

_cron_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()

# ─── Logging ──────────────────────────────────────────────────────────────────

def add_log(message: str, log_type: str = "info") -> None:
    entry = {
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "message": message,
        "type": log_type,
    }
    _state["log"].append(entry)
    if len(_state["log"]) > 50:
        _state["log"] = _state["log"][-50:]
    print(f"[cindy-cron][{log_type}] {message}", file=sys.stderr, flush=True)

# ─── Supabase client (synchronous — safe in background thread) ────────────────

_supabase_client = None
_supabase_lock = threading.Lock()
_cindy_bot_id = None


def _get_supabase():
    global _supabase_client
    with _supabase_lock:
        if _supabase_client is not None:
            return _supabase_client
        from supabase import create_client
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        if not url or not key or "your-project-ref" in url or key.startswith("your-"):
            raise RuntimeError(
                "[cindy][supabase] Supabase is not configured — set SUPABASE_URL and "
                "SUPABASE_SERVICE_ROLE_KEY in automation-python/.env (see the setup guide)"
            )
        _supabase_client = create_client(url, key)
        print("[cindy][supabase] Client ready ✓", file=sys.stderr, flush=True)
        return _supabase_client


def _get_cindy_bot_id() -> str:
    global _cindy_bot_id
    if _cindy_bot_id is not None:
        return _cindy_bot_id
    sb = _get_supabase()
    result = sb.from_("bots").select("id").eq("name", "cindy").maybe_single().execute()
    if not result.data:
        raise RuntimeError("[cindy][supabase] Bot 'cindy' not found in bots table")
    _cindy_bot_id = result.data["id"]
    return _cindy_bot_id


# ─── Cookie helpers ───────────────────────────────────────────────────────────

def cookie_string_to_playwright(raw: str) -> list:
    """Convert raw Cookie header string → list of Playwright cookie dicts."""
    www_cookies = {"JSESSIONID", "bscookie", "timezone", "li_theme", "li_theme_set"}
    result = []
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        eq = part.index("=")
        name = part[:eq].strip()
        value = part[eq + 1:].strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        if not name:
            continue
        result.append({
            "name": name,
            "value": value,
            "domain": ".www.linkedin.com" if name in www_cookies else ".linkedin.com",
            "path": "/",
            "expires": -1,
            "httpOnly": name in ("bscookie", "__cf_bm"),
            "secure": True,
            "sameSite": "None",
        })
    print(f"[cindy][cookies] Parsed {len(result)} cookies from raw string", file=sys.stderr, flush=True)
    return result


def extract_jsession_id(raw: str) -> str:
    """Extract and return JSESSIONID value (stripped of quotes)."""
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            name, value = part.split("=", 1)
            if name.strip() == "JSESSIONID":
                val = value.strip().strip('"')
                print(f"[cindy][cookies] JSESSIONID found: {val[:30]}...", file=sys.stderr, flush=True)
                return val
    print("[cindy][cookies] WARNING: JSESSIONID not found in cookie string", file=sys.stderr, flush=True)
    return ""


def _get_cookies_from_db() -> Optional[str]:
    """Fetch raw LinkedIn cookies from bot_sessions (Supabase)."""
    try:
        sb = _get_supabase()
        bot_id = _get_cindy_bot_id()
        # Newest active row — maybe_single() raises when duplicate rows exist
        result = sb.from_("bot_sessions").select("extra_data, raw_cookies").eq("bot_id", bot_id).eq("session_type", "li_session").eq("status", "active").order("saved_at", desc=True).limit(1).execute()
        row = result.data[0] if result.data else None
        if row:
            extra = row.get("extra_data") or {}
            raw = extra.get("rawCookies") or row.get("raw_cookies")
            if raw:
                print(f"[cindy][cookies] Loaded cookies from DB (len={len(raw)})", file=sys.stderr, flush=True)
                return raw
        print("[cindy][cookies] No active li_session found in bot_sessions (or cookies missing)", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[cindy][cookies] DB fetch error: {e}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
    return None


async def _save_cookies_to_db(browser_context) -> None:
    """
    Read the current cookies from the Playwright browser context and write them
    back to cindy_config as rawCookies.

    This is the key to keeping sessions alive across cron ticks: LinkedIn
    refreshes its session cookies via Set-Cookie on every request.  If we don't
    persist those updates, the next tick starts with stale cookies.
    """
    try:
        cookies = await browser_context.cookies()
        parts = []
        # Collect all linkedin.com cookies, preserving the JSESSIONID quoting
        # that LinkedIn's API expects.
        for c in cookies:
            domain = c.get("domain", "")
            if "linkedin.com" not in domain:
                continue
            name = c.get("name", "")
            value = c.get("value", "")
            if not name or not value:
                continue
            if name == "JSESSIONID" and not (value.startswith('"') and value.endswith('"')):
                value = f'"{value}"'
            parts.append(f"{name}={value}")

        if not parts:
            print("[cindy][cookies] No LinkedIn cookies found in browser context to save", file=sys.stderr, flush=True)
            return

        raw_cookies = "; ".join(parts)
        sb = _get_supabase()
        bot_id = _get_cindy_bot_id()
        # Merge into existing extra_data so other fields (name, etc.) are preserved
        existing = sb.from_("bot_sessions").select("extra_data").eq("bot_id", bot_id).eq("session_type", "li_session").order("saved_at", desc=True).limit(1).execute()
        existing_row = existing.data[0] if existing.data else {}
        extra = (existing_row.get("extra_data") or {})
        extra["rawCookies"] = raw_cookies
        sb.from_("bot_sessions").update(
            {"extra_data": extra, "raw_cookies": raw_cookies, "status": "active", "saved_at": datetime.now(timezone.utc).isoformat()},
        ).eq("bot_id", bot_id).eq("session_type", "li_session").execute()
        print(f"[cindy][cookies] Saved {len(parts)} refreshed cookies back to DB ✓", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[cindy][cookies] Failed to save updated cookies: {e}", file=sys.stderr, flush=True)


# ─── LinkedIn header builders ─────────────────────────────────────────────────

def _build_li_track() -> str:
    vp = random.choice(_VIEWPORT_PROFILES)
    ver = random.choice(_LI_CLIENT_VERSIONS)
    return json.dumps({
        "clientVersion": ver, "mpVersion": ver, "osName": "web",
        "timezoneOffset": 5, "timezone": "Asia/Karachi",
        "deviceFormFactor": "DESKTOP", "mpName": "voyager-web",
        "displayDensity": vp["d"], "displayWidth": vp["w"], "displayHeight": vp["h"],
    })


def _chrome_ver(ua: str) -> str:
    m = re.search(r"Chrome/(\d+)", ua)
    return m.group(1) if m else "124"


def _build_inbox_headers(csrf_token: str, ua: str) -> dict:
    cv = _chrome_ver(ua)
    return {
        "csrf-token": csrf_token,
        "Accept": "application/graphql",
        "x-restli-protocol-version": "2.0.0",
        "x-li-lang": "en_US",
        "x-li-track": _build_li_track(),
        "Referer": "https://www.linkedin.com/messaging/",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "sec-fetch-dest": "empty",
        "sec-ch-ua": f'"Chromium";v="{cv}", "Google Chrome";v="{cv}", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": ua,
    }


def _build_send_headers(csrf_token: str, ua: str) -> dict:
    cv = _chrome_ver(ua)
    return {
        "csrf-token": csrf_token,
        "Content-Type": "text/plain;charset=UTF-8",
        "Accept": "application/json",
        "x-restli-protocol-version": "2.0.0",
        "x-li-lang": "en_US",
        "x-li-track": _build_li_track(),
        "Referer": "https://www.linkedin.com/messaging/",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "sec-fetch-dest": "empty",
        "sec-ch-ua": f'"Chromium";v="{cv}", "Google Chrome";v="{cv}", "Not-A.Brand";v="99"',
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-mobile": "?0",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": ua,
        "x-li-sync": "=true",
        "origin": "https://www.linkedin.com",
    }


# ─── JavaScript injected into Playwright page for browser-side fetch ─────────
# Using browser fetch() with credentials:'include' ensures the browser's own
# cookie jar is used — this is the key advantage over Node.js raw fetch calls.

_BROWSER_FETCH_JS = """
async (params) => {
    const { url, method, headers, body } = params;
    console.log('[cindy-browser] Fetching:', method || 'GET', url.substring(0, 80));
    try {
        const init = {
            method: method || 'GET',
            headers: headers || {},
            credentials: 'include',
            redirect: 'follow',
        };
        if (body) init.body = body;

        const response = await fetch(url, init);
        let text = '';
        try { text = await response.text(); } catch(e) { text = ''; }

        const finalUrl = response.url || '';
        const authPaths = ['/login', '/authwall', '/checkpoint', '/challenge', '/platform-change', '/uas/'];
        const isAuthRedirect = response.redirected && authPaths.some(p => finalUrl.includes(p));

        console.log('[cindy-browser] Response:', response.status,
            'redirected:', response.redirected,
            'finalUrl:', finalUrl.substring(0, 60),
            'isAuthRedirect:', isAuthRedirect,
            'bodyLen:', text.length);

        return {
            status: response.status,
            ok: response.ok,
            text: text,
            finalUrl: finalUrl,
            redirected: response.redirected,
            isAuthRedirect: isAuthRedirect,
        };
    } catch(e) {
        console.error('[cindy-browser] Fetch error:', e.toString());
        return { error: e.toString(), status: 0, ok: false, text: '', finalUrl: '' };
    }
}
"""

# ─── Knowledge base ────────────────────────────────────────────────────────────

def _get_knowledge_context() -> str:
    try:
        sb = _get_supabase()
        bot_id = _get_cindy_bot_id()
        result = sb.from_("knowledge_base").select("type,title,content").or_(f"bot_id.eq.{bot_id},bot_id.is.null").execute()
        entries = result.data or []
        if not entries:
            return ""
        parts = [f"[{e['type'].upper()}] {e['title']}:\n{e['content']}" for e in entries]
        return "\n\nCOMPANY KNOWLEDGE BASE:\n" + "\n\n".join(parts)
    except Exception as e:
        print(f"[cindy][kb] Error fetching knowledge context: {e}", file=sys.stderr)
        return ""


# ─── AI reply generation ──────────────────────────────────────────────────────

def _generate_ai_reply(
    chat_history: list,
    knowledge_ctx: str,
    openai_key: str,
) -> tuple:
    """Return (reply_text: str, needs_escalation: bool, escalation_reason: str)."""
    system_prompt = (
        "You are Cindy, a professional LinkedIn assistant for Ammar Sharif at SparkoSol. "
        "Always reply briefly, warmly and professionally. Keep replies under 3 sentences. No emojis.\n\n"
        + knowledge_ctx
        + "\n\n--- ESCALATION RULES (READ CAREFULLY) ---\n"
        "RULE 1 — SOCIAL CHAT: For greetings, small talk, pleasantries, or clearly personal/casual questions "
        "(e.g. 'how are you', weather, compliments) — respond naturally WITHOUT ##ESCALATE##.\n\n"
        "RULE 2 — KB SAYS ESCALATE: If a Knowledge Base entry for the topic says anything like "
        "'Send Notification for Human Approval', 'notify', 'escalate', 'human review', or 'get back to you' — "
        "you MUST include ##ESCALATE## at the end of your reply: "
        "\"<your warm reply here> ##ESCALATE## <short reason>\"\n\n"
        "RULE 3 — UNKNOWN BUSINESS QUESTION: For business questions (pricing, discounts, services, technical "
        "details, availability) that are NOT mentioned anywhere in the Knowledge Base at all — reply: "
        "\"Let me confirm this for you — our team will follow up shortly. ##ESCALATE## <short reason>\"\n\n"
        "RULE 4 — KNOWN, NO ESCALATION: For business questions that the Knowledge Base answers completely "
        "(with no instruction to escalate) — answer using KB content, no ##ESCALATE## needed.\n\n"
        "EXAMPLES:\n"
        "Prospect: \"What are your rates?\" (Not in KB)\n"
        "Reply: \"Let me confirm our current pricing for you — our team will reach out shortly. ##ESCALATE## Pricing inquiry\"\n\n"
        "Prospect: \"Can we jump on a call tomorrow?\" (KB says: Escalate for calls)\n"
        "Reply: \"I'd love to set that up. Let me check the schedule and get back to you! ##ESCALATE## Call request\"\n\n"
        "Prospect: \"Hey there!\" (Social)\n"
        "Reply: \"Hi! How can I help you today?\"\n\n"
        "IMPORTANT: ##ESCALATE## is stripped from the message before sending — the prospect never sees it. "
        "Always err on the side of escalating when a human should be notified."
    )

    messages = [{"role": "system", "content": system_prompt}] + chat_history
    print(f"[cindy][ai] Calling OpenAI with {len(chat_history)} history messages", file=sys.stderr, flush=True)

    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"},
        json={"model": "gpt-4o-mini", "max_tokens": 220, "messages": messages},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    raw_reply = (
        (data.get("choices") or [{}])[0]
        .get("message", {})
        .get("content", "")
        or "Thank you for your message!"
    ).strip()

    print(f"[cindy][ai] Raw reply ({len(raw_reply)} chars): {raw_reply[:80]}...", file=sys.stderr, flush=True)

    has_tag = "##ESCALATE##" in raw_reply
    has_phrase = "let me confirm this for you" in raw_reply.lower()

    if has_tag or has_phrase:
        escalation_reason = "Out-of-context query"
        if has_tag:
            parts = raw_reply.split("##ESCALATE##")
            raw_reply = parts[0].strip()
            escalation_reason = (parts[1] or "").strip()
        print(f"[cindy][ai] Escalation triggered: {escalation_reason}", file=sys.stderr, flush=True)
        return raw_reply, True, escalation_reason

    return raw_reply, False, ""


# ─── Node.js SSE bridge ────────────────────────────────────────────────────────
# Python writes to Supabase but the Node.js SSE system doesn't get DB triggers.
# This fires a lightweight HTTP call to the Node.js internal endpoint so the
# frontend gets an immediate push notification (session-expired, escalation, etc.)

def _notify_node(event_type: str, bot_name: str = "cindy", platform: str = "LinkedIn", data: dict = None) -> None:
    node_url = os.environ.get("NODE_BACKEND_URL", "http://localhost:3005")
    try:
        requests.post(
            f"{node_url}/api/notification/internal",
            json={"type": event_type, "botName": bot_name, "platform": platform, "data": data or {}},
            timeout=5,
        )
        print(f"[cindy][notify-node] Sent '{event_type}' to Node.js SSE bridge", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[cindy][notify-node] Failed to notify Node.js (non-fatal): {e}", file=sys.stderr)


# ─── Escalation ───────────────────────────────────────────────────────────────

def _create_escalation(conversation_id: str, sender_name: str, last_message: str, reason: str, sender_id: str = "") -> None:
    """
    Create or skip duplicate escalation.
    Ensures the conversation row exists first so escalations.conversation_id FK is satisfied.
    """
    try:
        sb = _get_supabase()
        bot_id = _get_cindy_bot_id()
        now_iso = datetime.now(timezone.utc).isoformat()

        # ── Upsert conversation row (may not exist yet when escalation fires) ──
        conv_result = sb.from_("conversations").select("id").eq("bot_id", bot_id).eq("external_conversation_id", conversation_id).maybe_single().execute()
        if conv_result.data:
            conv_uuid = conv_result.data["id"]
        else:
            new_conv = sb.from_("conversations").insert({
                "bot_id": bot_id,
                "external_conversation_id": conversation_id,
                "sender_id": sender_id or conversation_id,
                "sender_name": sender_name,
                "last_activity": now_iso,
            }).select("id").single().execute()
            conv_uuid = new_conv.data["id"] if new_conv.data else None
            print(f"[cindy][escalation] Created conversation stub for {sender_name}", file=sys.stderr, flush=True)

        # ── Skip if pending escalation already exists for this conversation ──
        if conv_uuid:
            existing = sb.from_("escalations").select("id").eq("status", "pending").eq("conversation_id", conv_uuid).limit(1).execute()
            if existing.data:
                print(f"[cindy][escalation] Already pending — re-notifying UI for {sender_name}", file=sys.stderr, flush=True)
                _notify_node("escalation", "cindy", "LinkedIn", {"senderName": sender_name, "reason": reason})
                return

        sb.from_("escalations").insert({
            "bot_id": bot_id,
            "conversation_id": conv_uuid,
            "platform": "LinkedIn",
            "sender_name": sender_name,
            "last_message": last_message,
            "reason": reason,
            "status": "pending",
        }).execute()
        print(f"[cindy][escalation] ✓ Created for {sender_name}: {reason}", file=sys.stderr, flush=True)
        _notify_node("escalation", "cindy", "LinkedIn", {"senderName": sender_name, "reason": reason})
    except Exception as e:
        print(f"[cindy][escalation] ERROR: {e}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        # Still fire SSE so frontend badge updates; the poll will confirm DB state
        _notify_node("escalation", "cindy", "LinkedIn", {"senderName": sender_name, "reason": reason})


# ─── Session alerts ────────────────────────────────────────────────────────────

def _create_session_alert() -> None:
    try:
        sb = _get_supabase()
        bot_id = _get_cindy_bot_id()
        existing = sb.from_("session_alerts").select("id").eq("bot_id", bot_id).eq("status", "pending").limit(1).execute()
        if existing.data:
            return
        sb.from_("session_alerts").insert({"bot_id": bot_id, "platform": "LinkedIn", "status": "pending"}).execute()
        print("[cindy][session-alert] Created session alert", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[cindy][session-alert] Error: {e}", file=sys.stderr)
    # Notify Node.js regardless of DB success so SSE fires even if insert was duplicate
    _notify_node("session-expired", "cindy", "LinkedIn")


def _resolve_session_alert() -> None:
    try:
        sb = _get_supabase()
        bot_id = _get_cindy_bot_id()
        sb.from_("session_alerts").update({"status": "resolved", "resolved_at": datetime.now(timezone.utc).isoformat()}).eq("bot_id", bot_id).eq("status", "pending").execute()
        print("[cindy][session-alert] Resolved session alert", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[cindy][session-alert] Resolve error: {e}", file=sys.stderr)
    _notify_node("session-restored", "cindy", "LinkedIn")


# ─── Processed message persistence ────────────────────────────────────────────

def _load_persisted_processed_ids() -> None:
    """Load recent processed message IDs from DB into in-memory set."""
    try:
        sb = _get_supabase()
        bot_id = _get_cindy_bot_id()
        result = sb.from_("processed_messages").select("external_msg_id").eq("bot_id", bot_id).limit(500).execute()
        loaded = 0
        for d in (result.data or []):
            if d.get("external_msg_id"):
                _state["processed_message_ids"].add(d["external_msg_id"])
                loaded += 1
        print(f"[cindy][processed] Loaded {loaded} processed IDs from DB", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[cindy][processed] Load error: {e}", file=sys.stderr)


def _persist_processed_id(msg_id: str, conversation_urn: str) -> None:
    try:
        sb = _get_supabase()
        bot_id = _get_cindy_bot_id()
        sb.from_("processed_messages").upsert(
            {"bot_id": bot_id, "external_msg_id": msg_id, "external_conversation_id": conversation_urn},
            on_conflict="bot_id,external_msg_id",
        ).execute()
    except Exception as e:
        print(f"[cindy][processed] Persist error: {e}", file=sys.stderr)


# ─── Follow-up helpers ────────────────────────────────────────────────────────

def _render_template(text: str, vars_dict: dict) -> str:
    return re.sub(r"\{\{(\w+)\}\}", lambda m: vars_dict.get(m.group(1), m.group(0)), text)


def _mark_follow_up_replied(conversation_urn: str) -> None:
    try:
        sb = _get_supabase()
        bot_id = _get_cindy_bot_id()
        now = datetime.now(timezone.utc).isoformat()
        sb.from_("follow_up_records").update(
            {"status": "replied", "reply_received": True, "reply_received_at": now, "updated_at": now}
        ).eq("bot_id", bot_id).eq("user_id", conversation_urn).in_("status", ["active", "paused"]).execute()
        print(f"[cindy][followup] Marked follow-up(s) replied for {conversation_urn[:40]}", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[cindy][followup] mark_replied error: {e}", file=sys.stderr)


def _message_matches_follow_up_rule(message_text: str) -> bool:
    try:
        sb = _get_supabase()
        bot_id = _get_cindy_bot_id()
        result = sb.from_("follow_up_rules").select("value").eq("bot_id", bot_id).eq("enabled", True).execute()
        rules = result.data or []
        lower = message_text.lower()
        matched = any(r.get("value", "").lower() in lower for r in rules)
        print(f"[cindy][followup] Rule match for '{message_text[:40]}': {matched} ({len(rules)} rules)", file=sys.stderr, flush=True)
        return matched
    except Exception as e:
        print(f"[cindy][followup] rule match error: {e}", file=sys.stderr)
        return False


def _register_follow_up(
    conversation_urn: str,
    sender_name: str,
    reply_text: str,
    incoming_user_messages: list,
) -> bool:
    """Register a follow-up chain if the message matches configured rules."""
    try:
        is_escalation = (
            "let me confirm this for you" in reply_text.lower()
            or "##ESCALATE##" in reply_text
        )
        text_to_match = " ".join(m for m in incoming_user_messages if m) or reply_text

        if not is_escalation and not _message_matches_follow_up_rule(text_to_match):
            print(f"[cindy][followup] No follow-up registered for {sender_name} — no rule match", file=sys.stderr, flush=True)
            return False

        sb = _get_supabase()
        bot_id = _get_cindy_bot_id()

        # Block if already replied
        replied = sb.from_("follow_up_records").select("id").eq("bot_id", bot_id).eq("user_id", conversation_urn).eq("status", "replied").limit(1).execute()
        if replied.data:
            print(f"[cindy][followup] Skipping follow-up — {sender_name} already replied", file=sys.stderr, flush=True)
            return False

        now = datetime.now(timezone.utc).isoformat()
        next_scheduled = datetime.fromtimestamp(
            time.time() + FOLLOWUP_HOURS[0] * 3600, tz=timezone.utc
        ).isoformat()
        msg_id = f"msg_{int(time.time() * 1000)}"

        # Upsert: reset existing active/paused or insert new
        existing = sb.from_("follow_up_records").select("id").eq("bot_id", bot_id).eq("user_id", conversation_urn).in_("status", ["active", "paused"]).limit(1).execute()
        if existing.data:
            sb.from_("follow_up_records").update({
                "user_name": sender_name, "original_message_id": msg_id,
                "original_message_text": reply_text, "original_message_sent_at": now,
                "reply_received": False, "follow_ups_sent": 0,
                "next_followup_scheduled_at": next_scheduled, "status": "active", "updated_at": now,
            }).eq("id", existing.data[0]["id"]).execute()
        else:
            sb.from_("follow_up_records").insert({
                "bot_id": bot_id, "user_id": conversation_urn, "user_name": sender_name,
                "original_message_id": msg_id, "original_message_text": reply_text,
                "original_message_sent_at": now, "reply_received": False, "follow_ups_sent": 0,
                "next_followup_scheduled_at": next_scheduled, "status": "active",
            }).execute()

        print(f"[cindy][followup] Registered follow-up for {sender_name}, next at {next_scheduled}", file=sys.stderr, flush=True)
        return True
    except Exception as e:
        print(f"[cindy][followup] registerFollowUp error: {e}\n{traceback.format_exc()}", file=sys.stderr)
        return False


async def _process_follow_ups(page, csrf_token: str, ua: str) -> None:
    """Check for due follow-ups and send them via the Playwright page."""
    try:
        sb = _get_supabase()
        bot_id = _get_cindy_bot_id()
        now = datetime.now(timezone.utc).isoformat()

        result = sb.from_("follow_up_records").select("*").eq("bot_id", bot_id).eq("status", "active").eq("reply_received", False).lte("next_followup_scheduled_at", now).execute()
        due = result.data or []

        if not due:
            print("[cindy][followup] No follow-ups due", file=sys.stderr, flush=True)
            return

        add_log(f"{len(due)} follow-up(s) due — dispatching...", "info")

        for record in due:
            follow_up_number = record.get("follow_ups_sent", 0) + 1
            print(f"[cindy][followup] Sending follow-up #{follow_up_number} to {record.get('user_name', '?')}", file=sys.stderr, flush=True)

            if follow_up_number > 5:
                sb.from_("follow_up_records").update({"status": "completed", "updated_at": datetime.now(timezone.utc).isoformat()}).eq("id", record["id"]).execute()
                continue

            tmpl = sb.from_("follow_up_templates").select("message_text").eq("bot_id", bot_id).eq("follow_up_number", follow_up_number).maybe_single().execute()
            template_text = (tmpl.data["message_text"] if tmpl.data else None) or DEFAULT_FOLLOWUP_TEMPLATES.get(follow_up_number, "Hi {{user_name}}, just following up!")

            original_sent_at = record.get("original_message_sent_at", now)
            try:
                days_waiting = max(0, int(
                    (time.time() - datetime.fromisoformat(original_sent_at.replace("Z", "+00:00")).timestamp())
                    / 86400
                ))
            except Exception:
                days_waiting = 0

            message_text = _render_template(template_text, {
                "user_name": record.get("user_name", "there"),
                "original_message": record.get("original_message_text", ""),
                "days_waiting": str(days_waiting),
            })

            sent = await _send_linkedin_message(page, csrf_token, ua, record["user_id"], message_text)

            now_iso = datetime.now(timezone.utc).isoformat()
            new_sent = record.get("follow_ups_sent", 0) + 1
            is_last = new_sent >= 5
            next_idx = min(new_sent, len(FOLLOWUP_HOURS) - 1)
            next_scheduled = (
                None if is_last
                else datetime.fromtimestamp(
                    time.time() + FOLLOWUP_HOURS[next_idx] * 3600, tz=timezone.utc
                ).isoformat()
            )

            if sent:
                sb.from_("follow_up_records").update({
                    "follow_ups_sent": new_sent,
                    "last_followup_sent_at": now_iso,
                    "next_followup_scheduled_at": next_scheduled or now_iso,
                    "status": "completed" if is_last else "active",
                    "updated_at": now_iso,
                }).eq("id", record["id"]).execute()
                suffix = " (final)" if is_last else ""
                add_log(f"Follow-up #{follow_up_number} sent to {record.get('user_name', 'unknown')}{suffix}", "success")
                _notify_node("followup-sent", "cindy", "LinkedIn", {
                    "userName": record.get("user_name"),
                    "followUpNumber": follow_up_number,
                    "isFinal": is_last,
                })
            else:
                add_log(f"Follow-up #{follow_up_number} failed for {record.get('user_name', 'unknown')} — will retry", "warning")

    except Exception as e:
        add_log(f"processFollowUps error: {str(e)[:100]}", "error")
        print(f"[cindy][followup] Error:\n{traceback.format_exc()}", file=sys.stderr)


# ─── LinkedIn message sender (runs inside Playwright browser context) ─────────

async def _send_linkedin_message(page, csrf_token: str, ua: str, conversation_urn: str, reply_text: str) -> bool:
    """Send a LinkedIn message using browser-side fetch() via page.evaluate()."""
    raw_thread_id = conversation_urn.replace("urn:li:messagingThread:", "")
    full_conversation_urn = f"urn:li:msg_conversation:({MY_PROFILE_URN},{raw_thread_id})"

    send_body = json.dumps({
        "message": {
            "body": {"attributes": [], "text": reply_text},
            "renderContentUnions": [],
            "conversationUrn": full_conversation_urn,
            "originToken": str(uuid.uuid4()),
        },
        "mailboxUrn": MY_PROFILE_URN,
        "trackingId": uuid.uuid4().hex[:16],
        "dedupeByClientGeneratedToken": False,
    })

    headers = _build_send_headers(csrf_token, ua)
    print(f"[cindy][send] Sending to {conversation_urn[:50]}...", file=sys.stderr, flush=True)
    print(f"[cindy][send] Reply text ({len(reply_text)} chars): {reply_text[:60]}...", file=sys.stderr, flush=True)

    result = await page.evaluate(_BROWSER_FETCH_JS, {
        "url": LINKEDIN_SEND_URL,
        "method": "POST",
        "headers": headers,
        "body": send_body,
    })

    print(
        f"[cindy][send] Result: status={result.get('status')}, ok={result.get('ok')}, "
        f"isAuthRedirect={result.get('isAuthRedirect')}, error={result.get('error', 'none')}",
        file=sys.stderr, flush=True,
    )

    if result.get("error"):
        print(f"[cindy][send] JS fetch error: {result['error']}", file=sys.stderr)
        return False

    if result.get("isAuthRedirect"):
        print("[cindy][send] Auth redirect on send — session may have expired", file=sys.stderr)
        return False

    return bool(result.get("ok"))


# ─── Core cron tick (async) ────────────────────────────────────────────────────

async def _cron_tick_async(force: bool = False) -> None:
    """
    Full inbox processing tick.
    force=True: bypass the running-state guard inside the message loop (used by trigger_tick).
    1. Check/restore suspended session.
    2. Load cookies from DB.
    3. Launch Playwright browser, inject cookies.
    4. Warm up: navigate to messaging page.
    5. Fetch inbox via browser-side fetch().
    6. For each unread conversation: generate AI reply, send, register follow-up.
    7. Process due follow-ups.
    """

    # ── 1. Session suspension check ─────────────────────────────────────────────
    if _state["session_suspended"]:
        raw = _get_cookies_from_db()
        if raw:
            add_log("LinkedIn session restored — resuming Cindy bot", "success")
            _state["session_suspended"] = False
            _resolve_session_alert()
        else:
            add_log("LinkedIn session still expired — waiting for renewal", "warning")
            return

    # ── 2. Load cookies ──────────────────────────────────────────────────────────
    raw_cookies = _get_cookies_from_db()
    if not raw_cookies:
        add_log("No LinkedIn cookies available in DB", "error")
        return

    csrf_token = extract_jsession_id(raw_cookies)
    if not csrf_token:
        add_log("Could not extract JSESSIONID from cookies", "error")
        return

    # ── 3. Tick-lock check ───────────────────────────────────────────────────────
    if _state["tick_running"]:
        started_at = _state.get("tick_started_at") or 0
        running_for = time.time() - (started_at if isinstance(started_at, (int, float)) else 0)
        if running_for < 10 * 60:
            add_log(f"Tick already running ({int(running_for)}s) — skipping", "warning")
            return
        add_log("Stale tick lock — force resetting", "warning")
        _state["processing_convs"] = set()

    _state["tick_running"] = True
    _state["tick_started_at"] = time.time()
    _state["last_run"] = datetime.now(timezone.utc).isoformat()
    add_log("Checking for unread messages (Python Playwright)...", "info")

    _load_persisted_processed_ids()

    # Human-like jitter before hitting LinkedIn
    jitter = random.uniform(0.8, 4.0)
    print(f"[cindy][tick] Human jitter: {jitter:.1f}s", file=sys.stderr, flush=True)
    await asyncio.sleep(jitter)

    playwright_cookies = cookie_string_to_playwright(raw_cookies)
    ua = random.choice(_CHROME_UAS)

    from linkedin_scraper.core.browser import BrowserManager

    try:
        async with BrowserManager(headless=True) as browser:
            print("[cindy][browser] Browser launched", file=sys.stderr, flush=True)

            # ── 3. Inject cookies ────────────────────────────────────────────────
            await browser.context.add_cookies(playwright_cookies)
            print(f"[cindy][browser] Injected {len(playwright_cookies)} cookies into context", file=sys.stderr, flush=True)

            # ── 4. Warm-up: navigate to messaging page ───────────────────────────
            add_log("Warming up: navigating to LinkedIn messaging...", "info")
            warmup_ok = False
            try:
                await browser.page.goto(WARMUP_URL, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(random.uniform(1.5, 3.0))
                current_url = browser.page.url
                print(f"[cindy][warmup] Landed on: {current_url}", file=sys.stderr, flush=True)

                if any(x in current_url for x in ["/login", "/authwall", "/checkpoint", "/uas/"]):
                    add_log(f"Session expired — redirected to {current_url.split('linkedin.com')[-1][:40]}", "error")
                    _create_session_alert()
                    _state["session_suspended"] = True
                    _state["network_errors"] = 0
                    return

                add_log(f"Warm-up OK — landed on {current_url.split('linkedin.com')[-1][:40]}", "info")
                warmup_ok = True

            except Exception as wu_e:
                wu_str = str(wu_e).lower()
                print(f"[cindy][warmup] Exception: {wu_e}", file=sys.stderr, flush=True)

                if "err_too_many_redirects" in wu_str or ("redirect" in wu_str and "authentication" not in wu_str):
                    add_log("Redirect loop on warm-up — treating as session expired", "error")
                    _create_session_alert()
                    _state["session_suspended"] = True
                    return

                # Non-fatal warmup issue — proceed and let the API call confirm
                add_log(f"Warm-up warning (proceeding): {str(wu_e)[:80]}", "warning")

            if not warmup_ok:
                add_log("Warm-up inconclusive — proceeding with inbox fetch", "warning")

            # ── 5. Fetch inbox via browser-side fetch() ──────────────────────────
            inbox_headers = _build_inbox_headers(csrf_token, ua)
            print(f"[cindy][inbox] Calling LinkedIn Voyager inbox API...", file=sys.stderr, flush=True)

            inbox_result = await browser.page.evaluate(_BROWSER_FETCH_JS, {
                "url": LINKEDIN_INBOX_URL,
                "method": "GET",
                "headers": inbox_headers,
            })

            print(
                f"[cindy][inbox] API result: status={inbox_result.get('status')}, "
                f"ok={inbox_result.get('ok')}, bodyLen={len(inbox_result.get('text',''))}, "
                f"error={inbox_result.get('error','none')}",
                file=sys.stderr, flush=True,
            )

            # ── Error / auth checks ──────────────────────────────────────────────
            if inbox_result.get("error"):
                _state["network_errors"] = _state.get("network_errors", 0) + 1
                err_count = _state["network_errors"]
                add_log(f"Network/JS error: {inbox_result['error'][:100]} — attempt {err_count}/5", "warning")
                if err_count >= 5:
                    add_log("5 consecutive network failures — suspending cron", "error")
                    _create_session_alert()
                    _state["session_suspended"] = True
                    _state["network_errors"] = 0
                return

            if inbox_result.get("isAuthRedirect"):
                add_log("LinkedIn redirected to auth page — session expired, suspending", "error")
                _create_session_alert()
                _state["session_suspended"] = True
                _state["network_errors"] = 0
                return

            status_code = inbox_result.get("status", 0)
            if not inbox_result.get("ok"):
                if status_code == 401:
                    add_log("LinkedIn cookies expired (401) — suspending cron", "error")
                    _create_session_alert()
                    _state["session_suspended"] = True
                elif status_code == 403:
                    add_log("LinkedIn blocked request (403) — suspending cron", "error")
                    _create_session_alert()
                    _state["session_suspended"] = True
                elif status_code == 0:
                    _state["network_errors"] = _state.get("network_errors", 0) + 1
                    add_log(f"No response from LinkedIn API — network error {_state['network_errors']}/5", "warning")
                    if _state["network_errors"] >= 5:
                        add_log("5 consecutive network failures — suspending cron", "error")
                        _create_session_alert()
                        _state["session_suspended"] = True
                        _state["network_errors"] = 0
                else:
                    add_log(f"LinkedIn API error: {status_code}", "error")
                return

            _state["network_errors"] = 0

            # ── Parse inbox JSON ─────────────────────────────────────────────────
            raw_body = inbox_result.get("text", "{}")
            print(f"[cindy][inbox] Raw body (first 300): {raw_body[:300]}", file=sys.stderr, flush=True)

            try:
                data = json.loads(raw_body)
            except json.JSONDecodeError as je:
                add_log(f"Failed to parse inbox JSON: {je}", "error")
                print(f"[cindy][inbox] JSON parse error. Raw (500): {raw_body[:500]}", file=sys.stderr, flush=True)
                return

            elements = (
                data.get("data", {}).get("messengerConversationsBySyncToken", {}).get("elements")
                or data.get("messengerConversationsBySyncToken", {}).get("elements")
                or []
            )
            print(f"[cindy][inbox] Got {len(elements)} conversation elements", file=sys.stderr, flush=True)

            if not elements:
                add_log("No conversations returned", "info")
                await _process_follow_ups(browser.page, csrf_token, ua)
                return

            openai_key = os.environ.get("OPENAI_API_KEY", "")
            if not openai_key:
                add_log("OPENAI_API_KEY not set — cannot generate replies", "error")
                return

            knowledge_ctx = _get_knowledge_context()
            new_messages = 0

            # ── 6. Process each unread conversation ─────────────────────────────
            for conv_idx, conv in enumerate(elements):
                if not force and not _state["running"]:
                    add_log("Cron stopped mid-execution", "warning")
                    return

                unread_count = conv.get("unreadCount", 0)
                is_read = conv.get("read") is True
                conv_urn_short = str(conv.get("backendUrn", ""))[:50]
                print(
                    f"[cindy][tick] Conv {conv_idx+1}/{len(elements)}: unreadCount={unread_count}, "
                    f"read={is_read}, urn={conv_urn_short}",
                    file=sys.stderr, flush=True,
                )

                if not unread_count or is_read:
                    print(f"[cindy][tick] ↳ SKIP: unreadCount={unread_count}, read={is_read}", file=sys.stderr, flush=True)
                    continue

                # Human-like pause between conversations
                pause = random.uniform(1.2, 3.5)
                print(f"[cindy][tick] Between-conv pause: {pause:.1f}s", file=sys.stderr, flush=True)
                await asyncio.sleep(pause)

                messages = conv.get("messages", {}).get("elements", [])
                if not messages:
                    continue
                latest = messages[0]

                # Find the latest message from the prospect among all elements
                # (Sometimes the latest message [0] is from us if we replied but didn't mark as read)
                prospect_messages = [
                    m for m in messages
                    if (m.get("actor", {}).get("hostIdentityUrn") or m.get("sender", {}).get("hostIdentityUrn")) != MY_PROFILE_URN
                ]
                if not prospect_messages:
                    print(f"[cindy][tick] ↳ SKIP: no recent messages found from the prospect", file=sys.stderr, flush=True)
                    continue

                # Use the latest prospect message for analysis
                latest_prospect_msg = prospect_messages[0]
                latest_from_prospect = (latest_prospect_msg.get("actor", {}).get("hostIdentityUrn") or latest_prospect_msg.get("sender", {}).get("hostIdentityUrn"))
                latest_is_us = (latest.get("actor", {}).get("hostIdentityUrn") or latest.get("sender", {}).get("hostIdentityUrn")) == MY_PROFILE_URN

                if latest_is_us:
                    print(f"[cindy][tick] ↳ INFO: Latest message overall is from us, but there are unread messages from prospect. Processing...", file=sys.stderr, flush=True)

                message_text = (latest_prospect_msg.get("body", {}).get("text") or "").strip()
                msg_id = latest_prospect_msg.get("backendUrn") or str(latest_prospect_msg.get("deliveredAt") or "")
                sender_urn = latest_from_prospect

                print(f"[cindy][tick] ↳ Processing prospect message: {message_text[:80]!r}", file=sys.stderr, flush=True)
                if not message_text:
                    print(f"[cindy][tick] ↳ SKIP: empty message text from prospect", file=sys.stderr, flush=True)
                    continue

                if msg_id in _state["processed_message_ids"]:
                    print(f"[cindy][tick] ↳ SKIP: msg_id {msg_id[:40]} already processed", file=sys.stderr, flush=True)
                    continue

                actor = latest_prospect_msg.get("actor") or latest_prospect_msg.get("sender") or {}
                member = (
                    actor.get("participantType", {})
                    .get("member", {})
                )
                sender_first = member.get("firstName", {}).get("text") or "there"
                sender_last = member.get("lastName", {}).get("text") or ""
                sender_name = f"{sender_first} {sender_last}".strip()
                conversation_urn = conv.get("backendUrn", "")

                add_log(f'New message from {sender_name}: "{message_text[:60]}..."', "info")
                print(f"[cindy][tick] conversationUrn: {conversation_urn[:60]}", file=sys.stderr, flush=True)

                if conversation_urn in _state["processing_convs"]:
                    add_log(f"Skipping {sender_name} — already being processed", "warning")
                    continue

                # In-memory dedup guard (DB lock removed — processing_convs Set is sufficient)
                _state["processing_convs"].add(conversation_urn)

                try:
                    _mark_follow_up_replied(conversation_urn)

                    # Build chat history (last 10 messages, chronological)
                    chat_history = []
                    for m in list(reversed(messages[:10])):
                        m_text = (m.get("body", {}).get("text") or "").strip()
                        if not m_text:
                            continue
                        m_sender = (
                            m.get("actor", {}).get("hostIdentityUrn")
                            or m.get("sender", {}).get("hostIdentityUrn")
                            or ""
                        )
                        role = "assistant" if m_sender == MY_PROFILE_URN else "user"
                        chat_history.append({"role": role, "content": m_text})

                    print(f"[cindy][tick] Chat history: {len(chat_history)} messages", file=sys.stderr, flush=True)

                    # Generate AI reply
                    raw_reply, needs_escalation, escalation_reason = _generate_ai_reply(
                        chat_history, knowledge_ctx, openai_key
                    )

                    if needs_escalation:
                        _create_escalation(conversation_urn, sender_name, message_text, escalation_reason, sender_urn)
                        add_log(f"Escalated {sender_name}: {escalation_reason}", "info")

                    reply_text = raw_reply.replace('"', "'").replace("\n", " ").replace("\r", "")

                    # Human "composing" delay
                    compose_pause = random.uniform(1.5, 4.0)
                    print(f"[cindy][tick] Composing pause: {compose_pause:.1f}s", file=sys.stderr, flush=True)
                    await asyncio.sleep(compose_pause)

                    sent = await _send_linkedin_message(browser.page, csrf_token, ua, conversation_urn, reply_text)

                    if sent:
                        _state["processed_message_ids"].add(msg_id)
                        _persist_processed_id(msg_id, conversation_urn)
                        new_messages += 1
                        add_log(f'Replied to {sender_name}: "{reply_text[:50]}..."', "success")

                        # Last 2 user messages for follow-up rule matching
                        last_two_user = [
                            m.get("body", {}).get("text", "").strip()
                            for m in messages
                            if (
                                m.get("actor", {}).get("hostIdentityUrn")
                                or m.get("sender", {}).get("hostIdentityUrn")
                                or ""
                            ) != MY_PROFILE_URN
                            and (m.get("body", {}).get("text") or "").strip()
                        ][:2]

                        _register_follow_up(conversation_urn, sender_name, reply_text, last_two_user)

                        # Log to Supabase conversations + messages tables
                        try:
                            sb = _get_supabase()
                            bot_id = _get_cindy_bot_id()
                            now_iso = datetime.now(timezone.utc).isoformat()
                            # Get or create conversation row
                            conv_row = sb.from_("conversations").select("id").eq("bot_id", bot_id).eq("external_conversation_id", conversation_urn).maybe_single().execute()
                            if conv_row.data:
                                conv_id = conv_row.data["id"]
                                sb.from_("conversations").update({"last_activity": now_iso, "sender_name": sender_name}).eq("id", conv_id).execute()
                            else:
                                new_conv = sb.from_("conversations").insert({
                                    "bot_id": bot_id, "external_conversation_id": conversation_urn,
                                    "sender_id": sender_urn, "sender_name": sender_name,
                                    "last_activity": now_iso,
                                }).select("id").single().execute()
                                conv_id = new_conv.data["id"]
                            sb.from_("messages").insert([
                                {"conversation_id": conv_id, "role": "prospect", "text": message_text, "source": "linkedin_inbox", "sent_at": now_iso},
                                {"conversation_id": conv_id, "role": "cindy", "text": reply_text, "source": "cindy_auto", "sent_at": now_iso},
                            ]).execute()
                        except Exception as log_err:
                            add_log(f"Failed to log conversation for {sender_name}: {str(log_err)[:200]}", "warning")
                            print(f"[cindy][conv-log] Full error: {log_err}", file=sys.stderr, flush=True)

                        # Post-send pause
                        await asyncio.sleep(3.0)

                    else:
                        add_log(f"Failed to send reply to {sender_name}", "error")
                        print(f"[cindy][tick] Send failed — check logs above for details", file=sys.stderr, flush=True)

                except Exception as conv_err:
                    add_log(f"Error processing {sender_name}: {str(conv_err)[:100]}", "error")
                    print(f"[cindy][tick] Conv error:\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                finally:
                    _state["processing_convs"].discard(conversation_urn)

            # ── 7. Follow-ups ────────────────────────────────────────────────────
            try:
                await _process_follow_ups(browser.page, csrf_token, ua)
            except Exception as fu_err:
                print(f"[cindy][followup] Error: {fu_err}\n{traceback.format_exc()}", file=sys.stderr)

            # ── 8. Persist refreshed cookies ─────────────────────────────────────
            # LinkedIn updates session cookies on every request via Set-Cookie.
            # We save whatever the browser context has now so the next tick
            # starts with fresh, non-expired cookies — same as Scraper does.
            await _save_cookies_to_db(browser.context)

            add_log(f"Tick complete. New replies: {new_messages}", "info")

    except Exception as tick_err:
        add_log(f"Cron tick error: {str(tick_err)[:150]}", "error")
        print(f"[cindy][tick] Unhandled error:\n{traceback.format_exc()}", file=sys.stderr, flush=True)
    finally:
        await asyncio.sleep(3.0)
        _state["tick_running"] = False
        _state["tick_started_at"] = None
        _state["processing_convs"] = set()
        print("[cindy][tick] Tick finished, lock released", file=sys.stderr, flush=True)


# ─── Synchronous wrapper (run inside background thread) ───────────────────────

def _cron_tick_sync(force: bool = False) -> None:
    """Synchronous entry point for the cron tick — spawns its own event loop."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        asyncio.run(_cron_tick_async(force=force))
    except Exception as e:
        add_log(f"Cron tick fatal error: {str(e)[:100]}", "error")
        print(f"[cindy][tick] Fatal:\n{traceback.format_exc()}", file=sys.stderr, flush=True)


# ─── Cron state persistence ───────────────────────────────────────────────────

def _save_cron_state(running: bool, interval_seconds: int) -> None:
    """Persist cron running state to bot_settings so Python server restarts auto-resume."""
    try:
        sb = _get_supabase()
        bot_id = _get_cindy_bot_id()
        current = sb.from_("bot_settings").select("data").eq("bot_id", bot_id).maybe_single().execute()
        existing_data = (current.data or {}).get("data") or {}
        merged = {
            **existing_data,
            "cindy_cron": {
                **(existing_data.get("cindy_cron") or {}),
                "running": running,
                "intervalSeconds": interval_seconds,
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            },
        }
        sb.from_("bot_settings").upsert(
            {"bot_id": bot_id, "data": merged, "updated_at": datetime.now(timezone.utc).isoformat()},
            on_conflict="bot_id",
        ).execute()
        print(f"[cindy][cron-state] Persisted running={running}, interval={interval_seconds}s", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[cindy][cron-state] Failed to persist state: {e}", file=sys.stderr, flush=True)


def _load_cron_state() -> tuple:
    """Load persisted cron state. Returns (should_run: bool, interval_seconds: int)."""
    try:
        sb = _get_supabase()
        bot_id = _get_cindy_bot_id()
        result = sb.from_("bot_settings").select("data").eq("bot_id", bot_id).maybe_single().execute()
        if result.data:
            cron = (result.data.get("data") or {}).get("cindy_cron") or {}
            if cron:
                return bool(cron.get("running", False)), int(cron.get("intervalSeconds", 60))
    except Exception as e:
        print(f"[cindy][cron-state] Failed to load state: {e}", file=sys.stderr, flush=True)
    return False, 60


# ─── Background cron thread ────────────────────────────────────────────────────

# Stored so the runner can read it if interval changes (future-proofing)
_cron_interval_seconds: int = 60


def _cron_runner(interval_seconds: int) -> None:
    """
    Long-running background thread.
    Runs one tick, waits interval_seconds, repeats — until stop_event is set.
    Keeps running as long as _state["running"] is True.
    """
    tick_count = 0
    print(f"[cindy][cron] ─── Runner started (interval={interval_seconds}s) ───", file=sys.stderr, flush=True)

    while not _stop_event.is_set():
        if not _state["running"]:
            print("[cindy][cron] running=False detected inside loop — exiting runner", file=sys.stderr, flush=True)
            break

        tick_count += 1
        print(f"[cindy][cron] ─── Tick #{tick_count} starting ───", file=sys.stderr, flush=True)
        try:
            _cron_tick_sync()
        except Exception as e:
            print(f"[cindy][cron] Runner tick exception (continuing): {e}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            add_log(f"Cron runner error (continuing): {str(e)[:80]}", "error")

        if _stop_event.is_set() or not _state["running"]:
            print("[cindy][cron] Stop detected after tick — exiting runner", file=sys.stderr, flush=True)
            break

        print(f"[cindy][cron] ─── Tick #{tick_count} done — waiting {interval_seconds}s for next tick ───", file=sys.stderr, flush=True)
        # Wait for the interval OR a stop signal (whichever comes first)
        was_stopped = _stop_event.wait(timeout=interval_seconds)
        if was_stopped:
            print("[cindy][cron] Stop event received during wait — exiting runner", file=sys.stderr, flush=True)
            break

        print(f"[cindy][cron] ─── Interval elapsed — starting tick #{tick_count + 1} ───", file=sys.stderr, flush=True)

    print(f"[cindy][cron] ─── Runner exited after {tick_count} tick(s) ───", file=sys.stderr, flush=True)


# ─── Public API ───────────────────────────────────────────────────────────────

def start_cron(interval_seconds: int = 60) -> None:
    """Start the Cindy inbox cron in a background daemon thread."""
    global _cron_thread, _cron_interval_seconds

    # If thread is still alive from a previous start, don't double-start
    if _cron_thread is not None and _cron_thread.is_alive():
        add_log(f"Cindy cron already running (thread alive, interval={interval_seconds}s)", "warning")
        print(f"[cindy][cron] start_cron called but thread {_cron_thread.name} is still alive — skipping", file=sys.stderr, flush=True)
        return

    _cron_interval_seconds = interval_seconds
    _state["running"] = True
    _state["session_suspended"] = False
    _state["log"] = []
    _state["tick_running"] = False
    _stop_event.clear()

    add_log(f"Cindy inbox cron started (Python Playwright, interval={interval_seconds}s)", "success")
    print(f"[cindy][cron] Starting cron thread with interval={interval_seconds}s", file=sys.stderr, flush=True)

    _cron_thread = threading.Thread(
        target=_cron_runner,
        args=(interval_seconds,),
        daemon=True,
        name="cindy-cron",
    )
    _cron_thread.start()
    print(f"[cindy][cron] Daemon thread started: {_cron_thread.name} (alive={_cron_thread.is_alive()})", file=sys.stderr, flush=True)

    # Persist to DB so auto-resume works after server restart
    threading.Thread(target=_save_cron_state, args=(True, interval_seconds), daemon=True).start()


def stop_cron() -> None:
    """Stop the Cindy inbox cron."""
    global _cron_thread
    _state["running"] = False
    _stop_event.set()
    add_log("Cindy inbox cron stopped", "info")
    print("[cindy][cron] Stop signal sent", file=sys.stderr, flush=True)
    _cron_thread = None

    # Persist stopped state to DB
    threading.Thread(target=_save_cron_state, args=(False, _cron_interval_seconds), daemon=True).start()


def auto_start_if_needed() -> None:
    """
    Called on server startup.
    If the DB says the cron was running before the server restarted, resume it
    automatically — same behaviour as other inbox bots that survive restarts.
    """
    print("[cindy][cron] Checking DB for persisted cron state...", file=sys.stderr, flush=True)
    try:
        should_run, interval = _load_cron_state()
        if should_run:
            print(f"[cindy][cron] DB says cron was running (interval={interval}s) — auto-resuming", file=sys.stderr, flush=True)
            start_cron(interval_seconds=interval)
        else:
            print("[cindy][cron] DB says cron was stopped — not auto-starting", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[cindy][cron] auto_start_if_needed error: {e}", file=sys.stderr, flush=True)


def trigger_tick() -> None:
    """Manually trigger one cron tick in a background thread."""
    print("[cindy][cron] Manual tick triggered", file=sys.stderr, flush=True)
    t = threading.Thread(target=lambda: _cron_tick_sync(force=True), daemon=True, name="cindy-tick-manual")
    t.start()


async def send_follow_up_direct(conversation_urn: str, message_text: str, sender_name: str) -> bool:
    """
    Send a single follow-up message via Playwright browser.
    Called from the API when Node.js dispatches a follow-up.
    """
    print(f"[cindy][followup-direct] Sending to {sender_name} ({conversation_urn[:40]})", file=sys.stderr, flush=True)

    raw_cookies = _get_cookies_from_db()
    if not raw_cookies:
        add_log("No LinkedIn cookies for follow-up send", "error")
        return False

    csrf_token = extract_jsession_id(raw_cookies)
    if not csrf_token:
        add_log("No JSESSIONID for follow-up send", "error")
        return False

    playwright_cookies = cookie_string_to_playwright(raw_cookies)
    ua = random.choice(_CHROME_UAS)

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    from linkedin_scraper.core.browser import BrowserManager

    try:
        async with BrowserManager(headless=True) as browser:
            await browser.context.add_cookies(playwright_cookies)
            print(f"[cindy][followup-direct] Cookies loaded, warming up...", file=sys.stderr, flush=True)
            try:
                await browser.page.goto(WARMUP_URL, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(random.uniform(1.0, 2.0))
                current_url = browser.page.url
                print(f"[cindy][followup-direct] Warm-up URL: {current_url}", file=sys.stderr, flush=True)
                if any(x in current_url for x in ["/login", "/authwall", "/checkpoint"]):
                    add_log("Session expired during follow-up send", "error")
                    return False
            except Exception as wu_e:
                print(f"[cindy][followup-direct] Warm-up error (proceeding): {wu_e}", file=sys.stderr)

            sent = await _send_linkedin_message(browser.page, csrf_token, ua, conversation_urn, message_text)
            # Always persist refreshed cookies even on standalone follow-up sends
            await _save_cookies_to_db(browser.context)
            if sent:
                add_log(f"Follow-up sent to {sender_name or conversation_urn[:30]}", "success")
            else:
                add_log(f"Follow-up send failed for {sender_name or conversation_urn[:30]}", "error")
            return sent

    except Exception as e:
        add_log(f"Follow-up send error: {str(e)[:100]}", "error")
        print(f"[cindy][followup-direct] Error:\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        return False


def get_status() -> dict:
    """Return current cron status (for GET /cindy/inbox/cron)."""
    thread_alive = _cron_thread is not None and _cron_thread.is_alive()

    # Self-heal: if the thread died unexpectedly but running flag is still True,
    # reset the flag so the frontend shows "Cron is Off" honestly.
    if _state["running"] and not thread_alive and not _state["tick_running"]:
        print("[cindy][cron] WARNING: running=True but thread is dead — resetting to False", file=sys.stderr, flush=True)
        _state["running"] = False

    return {
        "running": _state["running"],
        "sessionSuspended": _state["session_suspended"],
        "lastRun": _state["last_run"],
        "logs": _state["log"],
        "processedCount": len(_state["processed_message_ids"]),
        "tickRunning": _state["tick_running"],
        "threadAlive": thread_alive,
        "intervalSeconds": _cron_interval_seconds,
        "engine": "python-playwright",
    }


def clear_processed_memory() -> None:
    _state["processed_message_ids"] = set()
    _state["processing_convs"] = set()
    print("[cindy] Processed message memory cleared", file=sys.stderr, flush=True)


def clear_logs() -> None:
    _state["log"] = []
    print("[cindy] Logs cleared", file=sys.stderr, flush=True)
