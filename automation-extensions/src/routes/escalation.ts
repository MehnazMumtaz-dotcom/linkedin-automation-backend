import { Router, Request, Response } from "express";
import { supabase, getBotId, toCamel } from "../lib/supabase";
import { broadcastSSE } from "../lib/sseEmitter";
import { DEFAULT_BOT, isSuiteBot } from "../lib/suite";

const router = Router();

// GET /api/escalation
router.get("/", async (req: Request, res: Response) => {
  try {
    // Scope to the LinkedIn suite — unknown / missing bot falls back to Cindy
    const requested = req.query.botId as string | undefined;
    const botName = isSuiteBot(requested) ? requested : DEFAULT_BOT;
    const status  = req.query.status as string | undefined;

    let q = supabase
      .from("escalations")
      .select("*, bots(name)")
      .order("created_at", { ascending: false })
      .limit(200);

    if (botName) {
      const botId = await getBotId(botName);
      q = (q as any).eq("bot_id", botId);
    }
    if (status === "pending") {
      q = (q as any).in("status", ["pending", "reminded"]);
    } else if (status) {
      q = (q as any).eq("status", status);
    }

    const { data: escalations, error } = await q;
    if (error) throw error;

    // Counts via separate queries (parallel)
    let countQ = supabase.from("escalations").select("status");
    if (botName) {
      const botId = await getBotId(botName);
      countQ = (countQ as any).eq("bot_id", botId);
    }
    const { data: allRows } = await countQ;
    const allCount       = allRows?.length ?? 0;
    const actionNeeded   = allRows?.filter((r: any) => ["pending", "reminded"].includes(r.status)).length ?? 0;
    const resolvedCount  = allRows?.filter((r: any) => r.status === "resolved").length ?? 0;

    // Normalize: embed botId as name for frontend compat
    const normalized = (escalations ?? []).map((e: any) => ({
      ...toCamel(e),
      id: e.id,
      botId: e.bots?.name ?? e.bot_id,
    }));

    return res.json({
      success: true,
      escalations: normalized,
      counts: { all: allCount, pending: actionNeeded, resolved: resolvedCount },
    });
  } catch (err) {
    return res.status(500).json({ error: err instanceof Error ? err.message : "Unknown error" });
  }
});

// POST /api/escalation
router.post("/", async (req: Request, res: Response) => {
  try {
    const { botId: botName, platform, conversationId, senderName, senderUsername, lastMessage, reason } = req.body;

    if (!botName || !conversationId || !senderName || !lastMessage || !reason) {
      return res.status(400).json({ error: "botId, conversationId, senderName, lastMessage, reason are required." });
    }
    if (!isSuiteBot(botName)) {
      return res.status(400).json({ error: `Unsupported bot: ${botName}` });
    }

    const botId = await getBotId(botName);

    // Resolve conversation UUID from external ID
    const { data: conv } = await supabase
      .from("conversations")
      .select("id")
      .eq("bot_id", botId)
      .eq("external_conversation_id", conversationId)
      .maybeSingle();
    const convId = conv?.id ?? null;

    // Check duplicate
    if (convId) {
      const { data: existing } = await supabase
        .from("escalations")
        .select("id")
        .eq("conversation_id", convId)
        .eq("status", "pending")
        .maybeSingle();
      if (existing) return res.json({ success: true, id: existing.id, duplicate: true });
    }

    const { data: created, error } = await supabase
      .from("escalations")
      .insert({
        bot_id: botId,
        conversation_id: convId,
        platform: platform || botName,
        sender_name: senderName,
        sender_username: senderUsername ?? null,
        last_message: lastMessage,
        reason,
        status: "pending",
      })
      .select("id")
      .single();

    if (error) throw error;
    // Notify all SSE clients so frontend shows the badge/alert immediately
    broadcastSSE("new-escalation", {
      botId: botName,
      platform: platform || botName,
      senderName,
      reason,
      id: created?.id,
    });
    return res.json({ success: true, id: created?.id });
  } catch (err) {
    return res.status(500).json({ error: err instanceof Error ? err.message : "Unknown error" });
  }
});

// PATCH /api/escalation
router.patch("/", async (req: Request, res: Response) => {
  try {
    const { id, status } = req.body;
    if (!id || !status) return res.status(400).json({ error: "id and status are required." });

    const updates: Record<string, any> = { status };
    if (status === "resolved") updates.resolved_at = new Date().toISOString();
    if (status === "reminded") updates.reminder_sent_at = new Date().toISOString();

    await supabase.from("escalations").update(updates).eq("id", id);
    return res.json({ success: true });
  } catch (err) {
    return res.status(500).json({ error: err instanceof Error ? err.message : "Unknown error" });
  }
});

// GET /api/escalation/reminder
router.get("/reminder", async (_req: Request, res: Response) => {
  try {
    const cindyId = await getBotId(DEFAULT_BOT);
    const { data: escalations } = await supabase
      .from("escalations")
      .select("id, bot_id, platform, sender_name, created_at, reason, bots(name)")
      .eq("bot_id", cindyId)
      .in("status", ["pending", "reminded"])
      .order("created_at", { ascending: false })
      .limit(100);

    return res.json({
      success: true,
      escalations: (escalations ?? []).map((e: any) => ({
        id: e.id,
        botId: e.bots?.name ?? e.bot_id,
        platform: e.platform,
        senderName: e.sender_name,
        createdAt: e.created_at,
        reason: e.reason,
      })),
    });
  } catch (err) {
    return res.status(500).json({ error: err instanceof Error ? err.message : "Unknown error" });
  }
});

// DELETE /api/escalation
router.delete("/", async (req: Request, res: Response) => {
  try {
    const id       = req.query.id as string;
    const botName  = req.query.botId as string;
    const deleteAllResolved = req.query.deleteAllResolved === "true";

    if (deleteAllResolved && botName) {
      const botId = await getBotId(botName);
      const { data: deleted } = await supabase
        .from("escalations")
        .delete()
        .eq("bot_id", botId)
        .eq("status", "resolved")
        .select("id");
      return res.json({ success: true, count: deleted?.length ?? 0 });
    }

    if (!id) return res.status(400).json({ error: "id is required." });
    await supabase.from("escalations").delete().eq("id", id);
    return res.json({ success: true });
  } catch (err) {
    return res.status(500).json({ error: err instanceof Error ? err.message : "Unknown error" });
  }
});

export default router;
