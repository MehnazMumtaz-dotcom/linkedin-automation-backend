import { supabase, isSupabaseConfigured } from "./supabase";
import { resolveSessionAlert } from "./sessionAlert";
import { broadcastSSE } from "./sseEmitter";

const g = globalThis as any;

// LinkedIn Automation Suite — only Cindy's LinkedIn session is watched.
const BOT_SUSPENDED_FLAGS: Record<string, string[]> = {
  cindy:  ["cindy_inbox_sessionSuspended"],
};

export function startSessionWatcher(): void {
  if (g.__sessionWatcherStarted) return;
  if (!isSupabaseConfigured) {
    console.warn("[sessionWatcher] Skipped — Supabase is not configured.");
    return;
  }
  g.__sessionWatcherStarted = true;

  console.log("[sessionWatcher] Starting Supabase Realtime session watcher...");

  const handleSessionChange = async (payload: any) => {
    const newSession = payload.new;
    if (!newSession || newSession.status !== "active") return;

    const { data: bot } = await supabase
      .from("bots")
      .select("name")
      .eq("id", newSession.bot_id)
      .single();

    if (!bot) return;
    const botName: string = bot.name;
    if (!(botName in BOT_SUSPENDED_FLAGS)) return;
    const flags = BOT_SUSPENDED_FLAGS[botName];

    for (const flag of flags) g[flag] = false;
    console.log(`[sessionWatcher] ${botName} session renewed — resolving alert`);
    await resolveSessionAlert(botName);
    broadcastSSE("session-renewed", { botName, sessionType: newSession.session_type });
  };

  supabase
    .channel("bot_sessions_watch")
    .on("postgres_changes" as any, { event: "INSERT", schema: "public", table: "bot_sessions" }, handleSessionChange)
    .on("postgres_changes" as any, { event: "UPDATE", schema: "public", table: "bot_sessions" }, handleSessionChange)
    .subscribe((status: string) => {
      if (status === "SUBSCRIBED") {
        console.log("[sessionWatcher] Realtime subscription active on bot_sessions");
      }
    });
}
