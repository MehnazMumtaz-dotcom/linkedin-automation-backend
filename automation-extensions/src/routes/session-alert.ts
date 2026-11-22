import { Router, Request, Response } from "express";
import { supabase, getBotId } from "../lib/supabase";

const router = Router();

// LinkedIn Automation Suite — the single LinkedIn session is owned by Cindy.
const BOT_SESSION_TYPES: Record<string, string> = {
  cindy:  "li_session",
};

// GET /api/session-alert — returns pending session alerts
router.get("/", async (_req: Request, res: Response) => {
  try {
    // Fetch pending alerts with bot name
    const suiteBotIds = (
      await Promise.all(Object.keys(BOT_SESSION_TYPES).map((n) => getBotId(n).catch(() => null)))
    ).filter(Boolean) as string[];

    const { data: pendingRaw } = await supabase
      .from("session_alerts")
      .select("*, bots(name)")
      .in("bot_id", suiteBotIds)
      .eq("status", "pending")
      .order("created_at", { ascending: false });

    // Check which bots currently have an active session
    const activeBots = new Set<string>();
    await Promise.all(
      Object.entries(BOT_SESSION_TYPES).map(async ([botName, sessionType]) => {
        const botId = await getBotId(botName).catch(() => null);
        if (!botId) return;
        const { data } = await supabase
          .from("bot_sessions")
          .select("id")
          .eq("bot_id", botId)
          .eq("session_type", sessionType)
          .eq("status", "active")
          .limit(1)
          .maybeSingle();
        if (data) activeBots.add(botName);
      })
    );

    const alerts = [];
    for (const sa of pendingRaw ?? []) {
      const botName: string = (sa as any).bots?.name ?? "";
      if (activeBots.has(botName)) {
        // Auto-resolve — session is active again
        await supabase
          .from("session_alerts")
          .update({ status: "resolved", resolved_at: new Date().toISOString() })
          .eq("id", sa.id);
      } else {
        alerts.push({
          ...sa,
          botId: botName,
        });
      }
    }

    return res.json({ success: true, alerts });
  } catch (err) {
    return res.status(500).json({ error: err instanceof Error ? err.message : "Unknown error" });
  }
});

export default router;
