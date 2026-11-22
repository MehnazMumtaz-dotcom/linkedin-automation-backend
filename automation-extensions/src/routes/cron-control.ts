import { Router, Request, Response } from "express";
import { getCronStatuses } from "../lib/sseEmitter";

const router = Router();

// GET /api/cron-control — global cron status across all bots
router.get("/", (req: Request, res: Response) => {
  const statuses = getCronStatuses();
  const anyInboxRunning = Object.values(statuses.inbox).some((s) => s.running);
  const anyGrowRunning = Object.values(statuses.grow).some((s) => s.running);

  return res.json({ statuses, anyInboxRunning, anyGrowRunning });
});

export default router;
