import { Router, Request, Response } from "express";
import { supabase, getCronIntervals, saveCronIntervals, saveLinkedInSession } from "../lib/supabase";
import { requireApiKey } from "../middleware/auth";
import { resolveSessionAlert } from "../lib/sessionAlert";
import { broadcastSSE } from "../lib/sseEmitter";

const router = Router();

// Maps the legacy collection name (used by Chrome extension) → bot name + session_type
// LinkedIn Automation Suite — only the LinkedIn session (owned by Cindy) is supported.
const COLLECTION_TO_BOT: Record<string, { botName: string; sessionType: string }> = {
  cindy_config:  { botName: "cindy",  sessionType: "li_session" },
};

const VALID_COLLECTIONS = new Set(Object.keys(COLLECTION_TO_BOT));

// GET /api/bots — list all bots and their session status
router.get("/", async (_req: Request, res: Response): Promise<void> => {
  try {
    const { data: sessions } = await supabase
      .from("bot_sessions")
      .select("bot_id, status, session_type, saved_at, extra_data, bots(name)")
      .eq("status", "active");

    const results: Record<string, any> = {
      cindy:  { status: "inactive" },
    };

    for (const session of sessions ?? []) {
      const botName: string = (session as any).bots?.name;
      if (!botName || !(botName in results)) continue;
      results[botName] = {
        status: "active",
        name: (session as any).extra_data?.name ?? botName,
        savedAt: session.saved_at,
        type: session.session_type,
      };
    }

    res.json({ success: true, bots: results });
  } catch (err) {
    console.error("[Bots] GET failed:", err);
    res.status(500).json({ error: "Failed to fetch bot statuses" });
  }
});

// PATCH /api/bots/:collection/update-session
// Called by the Chrome bot-auth-manager extension — collection name kept for backward compat
router.patch(
  "/:collection/update-session",
  requireApiKey,
  async (req: Request, res: Response): Promise<void> => {
    const { collection } = req.params;

    if (!VALID_COLLECTIONS.has(collection)) {
      res.status(404).json({ error: `Unknown collection: ${collection}` });
      return;
    }

    const { cookies, fetchedAt, status, passcode } = req.body as {
      cookies: Record<string, string | null>;
      fetchedAt?: string;
      status?: string;
      passcode?: string;
    };

    if (!cookies || typeof cookies !== "object") {
      res.status(400).json({ error: "Missing or invalid 'cookies' field in request body" });
      return;
    }

    try {
      const { botName, sessionType } = COLLECTION_TO_BOT[collection];

      // Strip null/empty values
      const cookieFields: Record<string, string> = {};
      for (const [k, v] of Object.entries(cookies)) {
        if (v !== null && v !== undefined && v !== "") cookieFields[k] = v;
      }

      const rawCookies = Object.entries(cookieFields)
        .map(([k, v]) => `${k}=${v}`)
        .join("; ");

      // Validate LinkedIn cookie and extract the account name
      let name = "LinkedIn User";
      const { validateCookie } = await import("../lib/linkedin");
      const vResult = await validateCookie(rawCookies);
      if (vResult.valid) name = vResult.name ?? "LinkedIn User";

      // rawCookies always included so all consumers (Python Cindy cron, auth GET
      // fallback, scraper / Ceevee / Cara routes) read the same session.
      const extra: Record<string, any> = { name, passcode: passcode ?? "1122", rawCookies };

      const savedAt = fetchedAt ? new Date(fetchedAt).toISOString() : new Date().toISOString();

      if (!cookieFields.li_at || !cookieFields.JSESSIONID) {
        res.status(400).json({ error: "LinkedIn cookies li_at and JSESSIONID are required. Make sure you are logged in to linkedin.com." });
        return;
      }

      // Replace the stored LinkedIn session (single row, see saveLinkedInSession)
      const sessionId = await saveLinkedInSession({ rawCookies, extra, savedAt, status: status ?? "active" });

      // Resume bot and resolve any pending session alerts
      await resolveSessionAlert(botName);

      // Clear in-memory suspended flags
      const g = globalThis as any;
      g.cindy_inbox_sessionSuspended = false;

      // Notify frontend so it can auto-refresh auth state without a page reload
      broadcastSSE("session-renewed", { botName, sessionType });

      res.json({ success: true, message: vResult.valid ? `LinkedIn session saved for ${name}` : "LinkedIn session saved (could not verify account name)", id: sessionId });
    } catch (err) {
      const message = err instanceof Error ? err.message : "Unknown database error";
      console.error("[BotAuth] DB update failed:", { message });
      const isSafeSupabaseError = message.startsWith("Supabase ");
      res.status(500).json({ error: isSafeSupabaseError ? message : "Database update failed" });
    }
  }
);

// GET /api/bots/cron-intervals — Fetch current cron intervals
router.get("/cron-intervals", async (_req: Request, res: Response): Promise<void> => {
  try {
    const stored = await getCronIntervals();

    const intervals = {
      cindy: stored?.cindy ?? 240,
    };

    res.json({ success: true, intervals });
  } catch (err) {
    console.error("[Bots] GET intervals failed:", err);
    res.status(500).json({ error: "Failed to fetch cron intervals" });
  }
});

// PATCH /api/bots/cron-intervals — Update cron intervals
router.patch("/cron-intervals", async (req: Request, res: Response): Promise<void> => {
  try {
    const cindy = Number(req.body?.cindy);
    if (!Number.isFinite(cindy) || cindy < 60) {
      res.status(400).json({ error: "cindy interval (seconds, >= 60) is required" });
      return;
    }
    await saveCronIntervals({ cindy });

    // Update global state immediately (Python reads the DB value on next start)
    (globalThis as any)._cindy_interval_cfg = cindy;

    res.json({ success: true, message: "Cron intervals updated" });
  } catch (err) {
    console.error("[Bots] PATCH intervals failed:", err);
    res.status(500).json({ error: "Failed to update cron intervals" });
  }
});

export default router;
