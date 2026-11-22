from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import asyncio
import json
import re
import os
import sys
import random
import time
import queue
import threading

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# Force UTF-8 on Windows so emoji in post text don't crash the codec
if sys.stdout.encoding != 'utf-8':
    sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1, closefd=False)
if sys.stderr.encoding != 'utf-8':
    sys.stderr = open(sys.stderr.fileno(), mode='w', encoding='utf-8', buffering=1, closefd=False)

# Make sure the linkedin_scraper package is importable.
# The linkedin_scraper/ folder is in the same directory as this api.py file,
# so SCRAPER_ROOT defaults to the directory containing api.py.
# Override with LINKEDIN_SCRAPER_ROOT env var if the package is elsewhere.
SCRAPER_ROOT = os.environ.get(
    "LINKEDIN_SCRAPER_ROOT",
    os.path.dirname(os.path.abspath(__file__))
)
sys.path.insert(0, SCRAPER_ROOT)

from linkedin_scraper.core.browser import BrowserManager
from linkedin_scraper.scrapers.person_posts import PersonPostsScraper
from linkedin_scraper.scrapers.person import PersonScraper


app = FastAPI(title="LinkedIn Automation Suite — Scraping & Cindy Engine")

# ── Import Cindy inbox module ─────────────────────────────────────────────────
try:
    import cindy_inbox as _cindy
    print("[api] cindy_inbox module loaded OK", file=sys.stderr, flush=True)
except Exception as _cindy_err:
    _cindy = None  # type: ignore
    print(f"[api] WARNING: cindy_inbox failed to load: {_cindy_err}", file=sys.stderr, flush=True)


@app.on_event("startup")
async def _on_startup():
    """Auto-resume Cindy cron if it was running before the server restarted."""
    if _cindy is not None:
        print("[api] startup: checking if Cindy cron should auto-resume...", file=sys.stderr, flush=True)
        # Run in a thread so we don't block the ASGI startup with DB I/O
        import threading
        threading.Thread(target=_cindy.auto_start_if_needed, daemon=True, name="cindy-autostart").start()


@app.get("/")
async def root():
    return {"message": "LinkedIn Scraper API is running", "status": "online"}

# ── CORS: Allow frontend (and any origin) to reach this API ──
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ScrapeRequest(BaseModel):
    cookieString: str
    profileUrl: str = ""
    postUrls: list[str] = []
    limit: int = 10


def cookie_string_to_list(raw: str) -> list:
    """
    Convert a raw Cookie header string into a list of Playwright cookie dicts.
    e.g. "li_at=ABC; JSESSIONID=\"ajax:123\"; bcookie=..."
    """
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
    return result

def _jitter(lo: float = 1.5, hi: float = 4.5) -> float:
    return random.uniform(lo, hi)

async def _human_pause(lo: float = 1.5, hi: float = 4.5) -> None:
    await asyncio.sleep(_jitter(lo, hi))

def prettify_vanity(vanity: str) -> str:
    """Transform m-ammar-sharif-123 into Ammar Sharif."""
    if not vanity: return "Unknown"
    name = re.sub(r'^(m|in)-', '', vanity)
    name = re.sub(r'-[0-9a-zA-Z]{5,}$', '', name)
    name = re.sub(r'-[0-9]+$', '', name)
    name = name.replace('-', ' ').replace('.', ' ')
    return name.title().strip() or vanity.title()

def _do_scrape(cookie_string: str, profile_url: str, limit: int, progress_queue=None, post_urls: list = None) -> dict:
    import sys
    import asyncio

    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    def emit(stage: str, detail: str, pct: int):
        """Send a progress event to the queue if available."""
        if progress_queue:
            progress_queue.put({"stage": stage, "detail": detail, "pct": pct, "ts": time.time()})
        print(f"[api][progress] {stage}: {detail} ({pct}%)", file=sys.stderr)

    async def async_scrape():
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(profile_url)
        p_url = profile_url
        if not parsed.path.endswith('/'):
            p_url = urlunparse(parsed._replace(path=parsed.path + '/'))

        vanity = re.search(r'linkedin\.com/in/([^/?#]+)', p_url)
        vanity_name = vanity.group(1).strip("/") if vanity else ""

        result = {
            "profile": {
                "name": prettify_vanity(vanity_name) if vanity_name else "Unknown",
                "headline": "",
                "location": "",
                "profileUrl": p_url,
                "vanityName": vanity_name,
            },
            "posts": [],
        }

        cookies = cookie_string_to_list(cookie_string)
        emit("init", f"Parsed {len(cookies)} cookies", 2)
        print(f"[api] Scraping {p_url}, limit={limit}", file=sys.stderr)

        try:
            emit("browser", "Launching browser", 5)
            # headless=False is required for LinkedIn profile pages.
            # LinkedIn's new React-based profile frontend detects headless Chromium
            # and serves a stripped page (~141KB) without Experience/Education/Skills.
            # Non-headless browsers get the full profile (~700KB+).
            async with BrowserManager(headless=True) as browser:
                await browser.context.add_cookies(cookies)
                emit("browser", "Browser ready, cookies loaded", 8)

                # ── Session warm-up ──────────────────────────────────────────────
                # Navigate to the LinkedIn homepage. A valid logged-in session will
                # be redirected automatically to /feed/. If we stay on / or land on
                # a login/authwall page, the cookies are expired.
                emit("browser", "Warming up session...", 9)
                _auth_confirmed = False
                _warmup_urls = [
                    "https://www.linkedin.com/",
                    "https://www.linkedin.com/feed/",
                ]
                for _warmup_url in _warmup_urls:
                    try:
                        print(f"[api] Warm-up: trying {_warmup_url}", file=sys.stderr)
                        await browser.page.goto(
                            _warmup_url,
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        await asyncio.sleep(random.uniform(1.5, 2.5))
                        current_url = browser.page.url
                        print(f"[api] Warm-up landed on: {current_url}", file=sys.stderr)

                        # Hard-fail if LinkedIn explicitly redirected to auth pages
                        if any(x in current_url for x in ["/login", "/authwall", "/checkpoint", "/uas/"]):
                            raise Exception(
                                f"Authentication failed: cookies are invalid or expired — "
                                f"LinkedIn redirected to {current_url}. "
                                f"Please re-copy fresh cookies."
                            )

                        # A valid session redirects / → /feed/. Landing on plain / means
                        # LinkedIn accepted the cookies but didn't create a logged-in session.
                        # Treat as degraded — try /feed/ directly before giving up.
                        if current_url.rstrip("/") == "https://www.linkedin.com" and _warmup_url == "https://www.linkedin.com/":
                            print("[api] Warm-up stayed on homepage (not redirected to /feed/) — session may be degraded, trying /feed/ directly", file=sys.stderr)
                            continue

                        # Human warm-up: scroll and read to build natural interaction events
                        await browser.human_mouse_drift(steps=random.randint(3, 5))
                        await browser.human_scroll(distance=random.randint(300, 600))
                        await browser.human_read_pause(min_sec=2.0, max_sec=4.5)
                        await browser.human_scroll(distance=random.randint(150, 350), direction="up")
                        await browser.human_read_pause(min_sec=1.0, max_sec=2.5)
                        await browser.human_mouse_drift(steps=random.randint(2, 3))

                        _auth_confirmed = True
                        emit("browser", f"Session verified (landed: {current_url.split('linkedin.com')[-1][:30]})", 9)
                        break

                    except Exception as _wu_e:
                        _wu_str = str(_wu_e).lower()
                        print(f"[api] Warm-up error on {_warmup_url}: {_wu_e}", file=sys.stderr)

                        # Explicit auth redirect → definitely expired cookies
                        if "Authentication failed" in str(_wu_e):
                            raise

                        # ERR_TOO_MANY_REDIRECTS → try next URL
                        if "err_too_many_redirects" in _wu_str or "redirect" in _wu_str:
                            print(f"[api] Redirect loop on {_warmup_url} — trying next warm-up URL", file=sys.stderr)
                            continue

                        import traceback
                        traceback.print_exc(file=sys.stderr)
                        continue

                if not _auth_confirmed:
                    print("[api] Warm-up inconclusive — proceeding; profile navigation will confirm auth", file=sys.stderr)
                    emit("browser", "Session warm-up inconclusive, proceeding...", 9)

                # Inter-scrape pause — gives LinkedIn's servers time before we hit profile
                await _human_pause(2.0, 4.0)

                # ── Custom Posts Navigation ────────────────────────────────────
                if post_urls and len(post_urls) > 0:
                    emit("posts", f"Scraping {len(post_urls)} specific post(s)", 20)
                    result["profile"]["name"] = "Custom Posts Analysis"
                    result["profile"]["profileUrl"] = "Multiple URLs"
                    
                    try:
                        posts_scraper = PersonPostsScraper(browser.page)
                        for idx, purl in enumerate(post_urls):
                            emit("posts", f"Scraping post {idx+1}/{len(post_urls)}", 20 + int((idx/len(post_urls))*70))
                            try:
                                await browser.page.goto(purl, wait_until="domcontentloaded", timeout=60000)
                                await _human_pause(1.5, 3.0)
                                await posts_scraper._click_all_see_more_buttons()
                                
                                # Use extraction that checks the current page for full posts
                                page_posts = await posts_scraper._extract_posts_from_page()
                                if page_posts and len(page_posts) > 0:
                                    p = page_posts[0]
                                    result["posts"].append({
                                        "urn": p.urn or "",
                                        "text": p.text or "",
                                        "postedDate": p.posted_date or "",
                                        "reactionsCount": p.reactions_count or 0,
                                        "commentsCount": p.comments_count or 0,
                                        "repostsCount": p.reposts_count or 0,
                                        "postUrl": p.linkedin_url or purl,
                                        "imageUrls": list(p.image_urls or []),
                                        "videoUrl": p.video_url,
                                        "articleUrl": p.article_url,
                                    })
                                else:
                                    print(f"[api] No text found for {purl}", file=sys.stderr)
                            except Exception as pe:
                                print(f"[api] Individual post error: {pe}", file=sys.stderr)
                                
                    except Exception as e:
                        print(f"[api] Posts extraction error: {e}", file=sys.stderr)
                        emit("posts", "Posts extraction had errors", 90)

                # ── Profile & General Posts ────────────────────────────────────
                else:
                    # ── Step 1: Get the profile ────────────────────────────────────
                    emit("profile", "Scraping profile info", 10)
                    try:
                        from linkedin_scraper.callbacks import ProgressCallback

                        class _ProfileCallback(ProgressCallback):
                            """Relay PersonScraper's internal steps into the SSE 10–25% band."""
                            async def on_progress(self, message: str, percent: int) -> None:
                                # PersonScraper reports pct roughly in the 10–35 range.
                                # Normalise to 0–1 then map linearly into 11–24%.
                                normalised = max(0.0, min(1.0, (percent - 10) / 25))
                                sse_pct = 11 + int(normalised * 13)
                                emit("profile", message, sse_pct)

                        person_scraper = PersonScraper(browser.page, callback=_ProfileCallback())
                        person = await person_scraper.scrape(p_url)

                        result["profile"].update(person.model_dump())
                        result["profile"]["headline"] = person.headline or person.job_title or ""
                        result["profile"]["name"] = person.name or prettify_vanity(vanity_name)
                        result["profile"]["location"] = person.location or ""
                        result["profile"]["about"] = person.about or ""
                        result["profile"]["open_to_work"] = person.open_to_work
                        result["profile"]["profile_picture_url"] = person.profile_picture_url or ""
                        result["profile"]["skills"] = [s for s in (person.skills or [])]
                        result["profile"]["experiences"] = [e.model_dump() for e in (person.experiences or [])]
                        result["profile"]["educations"] = [e.model_dump() for e in (person.educations or [])]
                        result["profile"]["accomplishments"] = [a.model_dump() for a in (person.accomplishments or [])]
                        result["profile"]["interests"] = [i.model_dump() for i in (person.interests or [])]
                        result["profile"]["contacts"] = [c.model_dump() for c in (person.contacts or [])]
                        emit("profile", f"Profile scraped: {result['profile']['name']}", 25)
                    except Exception as e:
                        print(f"[api] Profile scrape error: {e}", file=sys.stderr)
                        e_str = str(e).lower()
                        if (
                            "Not logged in" in str(e)
                            or "authenticate" in e_str
                            or "redirect" in e_str
                            or "err_too_many_redirects" in e_str
                            or "cookies" in e_str
                            or "expired" in e_str
                        ):
                            raise e
                        import traceback
                        traceback.print_exc(file=sys.stderr)
                        emit("profile", "Profile extraction had errors", 25)

                    # ── Step 2: Scrape posts ─────────────────────────────────────────────
                    emit("posts", "Starting posts scrape", 30)
                    try:
                        posts_scraper = PersonPostsScraper(browser.page)

                        async def posts_progress(stage, detail, pct):
                            mapped_pct = 30 + int(pct * 0.3)
                            emit(stage, detail, mapped_pct)

                        posts = await posts_scraper.scrape(p_url, limit=limit, activity_type="all", on_progress=posts_progress)
                        emit("posts", f"Found {len(posts)} posts", 60)

                        result["posts"] = [
                            {
                                "urn": p.urn or "",
                                "text": p.text or "",
                                "postedDate": p.posted_date or "",
                                "reactionsCount": p.reactions_count or 0,
                                "commentsCount": p.comments_count or 0,
                                "repostsCount": p.reposts_count or 0,
                                "postUrl": p.linkedin_url or "",
                                "imageUrls": list(p.image_urls or []),
                                "videoUrl": p.video_url,
                                "articleUrl": p.article_url,
                            }
                            for p in posts
                        ]
                    except Exception as e:
                        print(f"[api] Posts scrape error: {e}", file=sys.stderr)
                        emit("posts", "Posts extraction had errors", 60)

                    # ── Step 3: Scrape comments ─────────────────────────────────────────────
                    emit("comments", "Starting comments scrape", 65)
                    try:
                        comment_limit = max(5, limit // 2)

                        async def comments_progress(stage, detail, pct):
                            mapped_pct = 65 + int(pct * 0.3)
                            emit(stage, detail, mapped_pct)

                        comments = await posts_scraper.scrape(p_url, limit=comment_limit, activity_type="comments", on_progress=comments_progress)
                        emit("comments", f"Found {len(comments)} comments", 95)

                        result["comments"] = [
                            {
                                "urn": p.urn or "",
                                "text": p.text or "",
                                "postedDate": p.posted_date or "",
                                "postUrl": p.linkedin_url or "",
                            }
                            for p in comments
                        ]
                    except Exception as e:
                        print(f"[api] Comments scrape error: {e}", file=sys.stderr)
                        emit("comments", "Comments extraction had errors", 95)


        except Exception as e:
            import traceback
            error_trace = traceback.format_exc()
            print(f"Top level error in do_scrape: {error_trace}", file=sys.stderr)
            return {"error": str(e), "traceback": error_trace}

        emit("done", f"Complete — {len(result['posts'])} posts, {len(result.get('comments', []))} comments", 100)
        return result

    return asyncio.run(async_scrape())


from concurrent.futures import ThreadPoolExecutor
executor = ThreadPoolExecutor(max_workers=5)

@app.post("/scrape")
async def run_scrape(request: ScrapeRequest):
    if not request.cookieString:
        raise HTTPException(status_code=400, detail="No cookieString provided")
    if not request.profileUrl:
        raise HTTPException(status_code=400, detail="No profileUrl provided")

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            executor,
            _do_scrape,
            request.cookieString,
            request.profileUrl or "",
            request.limit,
            None,
            request.postUrls
        )
        return result
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Server executor error: {error_trace}", file=sys.stderr)
        return {"error": str(e), "traceback": error_trace}


# ── SSE streaming endpoint ──────────────────────────────────────────────────
@app.post("/scrape-stream")
async def run_scrape_stream(request: ScrapeRequest):
    """Stream progress events as SSE (Server-Sent Events) with the final result."""
    if not request.cookieString:
        raise HTTPException(status_code=400, detail="No cookieString provided")
    if not request.profileUrl:
        raise HTTPException(status_code=400, detail="No profileUrl provided")

    progress_q = queue.Queue()

    def run_in_thread():
        return _do_scrape(request.cookieString, request.profileUrl or "", request.limit, progress_q, request.postUrls)

    future = executor.submit(run_in_thread)

    async def event_generator():
        """Yield SSE events as they come from the progress queue."""
        last_heartbeat = time.time()
        
        while not future.done():
            # Drain queue
            sent_something = False
            while True:
                try:
                    evt = progress_q.get_nowait()
                    yield f"data: {json.dumps(evt)}\n\n"
                    sent_something = True
                    last_heartbeat = time.time()
                except queue.Empty:
                    break
            
            # Send heartbeat every 15s to keep proxy/nextjs happy
            if not sent_something and (time.time() - last_heartbeat > 15):
                yield f"data: {json.dumps({'stage': 'heartbeat', 'pct': 0})}\n\n"
                last_heartbeat = time.time()
                
            await asyncio.sleep(0.5)  # poll every 500ms

        # Drain remaining events
        while True:
            try:
                evt = progress_q.get_nowait()
                yield f"data: {json.dumps(evt)}\n\n"
            except queue.Empty:
                break

        # Send final result
        try:
            result = future.result()
            # Mark result specifically
            yield f"data: {json.dumps({'stage': 'result', 'data': result, 'pct': 100})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'stage': 'error', 'detail': str(e), 'pct': 100})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Cindy Inbox Bot — Python Playwright cron endpoints ───────────────────────

class CindyCronRequest(BaseModel):
    action: str = ""
    conversationUrn: str = ""
    messageText: str = ""
    senderName: str = ""
    intervalSeconds: int = 60


def _cindy_unavailable():
    raise HTTPException(status_code=503, detail="cindy_inbox module not loaded — check server logs")


@app.get("/cindy/inbox/cron")
async def cindy_cron_status():
    """Return Cindy cron status, logs, and processed-message count."""
    if _cindy is None:
        _cindy_unavailable()
    print("[api][cindy] GET /cindy/inbox/cron — status requested", file=sys.stderr, flush=True)
    return _cindy.get_status()


@app.post("/cindy/inbox/cron")
async def cindy_cron_action(body: CindyCronRequest):
    """
    Control the Cindy inbox cron.

    actions:
      start           — start the background cron
      stop            — stop it
      send_followup   — send a follow-up message via Playwright
      reset-processed / clear-memory — clear in-memory processed IDs
      clear-logs      — clear the in-memory log buffer
      (anything else) — trigger one manual tick
    """
    if _cindy is None:
        _cindy_unavailable()

    action = body.action
    print(f"[api][cindy] POST /cindy/inbox/cron action={action!r}", file=sys.stderr, flush=True)

    if action == "start":
        # Resolve interval: bot_settings.cron_intervals.cindy > request body > default 60s
        interval = body.intervalSeconds
        try:
            sb = _cindy._get_supabase()
            bot_id = _cindy._get_cindy_bot_id()
            result = sb.from_("bot_settings").select("data").eq("bot_id", bot_id).maybe_single().execute()
            cron_intervals = ((result.data or {}).get("data") or {}).get("cron_intervals") or {}
            if cron_intervals.get("cindy"):
                interval = int(cron_intervals["cindy"])
                print(f"[api][cindy] Using bot_settings interval: {interval}s", file=sys.stderr, flush=True)
            else:
                # Fall back to last persisted interval if available
                _, persisted_interval = _cindy._load_cron_state()
                if persisted_interval and persisted_interval != 60:
                    interval = persisted_interval
                    print(f"[api][cindy] Using persisted interval: {interval}s", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"[api][cindy] Could not read DB interval: {e}", file=sys.stderr)

        _cindy.start_cron(interval_seconds=interval)
        return {"success": True, "running": True, "message": f"Cindy inbox cron started (interval={interval}s, engine=python-playwright)"}

    if action == "stop":
        _cindy.stop_cron()
        return {"success": True, "running": False, "message": "Cindy inbox cron stopped"}

    if action == "send_followup":
        if not body.conversationUrn or not body.messageText:
            raise HTTPException(status_code=400, detail="conversationUrn and messageText are required")
        print(f"[api][cindy] send_followup → {body.senderName} ({body.conversationUrn[:40]})", file=sys.stderr, flush=True)
        sent = await _cindy.send_follow_up_direct(body.conversationUrn, body.messageText, body.senderName)
        return {"sent": sent}

    if action in ("reset-processed", "clear-memory"):
        _cindy.clear_processed_memory()
        return {"success": True, "message": "Processed message memory cleared"}

    if action in ("clear-logs", "clear_logs"):
        _cindy.clear_logs()
        return {"success": True, "message": "Logs cleared"}

    # Default: trigger one manual tick
    _cindy.trigger_tick()
    return {"success": True, "message": "Manual cron tick triggered"}
