import { Router, Request, Response } from "express";
import { validateCookie, extractLiAt, extractJsessionId } from "../lib/linkedin";
import { resolveSessionAlert } from "../lib/sessionAlert";
import { supabase, getBotId, saveLinkedInSession } from "../lib/supabase";
import { broadcastSSE } from "../lib/sseEmitter";

const router = Router();

const COOKIE_MAX_AGE = 60 * 60 * 24 * 7; // 7 days in seconds

// POST /api/auth — validate and store LinkedIn cookies
router.post("/", async (req: Request, res: Response): Promise<void> => {
  try {
    const body = req.body;
    const rawCookie: string = (body.cookieString ?? body.cookie ?? "").trim();

    if (!rawCookie) {
      res.status(400).json({ error: "Cookie string is required" });
      return;
    }
    if (!rawCookie.includes("li_at=")) {
      res.status(400).json({ error: 'Cookie string must contain "li_at=". Please copy the full Cookie header.' });
      return;
    }
    if (!rawCookie.includes("JSESSIONID=")) {
      res.status(400).json({ error: 'Cookie string must contain "JSESSIONID=". Please copy the full Cookie header (not just li_at).' });
      return;
    }

    const liAt = extractLiAt(rawCookie);
    const jsessionId = extractJsessionId(rawCookie);
    if (!liAt || !jsessionId) {
      res.status(400).json({ error: "Could not parse li_at or JSESSIONID from the cookie string." });
      return;
    }

    const result = await validateCookie(rawCookie);
    if (!result.valid) {
      res.status(401).json({ error: "LinkedIn rejected these cookies. They may be expired. Please copy fresh cookies from your browser." });
      return;
    }

    const name = result.name ?? "LinkedIn User";

    const cookieOpts = {
      secure: process.env.NODE_ENV === "production",
      sameSite: "strict" as const,
      path: "/",
      maxAge: COOKIE_MAX_AGE * 1000, // Express uses milliseconds
    };

    res.cookie("li_session", rawCookie, { ...cookieOpts, httpOnly: true });
    res.cookie("li_name", name, { ...cookieOpts, httpOnly: false });

    // Persist to DB
    try {
      await saveLinkedInSession({ rawCookies: rawCookie, extra: { name } });
    } catch (dbErr) {
      console.error("[auth] Failed to save LinkedIn session to DB:", dbErr);
    }

    await resolveSessionAlert("cindy");

    // Clear suspended flag so cindy inbox cron resumes on next tick
    const _g = globalThis as any;
    _g.cindy_inbox_sessionSuspended = false;

    // Notify all open frontend tabs
    broadcastSSE("session-renewed", { botName: "cindy", sessionType: "li_session" });

    res.json({ success: true, name });
  } catch (err) {
    console.error("Auth POST error:", err);
    res.status(500).json({ error: "Server error during authentication" });
  }
});

// GET /api/auth — check session status
router.get("/", async (req: Request, res: Response): Promise<void> => {
  // Check browser cookie first
  const session = req.cookies?.li_session;
  const name = req.cookies?.li_name;

  if (session) {
    res.json({ authenticated: true, name: name ?? "LinkedIn User" });
    return;
  }

  // Fallback: Check DB — covers both manual auth (extra_data.rawCookies) and
  // extension update (raw_cookies column) paths
  try {
    const cindyId = await getBotId("cindy");
    const { data: doc } = await supabase
      .from("bot_sessions")
      .select("extra_data, status, raw_cookies")
      .eq("bot_id", cindyId)
      .eq("session_type", "li_session")
      .eq("status", "active")
      .order("saved_at", { ascending: false })
      .limit(1)
      .maybeSingle();

    const dbSession = (doc?.extra_data?.rawCookies ?? (doc as any)?.raw_cookies) as string | undefined;
    if (dbSession) {
      const dbName = (doc?.extra_data?.name as string) ?? "LinkedIn User";

      const cookieOpts = {
        secure: process.env.NODE_ENV === "production",
        sameSite: "strict" as const,
        path: "/",
        maxAge: COOKIE_MAX_AGE * 1000,
      };
      res.cookie("li_session", dbSession, { ...cookieOpts, httpOnly: true });
      res.cookie("li_name", dbName, { ...cookieOpts, httpOnly: false });

      res.json({ authenticated: true, name: dbName });
      return;
    }
  } catch (err) {
    console.error("[auth GET] DB session sync failed:", err);
  }

  res.json({ authenticated: false });
});

// DELETE /api/auth — logout
router.delete("/", async (_req: Request, res: Response): Promise<void> => {
  res.clearCookie("li_session");
  res.clearCookie("li_name");

  try {
    const cindyId = await getBotId("cindy");
    await supabase.from("bot_sessions").update({ status: "expired" }).eq("bot_id", cindyId).eq("session_type", "li_session");
  } catch (dbErr) {
    console.error("[auth] Failed to expire LinkedIn session in DB:", dbErr);
  }

  res.json({ success: true });
});

export default router;
