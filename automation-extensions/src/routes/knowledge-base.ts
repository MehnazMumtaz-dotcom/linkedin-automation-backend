import { Router, Request, Response } from "express";
import { supabase, getBotId, toCamel } from "../lib/supabase";
import { DEFAULT_BOT, isSuiteBot } from "../lib/suite";

const router = Router();

// GET /api/knowledge-base
router.get("/", async (req: Request, res: Response) => {
  try {
    // Always scoped: suite bot entries + global (bot_id IS NULL) entries
    const requested = req.query.botId as string | undefined;
    const botName = isSuiteBot(requested) ? requested : DEFAULT_BOT;

    let q = supabase.from("knowledge_base").select("*").order("updated_at", { ascending: false });
    const botId = await getBotId(botName);
    q = (q as any).or(`bot_id.eq.${botId},bot_id.is.null`);

    const { data: entries, error } = await q;
    if (error) throw error;

    // Map bot_id UUID back to bot name for frontend compat
    const normalized = await Promise.all(
      (entries ?? []).map(async (e: any) => {
        let botIdName: string | null = null;
        if (e.bot_id) {
          const { data: bot } = await supabase.from("bots").select("name").eq("id", e.bot_id).single();
          botIdName = bot?.name ?? null;
        }
        return { ...toCamel(e), id: e.id, botId: botIdName ?? "all" };
      })
    );

    return res.json({ success: true, entries: normalized });
  } catch (err) {
    return res.status(500).json({ error: err instanceof Error ? err.message : "Unknown error" });
  }
});

// POST /api/knowledge-base
router.post("/", async (req: Request, res: Response) => {
  try {
    const { botId: botName, type, title, content } = req.body;
    if (!type || !title || !content) {
      return res.status(400).json({ error: "type, title, and content are required." });
    }

    // botId "all" → null (applies globally)
    if (botName && botName !== "all" && !isSuiteBot(botName)) {
      return res.status(400).json({ error: `Unsupported bot: ${botName}` });
    }
    const botId = botName && botName !== "all" ? await getBotId(botName) : null;

    const { data: created, error } = await supabase
      .from("knowledge_base")
      .insert({ bot_id: botId, type, title: title.trim(), content: content.trim() })
      .select("id")
      .single();

    if (error) throw error;
    return res.json({ success: true, id: created?.id });
  } catch (err) {
    return res.status(500).json({ error: err instanceof Error ? err.message : "Unknown error" });
  }
});

// PATCH /api/knowledge-base
router.patch("/", async (req: Request, res: Response) => {
  try {
    const { id, title, content, type, botId: botName } = req.body;
    if (!id) return res.status(400).json({ error: "id is required." });

    const updates: Record<string, any> = {};
    if (title)   updates.title   = title.trim();
    if (content) updates.content = content.trim();
    if (type)    updates.type    = type;
    if (botName !== undefined) {
      updates.bot_id = botName && botName !== "all" ? await getBotId(botName) : null;
    }

    await supabase.from("knowledge_base").update(updates).eq("id", id);
    return res.json({ success: true });
  } catch (err) {
    return res.status(500).json({ error: err instanceof Error ? err.message : "Unknown error" });
  }
});

// DELETE /api/knowledge-base
router.delete("/", async (req: Request, res: Response) => {
  try {
    const id = req.query.id as string;
    if (!id) return res.status(400).json({ error: "id is required." });
    await supabase.from("knowledge_base").delete().eq("id", id);
    return res.json({ success: true });
  } catch (err) {
    return res.status(500).json({ error: err instanceof Error ? err.message : "Unknown error" });
  }
});

export default router;
