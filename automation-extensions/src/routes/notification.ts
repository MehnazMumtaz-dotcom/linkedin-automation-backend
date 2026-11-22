import { Router, Request, Response } from "express";
import { supabase, getBotId } from "../lib/supabase";
import { broadcastSSE, broadcastCronStatus } from "../lib/sseEmitter";

const router = Router();

// GET /api/notification/status — lean status endpoint for EscalationNotifier
router.get("/status", async (_req: Request, res: Response) => {
  try {
    const cindyId = await getBotId("cindy");

    const [
      { data: pendingEscalations },
      { data: saResultRaw },
      cindySession,
    ] = await Promise.all([
      supabase.from("escalations").select("id, created_at").eq("bot_id", cindyId).in("status", ["pending", "reminded"]),
      supabase.from("session_alerts").select("*").eq("bot_id", cindyId).eq("status", "pending"),
      supabase.from("bot_sessions").select("id").eq("bot_id", cindyId).eq("session_type", "li_session").eq("status", "active").limit(1).maybeSingle(),
    ]);

    // Build set of bot UUIDs that have an active session
    const activeBotIds = new Set<string>();
    if (cindySession.data) activeBotIds.add(cindyId);

    let pendingSessionAlerts = 0;
    let saLastAt: string | null = null;

    for (const sa of (saResultRaw ?? [])) {
      if (activeBotIds.has(sa.bot_id as string)) {
        // Bot now has an active session — auto-resolve the stale alert
        await supabase.from("session_alerts").update({ status: "resolved", resolved_at: new Date().toISOString() }).eq("id", sa.id);
      } else {
        pendingSessionAlerts++;
        if (!saLastAt || sa.created_at > saLastAt) {
          saLastAt = sa.created_at;
        }
      }
    }

    const pendingEscalationsCount: number = pendingEscalations?.length ?? 0;
    const escLastAt: string | null = pendingEscalations?.length
      ? pendingEscalations.reduce((max: string | null, e: any) => (!max || e.created_at > max ? e.created_at : max), null)
      : null;

    const lastChangedAt =
      escLastAt && saLastAt
        ? escLastAt > saLastAt ? escLastAt : saLastAt
        : escLastAt ?? saLastAt ?? null;

    return res.json({ pendingEscalations: pendingEscalationsCount, pendingSessionAlerts, lastChangedAt });
  } catch (err) {
    const msg = err instanceof Error ? err.message : "Unknown error";
    return res.status(500).json({ error: msg });
  }
});

// POST /api/notification/internal — called by Python backend to push SSE events to frontend
// Python writes to Supabase but cannot reach the Node.js SSE socket directly; this bridge closes that gap.
router.post("/internal", (req: Request, res: Response) => {
  const { type, botName, platform, data } = req.body as {
    type: string;
    botName?: string;
    platform?: string;
    data?: Record<string, unknown>;
  };

  switch (type) {
    case "escalation":
      broadcastSSE("new-escalation", { botId: botName, platform, ...(data ?? {}) });
      break;
    case "session-expired":
      broadcastSSE("session-expired", { botId: botName, platform });
      broadcastCronStatus();
      break;
    case "session-restored":
      broadcastSSE("session-restored", { botId: botName });
      broadcastCronStatus();
      break;
    case "followup-sent":
      broadcastSSE("followup-sent", { botId: botName, ...(data ?? {}) });
      break;
    default:
      return res.status(400).json({ error: `Unknown notify type: ${type}` });
  }

  return res.json({ success: true });
});

export default router;
