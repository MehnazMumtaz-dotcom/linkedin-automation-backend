import { createClient } from "@supabase/supabase-js";
import dotenv from "dotenv";
import { broadcastSSE } from "./sseEmitter";

dotenv.config();

const SUPABASE_URL = (process.env.SUPABASE_URL ?? "").trim();
const SUPABASE_KEY = (process.env.SUPABASE_SERVICE_ROLE_KEY ?? "").trim();

/** False while .env still has the placeholder values (or nothing at all). */
export const isSupabaseConfigured =
  /^https?:\/\//.test(SUPABASE_URL) &&
  !SUPABASE_URL.includes("your-project-ref") &&
  SUPABASE_KEY.length > 20 &&
  !SUPABASE_KEY.startsWith("your-");

if (!isSupabaseConfigured) {
  console.warn(
    "[Supabase] NOT CONFIGURED — set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in automation-extensions/.env " +
    "(see the setup guide, section 'Database'). Database features will return errors until then."
  );
}

export const supabase = createClient(
  isSupabaseConfigured ? SUPABASE_URL : "https://not-configured.invalid",
  isSupabaseConfigured ? SUPABASE_KEY : "not-configured",
  { auth: { autoRefreshToken: false, persistSession: false } }
);

type SupabaseError = {
  code?: string;
  message?: string;
  details?: string | null;
  hint?: string | null;
};

function redactSensitiveDiagnostic(value: string | null | undefined): string | null {
  if (!value) return null;
  // PostgreSQL constraint errors can include a failing row, which may contain
  // LinkedIn cookies or tokens. Keep useful diagnostics without risking a leak.
  if (/li_at|jsessionid|csrf|cookie|access_token|token|api[_-]?key|authorization|secret/i.test(value)) {
    return "[redacted]";
  }
  return value;
}

function getSupabaseErrorContext(error: SupabaseError) {
  return {
    code: error.code ?? "unknown",
    message: redactSensitiveDiagnostic(error.message) ?? "Unknown Supabase error",
    details: redactSensitiveDiagnostic(error.details),
    hint: redactSensitiveDiagnostic(error.hint),
  };
}

function formatSupabaseError(context: ReturnType<typeof getSupabaseErrorContext>): string {
  return `code=${context.code}; message=${context.message}; details=${context.details ?? "none"}; hint=${context.hint ?? "none"}`;
}

function throwSupabaseOperationError(operation: string, error: SupabaseError): never {
  const context = getSupabaseErrorContext(error);
  // Deliberately omit row values: session rows contain LinkedIn credentials.
  console.error(`[Supabase] ${operation} failed`, context);
  throw new Error(`Supabase ${operation} failed: ${formatSupabaseError(context)}`);
}

/** Lightweight connectivity check used by GET /api/storage. */
export async function checkDatabase(): Promise<{ configured: boolean; reachable: boolean; suiteBotReady: boolean; error?: string }> {
  if (!isSupabaseConfigured) return { configured: false, reachable: false, suiteBotReady: false, error: "Supabase credentials not set" };
  try {
    const { data, error } = await supabase.from("bots").select("id").eq("name", "cindy").limit(1);
    if (error) return { configured: true, reachable: !/fetch failed|ENOTFOUND|ECONNREFUSED/i.test(error.message), suiteBotReady: false, error: error.message };
    return { configured: true, reachable: true, suiteBotReady: (data?.length ?? 0) > 0 };
  } catch (err) {
    return { configured: true, reachable: false, suiteBotReady: false, error: err instanceof Error ? err.message : String(err) };
  }
}

// ── Bot ID cache ───────────────────────────────────────────────────────────────
const _botIdCache: Record<string, string> = {};

export async function getBotId(name: string): Promise<string> {
  if (_botIdCache[name]) return _botIdCache[name];
  if (!isSupabaseConfigured) {
    throw new Error("Database not configured — set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in automation-extensions/.env");
  }
  const { data, error } = await supabase
    .from("bots")
    .select("id")
    .eq("name", name)
    .single();
  if (error) {
    const context = getSupabaseErrorContext(error);
    console.error("[Supabase] getBotId failed for public.bots", { name, ...context });

    // `bots.name` is unique, so PGRST116 here means the requested bot does not exist.
    if (context.code === "PGRST116") {
      throw new Error(`Bot not found in DB: ${name} — run the Supabase migration (it seeds this row)`);
    }

    throw new Error(`Supabase query failed for public.bots (name=${name}): ${formatSupabaseError(context)}`);
  }
  if (!data) throw new Error(`Bot not found in DB: ${name} — run the Supabase migration (it seeds this row)`);
  _botIdCache[name] = data.id;
  return data.id;
}

export async function warmBotIdCache(): Promise<void> {
  if (!isSupabaseConfigured) return;
  const { data } = await supabase.from("bots").select("id, name");
  if (data) {
    for (const bot of data) _botIdCache[bot.name] = bot.id;
    console.log(`[Supabase] Bot ID cache warmed: ${data.map((b: any) => b.name).join(", ")}`);
  }
}

// ── snake_case → camelCase deep converter ─────────────────────────────────────
export function toCamel(obj: any): any {
  if (Array.isArray(obj)) return obj.map(toCamel);
  if (obj && typeof obj === "object" && !(obj instanceof Date)) {
    return Object.fromEntries(
      Object.entries(obj).map(([k, v]) => [
        k.replace(/_([a-z])/g, (_, c) => c.toUpperCase()),
        toCamel(v),
      ])
    );
  }
  return obj;
}

// ── Session helpers ────────────────────────────────────────────────────────────

// Note: always read the NEWEST active row (order + limit 1). Older deployments
// accumulated several bot_sessions rows per bot, and .maybeSingle() errors (and
// returns null) when more than one row matches — which looked like "no session".
export async function getBotSession(botName: string): Promise<any | null> {
  const botId = await getBotId(botName);
  const { data } = await supabase
    .from("bot_sessions")
    .select("*")
    .eq("bot_id", botId)
    .eq("status", "active")
    .order("saved_at", { ascending: false })
    .limit(1)
    .maybeSingle();
  return data ?? null;
}

/**
 * Store the LinkedIn session (owned by Cindy) in exactly one bot_sessions row.
 * Updates the newest existing li_session row (works with or without a unique
 * constraint on bot_id+session_type), expires any duplicates, or inserts the
 * first row. Used by the Chrome extension endpoint and the manual login page.
 */
export async function saveLinkedInSession(params: {
  rawCookies: string;
  extra?: Record<string, any>;
  savedAt?: string;
  status?: string;
}): Promise<string> {
  const botId = await getBotId("cindy");
  const liAt = params.rawCookies.match(/(?:^|;\s*)li_at=([^;]+)/)?.[1] ?? null;
  const row = {
    bot_id: botId,
    session_type: "li_session",
    raw_cookies: params.rawCookies,
    access_token: liAt,
    extra_data: { ...(params.extra ?? {}), rawCookies: params.rawCookies },
    status: params.status ?? "active",
    saved_at: params.savedAt ?? new Date().toISOString(),
  };

  const { data: existing, error: selErr } = await supabase
    .from("bot_sessions")
    .select("id")
    .eq("bot_id", botId)
    .eq("session_type", "li_session")
    .order("saved_at", { ascending: false });
  if (selErr) throwSupabaseOperationError("select public.bot_sessions", selErr);

  if (existing && existing.length > 0) {
    const [keep, ...duplicates] = existing;
    const { error } = await supabase.from("bot_sessions").update(row).eq("id", keep.id);
    if (error) throwSupabaseOperationError("update public.bot_sessions", error);
    if (duplicates.length) {
      const { error: expireErr } = await supabase
        .from("bot_sessions")
        .update({ status: "expired" })
        .in("id", duplicates.map((d: any) => d.id));
      if (expireErr) throwSupabaseOperationError("expire duplicate public.bot_sessions rows", expireErr);
    }
    return keep.id as string;
  }

  const { data: inserted, error } = await supabase.from("bot_sessions").insert(row).select("id").single();
  if (error) throwSupabaseOperationError("insert public.bot_sessions", error);
  if (!inserted) throw new Error("Supabase insert public.bot_sessions returned no session id");
  return inserted.id as string;
}

export async function markSessionExpired(botName: string): Promise<void> {
  const botId = await getBotId(botName);
  await supabase
    .from("bot_sessions")
    .update({ status: "expired" })
    .eq("bot_id", botId)
    .eq("status", "active");
}

// ── Conversation helpers ───────────────────────────────────────────────────────

export async function getOrCreateConversation(
  botName: string,
  externalId: string,
  senderId: string,
  senderName: string,
  senderUsername?: string
): Promise<string> {
  const botId = await getBotId(botName);

  const { data: existing } = await supabase
    .from("conversations")
    .select("id")
    .eq("bot_id", botId)
    .eq("external_conversation_id", externalId)
    .maybeSingle();

  if (existing) return existing.id;

  const { data: created, error } = await supabase
    .from("conversations")
    .insert({
      bot_id: botId,
      external_conversation_id: externalId,
      sender_id: senderId,
      sender_name: senderName,
      sender_username: senderUsername ?? null,
      last_activity: new Date().toISOString(),
    })
    .select("id")
    .single();

  if (error || !created) throw new Error(`Failed to create conversation: ${error?.message}`);
  return created.id;
}

export async function getConversationMessages(conversationId: string): Promise<any[]> {
  const { data } = await supabase
    .from("messages")
    .select("role, text, sent_at, source")
    .eq("conversation_id", conversationId)
    .order("sent_at", { ascending: true });
  return data ?? [];
}

export async function appendMessage(
  conversationId: string,
  role: string,
  text: string,
  source: string
): Promise<void> {
  const now = new Date().toISOString();
  await supabase.from("messages").insert({
    conversation_id: conversationId,
    role,
    text,
    source,
    sent_at: now,
  });
  await supabase
    .from("conversations")
    .update({ last_activity: now })
    .eq("id", conversationId);
}

// ── Knowledge base helper ──────────────────────────────────────────────────────

export async function getKnowledgeBase(botName: string): Promise<any[]> {
  const botId = await getBotId(botName);
  const { data } = await supabase
    .from("knowledge_base")
    .select("*")
    .or(`bot_id.eq.${botId},bot_id.is.null`);
  return data ?? [];
}

// ── Escalation helper ──────────────────────────────────────────────────────────

export async function findOrCreateEscalation(
  botName: string,
  externalConvId: string,
  payload: {
    platform: string;
    senderName: string;
    senderUsername?: string;
    lastMessage: string;
    reason: string;
  }
): Promise<{ id: string; duplicate: boolean }> {
  const botId = await getBotId(botName);

  // Resolve conversation UUID from external ID
  const { data: conv } = await supabase
    .from("conversations")
    .select("id")
    .eq("bot_id", botId)
    .eq("external_conversation_id", externalConvId)
    .maybeSingle();
  const convId = conv?.id ?? null;

  // Check for existing pending escalation
  if (convId) {
    const { data: existing } = await supabase
      .from("escalations")
      .select("id")
      .eq("conversation_id", convId)
      .eq("status", "pending")
      .maybeSingle();
    if (existing) return { id: existing.id, duplicate: true };
  }

  const { data: created, error } = await supabase
    .from("escalations")
    .insert({
      bot_id: botId,
      conversation_id: convId,
      platform: payload.platform,
      sender_name: payload.senderName,
      sender_username: payload.senderUsername ?? null,
      last_message: payload.lastMessage,
      reason: payload.reason,
      status: "pending",
    })
    .select("id")
    .single();

  if (error || !created) throw new Error(`Failed to create escalation: ${error?.message}`);

  // FIRE SSE BROADCAST
  broadcastSSE("new-escalation", {
    botId: botName,
    platform: payload.platform,
    senderName: payload.senderName,
    reason: payload.reason,
    lastMessage: payload.lastMessage,
  });

  return { id: created.id, duplicate: false };
}

// ── Processed messages (dedup) ─────────────────────────────────────────────────
// Uses the atomic DB function mark_message_processed(p_bot_id, p_msg_id, p_conversation_id)
// Returns true if the message was newly marked (not a duplicate), false if already processed.

export async function markMessageProcessed(
  botName: string,
  msgId: string,
  externalConvId: string
): Promise<boolean> {
  const botId = await getBotId(botName);
  const { data, error } = await supabase.rpc("mark_message_processed", {
    p_bot_id: botId,
    p_msg_id: msgId,
    p_conversation_id: externalConvId,
  });
  if (error) {
    // Fallback: direct insert if RPC fails (e.g. param name mismatch during migration)
    const { error: insertErr } = await supabase.from("processed_messages").insert({
      bot_id: botId,
      external_msg_id: msgId,
      external_conversation_id: externalConvId,
    });
    return !insertErr; // insert succeeded → newly processed
  }
  return !!data; // DB function returns true = newly marked
}

// Kept for callers that only want to check without marking
export async function isMessageProcessed(botName: string, msgId: string): Promise<boolean> {
  const botId = await getBotId(botName);
  const { data } = await supabase
    .from("processed_messages")
    .select("id")
    .eq("bot_id", botId)
    .eq("external_msg_id", msgId)
    .single();
  return !!data;
}

// ── Conversation locking (prevents duplicate cron processing) ──────────────────
// Uses DB functions acquire_conversation_lock(p_id) and release_conversation_lock(p_id)

export async function acquireConversationLock(conversationId: string): Promise<boolean> {
  const { data, error } = await supabase.rpc("acquire_conversation_lock", { p_id: conversationId });
  if (error) return false;
  return !!data;
}

export async function releaseConversationLock(conversationId: string): Promise<void> {
  await supabase.rpc("release_conversation_lock", { p_id: conversationId });
}

// ── Growth log helper ──────────────────────────────────────────────────────────

export async function addGrowthLog(botName: string, log: {
  action: string;
  target_username?: string;
  target_url?: string;
  hashtag?: string;
  source_type?: string;
  source_value?: string;
  content?: string;
  status: string;
  error?: string;
  note?: string;
}): Promise<void> {
  const botId = await getBotId(botName);
  await supabase.from("growth_logs").insert({
    bot_id: botId,
    ...log,
    timestamp: new Date().toISOString(),
  });
}

// ── Bot settings helper ────────────────────────────────────────────────────────

export async function getBotSettings(botName: string): Promise<any> {
  const botId = await getBotId(botName);
  const { data } = await supabase
    .from("bot_settings")
    .select("data")
    .eq("bot_id", botId)
    .single();
  return data?.data ?? null;
}

export async function saveBotSettings(botName: string, settingsData: any): Promise<void> {
  const botId = await getBotId(botName);
  await supabase
    .from("bot_settings")
    .upsert({ bot_id: botId, data: settingsData, updated_at: new Date().toISOString() }, { onConflict: "bot_id" });
}

// ── Global/system settings (cron intervals) ────────────────────────────────────
// Stored in bot_settings with bot_id = cindy's id, key "cron_intervals" inside data jsonb

export async function getCronIntervals(): Promise<any> {
  const botId = await getBotId("cindy");
  const { data } = await supabase
    .from("bot_settings")
    .select("data")
    .eq("bot_id", botId)
    .single();
  return data?.data?.cron_intervals ?? null;
}

export async function saveCronIntervals(intervals: any): Promise<void> {
  const botId = await getBotId("cindy");
  const { data: current } = await supabase
    .from("bot_settings")
    .select("data")
    .eq("bot_id", botId)
    .single();
  const merged = { ...(current?.data ?? {}), cron_intervals: { ...(current?.data?.cron_intervals ?? {}), ...intervals } };
  await supabase
    .from("bot_settings")
    .upsert({ bot_id: botId, data: merged, updated_at: new Date().toISOString() }, { onConflict: "bot_id" });
}

// ── Register / process follow-ups ──────────────────────────────────────────────

export async function registerFollowUpRecord(
  botName: string,
  userId: string,
  userName: string,
  originalMessageId: string,
  originalMessageText: string,
  nextScheduledAt: string
): Promise<{ id: string; duplicate: boolean }> {
  const botId = await getBotId(botName);

  // Check existing active/paused
  const { data: existing } = await supabase
    .from("follow_up_records")
    .select("id")
    .eq("bot_id", botId)
    .eq("user_id", userId)
    .in("status", ["active", "paused"])
    .single();
  if (existing) return { id: existing.id, duplicate: true };

  // Check already replied
  const { data: replied } = await supabase
    .from("follow_up_records")
    .select("id")
    .eq("bot_id", botId)
    .eq("user_id", userId)
    .eq("status", "replied")
    .single();
  if (replied) return { id: replied.id, duplicate: true };

  const now = new Date().toISOString();
  const { data: created } = await supabase
    .from("follow_up_records")
    .insert({
      bot_id: botId,
      user_id: userId,
      user_name: userName,
      original_message_id: originalMessageId,
      original_message_text: originalMessageText,
      original_message_sent_at: now,
      reply_received: false,
      follow_ups_sent: 0,
      next_followup_scheduled_at: nextScheduledAt,
      status: "active",
    })
    .select("id")
    .single();

  return { id: created?.id ?? "", duplicate: false };
}

export async function markFollowUpReplied(botName: string, userId: string): Promise<void> {
  const botId = await getBotId(botName);
  const now = new Date().toISOString();
  await supabase
    .from("follow_up_records")
    .update({ status: "replied", reply_received: true, reply_received_at: now, updated_at: now })
    .eq("bot_id", botId)
    .eq("user_id", userId)
    .in("status", ["active", "paused"]);
}

export async function messageMatchesFollowUpRule(botName: string, messageText: string): Promise<boolean> {
  const botId = await getBotId(botName);
  const { data: rules } = await supabase
    .from("follow_up_rules")
    .select("value")
    .eq("bot_id", botId)
    .eq("enabled", true);
  if (!rules?.length) return false;
  const lower = messageText.toLowerCase();
  return rules.some((r: any) => lower.includes(r.value.toLowerCase()));
}
