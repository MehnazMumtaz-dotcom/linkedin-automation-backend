/**
 * LinkedIn Automation Suite — bot scope.
 *
 * The Supabase project may be shared with other (non-LinkedIn) bots, so every
 * list endpoint in this service is scoped to the bots below. Cindy owns the
 * LinkedIn session, the inbox, escalations, follow-ups and the knowledge base.
 */
export const SUITE_BOTS = ["cindy"] as const;
export type SuiteBot = (typeof SUITE_BOTS)[number];

/** Default bot for conversation-management features (inbox, KB, escalations, follow-ups). */
export const DEFAULT_BOT: SuiteBot = "cindy";

export function isSuiteBot(name: unknown): name is SuiteBot {
  return typeof name === "string" && (SUITE_BOTS as readonly string[]).includes(name);
}
