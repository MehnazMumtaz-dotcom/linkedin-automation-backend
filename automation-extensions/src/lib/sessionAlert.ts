import { supabase, getBotId } from "./supabase";
import { broadcastSSE, broadcastCronStatus } from "./sseEmitter";

export async function createSessionAlert(botName: string, platform: string): Promise<void> {
  try {
    const botId = await getBotId(botName);

    const { data: existing } = await supabase
      .from("session_alerts")
      .select("id")
      .eq("bot_id", botId)
      .eq("status", "pending")
      .maybeSingle();

    if (!existing) {
      await supabase.from("session_alerts").insert({
        bot_id: botId,
        platform,
        status: "pending",
      });
    }
  } catch (err) {
    console.error(`[sessionAlert] Failed to create alert for ${botName}:`, err);
  }

  broadcastSSE("session-expired", { botId: botName, platform });
  broadcastCronStatus();
}

export async function resolveSessionAlert(botName: string): Promise<void> {
  try {
    const botId = await getBotId(botName);
    await supabase
      .from("session_alerts")
      .update({ status: "resolved", resolved_at: new Date().toISOString() })
      .eq("bot_id", botId)
      .eq("status", "pending");
  } catch (err) {
    console.error(`[sessionAlert] Failed to resolve alert for ${botName}:`, err);
  }

  broadcastSSE("session-restored", { botId: botName });
  broadcastCronStatus();
}
