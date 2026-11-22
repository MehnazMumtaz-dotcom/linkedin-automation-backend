import { Router, Request, Response } from "express";
import { checkDatabase } from "../lib/supabase";

const router = Router();

/**
 * GET /api/storage — where the suite keeps its data (shown by the "i" storage
 * badge in the frontend).
 *
 *  • Browser (localStorage, per device): lightweight UI state — scraper results,
 *    the latest Ceevee report and the Scraper → Ceevee hand-off.
 *  • Database (Supabase / PostgreSQL): everything the background services need —
 *    LinkedIn session, Cindy inbox history, knowledge base, escalations,
 *    follow-ups, Cara personas and settings.
 */
router.get("/", async (_req: Request, res: Response) => {
  const db = await checkDatabase();
  res.json({
    success: true,
    browser: {
      provider: "localStorage",
      items: ["Scraper results", "Latest Ceevee report", "Scraper → Ceevee hand-off"],
    },
    database: {
      provider: "supabase",
      ...db,
      items: [
        "LinkedIn session",
        "Cindy conversations & messages",
        "Knowledge base",
        "Escalations",
        "Follow-ups (records, rules, templates)",
        "Cara personas & simulations",
        "Cron settings",
      ],
    },
  });
});

export default router;
