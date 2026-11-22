import { Router, Request, Response } from "express";
import {
  addSSEClient,
  removeSSEClient,
  getCronStatuses,
} from "../lib/sseEmitter";
import { supabase, getBotId } from "../lib/supabase";

const router = Router();


router.get("/", async (req: Request, res: Response) => {
  req.socket.setTimeout(0);
  req.socket.setNoDelay(true);
  req.socket.setKeepAlive(true, 30000);

  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection", "keep-alive");
  res.setHeader("X-Accel-Buffering", "no"); // Disable nginx / Next.js proxy buffering
  res.flushHeaders();
  addSSEClient(res);

  // ── Send initial state on connect ─────────────────────────────────────────
  try {
    const cindyId = await getBotId("cindy");

    const [rawAlerts, cindySession] = await Promise.all([
      supabase.from("session_alerts").select("*").eq("bot_id", cindyId).eq("status", "pending"),
      supabase.from("bot_sessions").select("id").eq("bot_id", cindyId).eq("session_type", "li_session").eq("status", "active").limit(1).maybeSingle(),
    ]);

    // Map UUID → name so frontend gets human-readable botId
    const botIdToName: Record<string, string> = {
      [cindyId]: "cindy",
    };
    const activeBotIds = new Set<string>();
    if (cindySession.data) activeBotIds.add(cindyId);

    const sessionAlerts = (rawAlerts.data ?? [])
      .filter((a: any) => !activeBotIds.has(a.bot_id as string))
      .map((a: any) => ({ botId: botIdToName[a.bot_id as string] ?? a.bot_id, platform: a.platform as string }));

    res.write(
      `event: connected\ndata: ${JSON.stringify({
        sessionAlerts,
        cronStatuses: getCronStatuses(),
      })}\n\n`
    );
  } catch {
    res.write(
      `event: connected\ndata: ${JSON.stringify({
        sessionAlerts: [],
        cronStatuses: getCronStatuses(),
      })}\n\n`
    );
  }

  // ── Keep-alive heartbeat ──────────────────────────────────────────────────
  const heartbeat = setInterval(() => {
    if (res.writableEnded || res.destroyed) {
      clearInterval(heartbeat);
      removeSSEClient(res);
      return;
    }
    try {
      res.write(": heartbeat\n\n");
    } catch {
      clearInterval(heartbeat);
      removeSSEClient(res);
    }
  }, 15_000);

  // ── Cleanup on disconnect ─────────────────────────────────────────────────
  req.on("close", () => {
    clearInterval(heartbeat);
    removeSSEClient(res);
  });
});

export default router;
