import { Router, Request, Response } from "express";
import { supabase, getBotId, toCamel } from "../lib/supabase";
import { DEFAULT_BOT, isSuiteBot } from "../lib/suite";
import { broadcastSSE } from "../lib/sseEmitter";

const router = Router();

const FOLLOWUP_HOURS = [2 / 60, 5 / 60, 10 / 60, 20 / 60, 30 / 60];

function computeNextScheduledAt(_sentAt: string, followUpsSent: number, customDelay?: number): string | null {
  const followUpNumber = followUpsSent + 1;
  if (followUpNumber > 5) return null;
  const waitHours = customDelay ?? FOLLOWUP_HOURS[followUpsSent] ?? 24;
  return new Date(Date.now() + waitHours * 3_600_000).toISOString();
}

// Helper: convert Supabase snake_case row → camelCase for frontend
function mapRecord(row: any, botName: string): any {
  return {
    id: row.id,
    botName,
    userId: row.user_id,
    userName: row.user_name,
    contactInfo: row.contact_info,
    originalMessageId: row.original_message_id,
    originalMessageText: row.original_message_text,
    originalMessageSentAt: row.original_message_sent_at,
    replyReceived: row.reply_received,
    replyReceivedAt: row.reply_received_at,
    followUpsSent: row.follow_ups_sent,
    lastFollowupSentAt: row.last_followup_sent_at,
    nextFollowupScheduledAt: row.next_followup_scheduled_at,
    status: row.status,
    manuallyStoppedAt: row.manually_stopped_at,
    notes: row.notes,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  };
}

// GET /api/follow-up
router.get("/", async (req: Request, res: Response) => {
  try {
    const requested = req.query.botName as string | undefined;
    const botName = isSuiteBot(requested) ? requested : DEFAULT_BOT;
    const status  = req.query.status  as string | undefined;
    const userId  = req.query.userId  as string | undefined;
    const limit   = parseInt((req.query.limit as string) || "100");

    let q = supabase.from("follow_up_records").select("*, bots(name)").order("created_at", { ascending: false }).limit(limit);

    if (botName) {
      const botId = await getBotId(botName);
      q = (q as any).eq("bot_id", botId);
    }
    if (status) q = (q as any).eq("status", status);
    if (userId) q = (q as any).eq("user_id", userId);

    const { data: rows, error } = await q;
    if (error) throw error;

    const records = (rows ?? []).map((r: any) => mapRecord(r, r.bots?.name ?? botName ?? ""));

    // Counts
    let cq = supabase.from("follow_up_records").select("status, reply_received_at");
    if (botName) {
      const botId = await getBotId(botName);
      cq = (cq as any).eq("bot_id", botId);
    }
    const { data: allRows } = await cq;

    const todayStart = new Date();
    todayStart.setHours(0, 0, 0, 0);

    const allCount       = allRows?.length ?? 0;
    const activeCount    = allRows?.filter((r: any) => r.status === "active").length ?? 0;
    const pausedCount    = allRows?.filter((r: any) => r.status === "paused").length ?? 0;
    const stoppedCount   = allRows?.filter((r: any) => r.status === "stopped").length ?? 0;
    const completedCount = allRows?.filter((r: any) => r.status === "completed").length ?? 0;
    const repliedCount   = allRows?.filter((r: any) => r.status === "replied").length ?? 0;
    const repliedToday   = allRows?.filter((r: any) => r.status === "replied" && r.reply_received_at >= todayStart.toISOString()).length ?? 0;

    return res.json({
      success: true,
      records,
      stats: { active: activeCount, repliedToday, overdue: completedCount, paused: pausedCount },
      counts: { all: allCount, active: activeCount, paused: pausedCount, stopped: stoppedCount, completed: completedCount, replied: repliedCount },
    });
  } catch (err) {
    return res.status(500).json({ error: err instanceof Error ? err.message : "Unknown error" });
  }
});

// POST /api/follow-up
router.post("/", async (req: Request, res: Response) => {
  try {
    const { botName, userId, userName, contactInfo, originalMessageId, originalMessageText } = req.body;

    if (!botName || !userId || !userName || !originalMessageId || !originalMessageText) {
      return res.status(400).json({ error: "botName, userId, userName, originalMessageId, originalMessageText are required." });
    }
    if (!isSuiteBot(botName)) return res.status(400).json({ error: `Unsupported bot: ${botName}` });

    const botId = await getBotId(botName);

    // Check duplicate active/paused
    const { data: existing } = await supabase
      .from("follow_up_records")
      .select("id")
      .eq("bot_id", botId)
      .eq("user_id", userId)
      .in("status", ["active", "paused"])
      .single();
    if (existing) return res.json({ success: true, id: existing.id, duplicate: true });

    const now = new Date().toISOString();
    const sentAt = req.body.originalMessageSentAt || now;

    // Get first template delay if configured
    const { data: firstTemplate } = await supabase
      .from("follow_up_templates")
      .select("delay_in_hours")
      .eq("bot_id", botId)
      .eq("follow_up_number", 1)
      .single();

    const nextWait = firstTemplate?.delay_in_hours ?? FOLLOWUP_HOURS[0];
    const nextScheduled = computeNextScheduledAt(sentAt, 0, nextWait)!;

    const { data: created, error } = await supabase
      .from("follow_up_records")
      .insert({
        bot_id: botId,
        user_id: userId,
        user_name: userName,
        contact_info: contactInfo ?? null,
        original_message_id: originalMessageId,
        original_message_text: originalMessageText,
        original_message_sent_at: sentAt,
        reply_received: false,
        follow_ups_sent: 0,
        next_followup_scheduled_at: nextScheduled,
        status: "active",
      })
      .select("id")
      .single();

    if (error) throw error;
    return res.json({ success: true, id: created?.id });
  } catch (err) {
    return res.status(500).json({ error: err instanceof Error ? err.message : "Unknown error" });
  }
});

// PATCH /api/follow-up
router.patch("/", async (req: Request, res: Response) => {
  try {
    const { id, action, notes } = req.body;
    if (!id || !action) return res.status(400).json({ error: "id and action are required." });

    const { data: row } = await supabase.from("follow_up_records").select("*, bots(name)").eq("id", id).single();
    if (!row) return res.status(404).json({ error: "Record not found." });

    const now = new Date().toISOString();
    const update: Record<string, any> = { updated_at: now };

    switch (action) {
      case "pause":
        update.status = "paused";
        break;
      case "resume":
        update.status = "active";
        if (row.next_followup_scheduled_at < now) {
          update.next_followup_scheduled_at = new Date(Date.now() + 30 * 60 * 1000).toISOString();
        }
        break;
      case "stop":
        update.status = "stopped";
        update.manually_stopped_at = now;
        break;
      case "restart": {
        const botId = row.bot_id;
        const { data: firstTmpl } = await supabase
          .from("follow_up_templates")
          .select("delay_in_hours")
          .eq("bot_id", botId)
          .eq("follow_up_number", 1)
          .single();
        update.status = "active";
        update.follow_ups_sent = 0;
        update.reply_received = false;
        update.reply_received_at = null;
        update.last_followup_sent_at = null;
        update.manually_stopped_at = null;
        update.original_message_sent_at = now;
        update.next_followup_scheduled_at = computeNextScheduledAt(now, 0, firstTmpl?.delay_in_hours)!;
        break;
      }
      case "mark-replied":
        update.status = "replied";
        update.reply_received = true;
        update.reply_received_at = now;
        break;
      case "send-now":
        if (row.follow_ups_sent < 5) {
          update.next_followup_scheduled_at = now;
          update.status = "active";
        }
        break;
      case "add-note":
        update.notes = notes || "";
        break;
      default:
        return res.status(400).json({ error: `Unknown action: ${action}` });
    }

    await supabase.from("follow_up_records").update(update).eq("id", id);
    return res.json({ success: true });
  } catch (err) {
    return res.status(500).json({ error: err instanceof Error ? err.message : "Unknown error" });
  }
});

// DELETE /api/follow-up
router.delete("/", async (req: Request, res: Response) => {
  try {
    const id      = req.query.id as string;
    const botName = req.query.botName as string;
    const deleteAllResolved = req.query.deleteAllResolved === "true";

    if (deleteAllResolved && botName) {
      const botId = await getBotId(botName);
      const { data: deleted } = await supabase
        .from("follow_up_records")
        .delete()
        .eq("bot_id", botId)
        .in("status", ["replied", "completed", "stopped"])
        .select("id");
      return res.json({ success: true, count: deleted?.length ?? 0 });
    }

    if (!id) return res.status(400).json({ error: "id is required." });
    await supabase.from("follow_up_records").delete().eq("id", id);
    return res.json({ success: true });
  } catch (err) {
    return res.status(500).json({ error: err instanceof Error ? err.message : "Unknown error" });
  }
});

// ── Rules (/api/follow-up/rules) ──────────────────────────────────────────────

router.get("/rules", async (req: Request, res: Response) => {
  try {
    const requested = req.query.botName as string | undefined;
    const botName = isSuiteBot(requested) ? requested : DEFAULT_BOT;
    let q = supabase.from("follow_up_rules").select("*, bots(name)").order("created_at", { ascending: false });
    if (botName) {
      const botId = await getBotId(botName);
      q = (q as any).eq("bot_id", botId);
    }
    const { data: rules } = await q;
    const mapped = (rules ?? []).map((r: any) => ({
      id: r.id, botName: r.bots?.name ?? botName, type: r.type, value: r.value,
      enabled: r.enabled, createdAt: r.created_at,
    }));
    return res.json({ success: true, rules: mapped });
  } catch (err) {
    return res.status(500).json({ error: err instanceof Error ? err.message : "Unknown error" });
  }
});

router.post("/rules", async (req: Request, res: Response) => {
  try {
    const { botName, type, value } = req.body;
    if (!botName || !type || !value) return res.status(400).json({ error: "botName, type, and value are required." });
    if (!isSuiteBot(botName)) return res.status(400).json({ error: `Unsupported bot: ${botName}` });

    const botId = await getBotId(botName);

    // Check duplicate (case-insensitive)
    const { data: existing } = await supabase
      .from("follow_up_rules")
      .select("id")
      .eq("bot_id", botId)
      .ilike("value", value)
      .single();
    if (existing) return res.json({ success: true, id: existing.id, duplicate: true });

    const { data: created } = await supabase
      .from("follow_up_rules")
      .insert({ bot_id: botId, type, value, enabled: true })
      .select("id")
      .single();
    return res.json({ success: true, id: created?.id });
  } catch (err) {
    return res.status(500).json({ error: err instanceof Error ? err.message : "Unknown error" });
  }
});

router.patch("/rules", async (req: Request, res: Response) => {
  try {
    const { id, enabled } = req.body;
    if (!id || enabled === undefined) return res.status(400).json({ error: "id and enabled are required." });
    await supabase.from("follow_up_rules").update({ enabled: Boolean(enabled) }).eq("id", id);
    return res.json({ success: true });
  } catch (err) {
    return res.status(500).json({ error: err instanceof Error ? err.message : "Unknown error" });
  }
});

router.delete("/rules", async (req: Request, res: Response) => {
  try {
    const id = req.query.id as string;
    if (!id) return res.status(400).json({ error: "id is required." });
    await supabase.from("follow_up_rules").delete().eq("id", id);
    return res.json({ success: true });
  } catch (err) {
    return res.status(500).json({ error: err instanceof Error ? err.message : "Unknown error" });
  }
});

// ── Templates (/api/follow-up/templates) ──────────────────────────────────────

router.get("/templates", async (req: Request, res: Response) => {
  try {
    const requested = req.query.botName as string | undefined;
    const botName = isSuiteBot(requested) ? requested : DEFAULT_BOT;
    let q = supabase.from("follow_up_templates").select("*, bots(name)").order("follow_up_number", { ascending: true });
    if (botName) {
      const botId = await getBotId(botName);
      q = (q as any).eq("bot_id", botId);
    }
    const { data: templates } = await q;
    const mapped = (templates ?? []).map((t: any) => ({
      id: t.id, botName: t.bots?.name ?? botName,
      followUpNumber: t.follow_up_number,
      messageText: t.message_text,
      delayInHours: t.delay_in_hours,
      updatedAt: t.updated_at,
    }));
    return res.json({ success: true, templates: mapped });
  } catch (err) {
    return res.status(500).json({ error: err instanceof Error ? err.message : "Unknown error" });
  }
});

router.post("/templates", async (req: Request, res: Response) => {
  try {
    const { botName, followUpNumber, messageText, delayInHours } = req.body;
    if (!botName || !followUpNumber || !messageText) {
      return res.status(400).json({ error: "botName, followUpNumber, and messageText are required." });
    }
    if (!isSuiteBot(botName)) return res.status(400).json({ error: `Unsupported bot: ${botName}` });

    const botId = await getBotId(botName);
    await supabase.from("follow_up_templates").upsert(
      {
        bot_id: botId,
        follow_up_number: followUpNumber,
        message_text: messageText,
        delay_in_hours: delayInHours !== undefined ? Number(delayInHours) : null,
        updated_at: new Date().toISOString(),
      },
      { onConflict: "bot_id,follow_up_number" }
    );
    return res.json({ success: true });
  } catch (err) {
    return res.status(500).json({ error: err instanceof Error ? err.message : "Unknown error" });
  }
});

// ── Manual dispatch (/api/follow-up/cron) ─────────────────────────────────────
// Cindy's scheduled follow-ups are processed inside the Python Playwright engine
// on every inbox tick. These actions let a human trigger that work on demand:
//   run-now     → trigger one Cindy tick (inbox check + all due follow-ups)
//   manual-send → send the next follow-up for one record immediately

const MAX_FOLLOWUPS = 5;

const DEFAULT_FOLLOWUP_TEMPLATES: Record<number, string> = {
  1: "Hi {{user_name}}, just a friendly reminder about my previous message. Would love to hear your thoughts!",
  2: "Hi {{user_name}}, following up again — I wanted to make sure my message didn't get lost. Looking forward to connecting!",
  3: "Hi {{user_name}}, reaching out one more time. I sent a message {{days_waiting}} day(s) ago and haven't heard back. Happy to help whenever you're ready!",
  4: "Hi {{user_name}}, I know you're busy — just wanted to check in once more. Feel free to reach out anytime.",
  5: "Hi {{user_name}}, this will be my last follow-up. If you ever want to reconnect, don't hesitate to reach out. Wishing you all the best!",
};

function renderTemplate(text: string, vars: Record<string, string>): string {
  return text.replace(/\{\{(\w+)\}\}/g, (m, k) => (k in vars ? vars[k] : m));
}

function getPythonBase(): string {
  return (process.env.PYTHON_BACKEND_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
}

async function callCindyEngine(body: Record<string, unknown>, timeoutMs: number): Promise<any> {
  const r = await fetch(`${getPythonBase()}/cindy/inbox/cron`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "ngrok-skip-browser-warning": "true" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(timeoutMs),
  });
  return r.json();
}

router.post("/cron", async (req: Request, res: Response) => {
  const { action, botName: requested, recordId, customMessage } = req.body ?? {};
  try {
    if (action === "run-now") {
      const botName = isSuiteBot(requested) ? requested : DEFAULT_BOT;
      const botId = await getBotId(botName);
      const { data: due } = await supabase
        .from("follow_up_records")
        .select("id")
        .eq("bot_id", botId)
        .eq("status", "active")
        .eq("reply_received", false)
        .lte("next_followup_scheduled_at", new Date().toISOString());

      await callCindyEngine({ action: "tick" }, 15000);
      const n = due?.length ?? 0;
      return res.json({
        success: true,
        due: n,
        message: n
          ? `Cindy tick started — ${n} due follow-up(s) are being sent.`
          : "Cindy tick started — no follow-ups are due right now.",
      });
    }

    if (action === "manual-send") {
      if (!recordId) return res.status(400).json({ success: false, error: "recordId is required." });

      const { data: row } = await supabase.from("follow_up_records").select("*").eq("id", recordId).maybeSingle();
      if (!row) return res.status(404).json({ success: false, error: "Record not found." });
      if (row.status === "replied") return res.status(409).json({ success: false, error: "Prospect already replied — follow-ups are stopped." });

      const followUpNumber = (row.follow_ups_sent ?? 0) + 1;
      if (followUpNumber > MAX_FOLLOWUPS) {
        return res.status(409).json({ success: false, error: `All ${MAX_FOLLOWUPS} follow-ups have already been sent.` });
      }

      let template = typeof customMessage === "string" ? customMessage.trim() : "";
      if (!template) {
        const { data: tmpl } = await supabase
          .from("follow_up_templates")
          .select("message_text")
          .eq("bot_id", row.bot_id)
          .eq("follow_up_number", followUpNumber)
          .maybeSingle();
        template = tmpl?.message_text || DEFAULT_FOLLOWUP_TEMPLATES[followUpNumber] || "Hi {{user_name}}, just following up!";
      }
      const sentAt = row.original_message_sent_at ? new Date(row.original_message_sent_at).getTime() : Date.now();
      const messageText = renderTemplate(template, {
        user_name: row.user_name || "there",
        original_message: row.original_message_text || "",
        days_waiting: String(Math.max(0, Math.floor((Date.now() - sentAt) / 86_400_000))),
      });

      // Playwright warm-up + send can take a while
      const result = await callCindyEngine(
        { action: "send_followup", conversationUrn: row.user_id, messageText, senderName: row.user_name || "" },
        90000
      );
      if (!result?.sent) {
        return res.json({ success: false, error: result?.error || "LinkedIn rejected the follow-up (session may be expired)." });
      }

      const now = new Date().toISOString();
      const isLast = followUpNumber >= MAX_FOLLOWUPS;
      const { data: nextTmpl } = isLast
        ? { data: null }
        : await supabase
            .from("follow_up_templates")
            .select("delay_in_hours")
            .eq("bot_id", row.bot_id)
            .eq("follow_up_number", followUpNumber + 1)
            .maybeSingle();

      await supabase.from("follow_up_records").update({
        follow_ups_sent: followUpNumber,
        last_followup_sent_at: now,
        next_followup_scheduled_at: isLast ? now : computeNextScheduledAt(now, followUpNumber, nextTmpl?.delay_in_hours ?? undefined),
        status: isLast ? "completed" : "active",
        updated_at: now,
      }).eq("id", row.id);

      broadcastSSE("followup-sent", { botId: DEFAULT_BOT, userName: row.user_name, followUpNumber, isFinal: isLast });
      return res.json({ success: true, followUpNumber, message: `Follow-up #${followUpNumber} sent to ${row.user_name || "prospect"}.` });
    }

    return res.status(400).json({ success: false, error: `Unknown action: ${action}` });
  } catch (err) {
    const msg = err instanceof Error ? err.message : "Unknown error";
    const unreachable = /fetch failed|ECONNREFUSED|timeout|aborted/i.test(msg);
    return res.status(unreachable ? 502 : 500).json({
      success: false,
      error: unreachable ? "Cindy engine (Python backend) is unreachable." : msg,
    });
  }
});

export default router;
