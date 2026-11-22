import { Response } from "express";

/**
 * SSE broadcast utility.
 *
 * Stored on globalThis so the client registry survives hot-reload / module
 * re-evaluation while the process is still running.
 */
const g = globalThis as any;

if (!g.__sseClients) {
  g.__sseClients = new Set<Response>();
}

const clients: Set<Response> = g.__sseClients;

export function addSSEClient(res: Response): void {
  clients.add(res);
}

export function removeSSEClient(res: Response): void {
  clients.delete(res);
}

/** Broadcast a named SSE event to every connected client. */
export function broadcastSSE(event: string, data: unknown): void {
  const payload = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
  for (const res of clients) {
    if (res.writableEnded || res.destroyed) {
      clients.delete(res);
      continue;
    }
    try {
      res.write(payload);
    } catch {
      // Client disconnected without clean-up — remove it
      clients.delete(res);
    }
  }
}

/**
 * Read current cron state from globalThis and return a structured snapshot.
 * LinkedIn Automation Suite has a single background job: Cindy's inbox cron
 * (runs in the Python Playwright engine, mirrored into these globals).
 */
export function getCronStatuses() {
  return {
    inbox: {
      cindy: {
        running: !!g.cindy_inbox_running,
        suspended: !!g.cindy_inbox_sessionSuspended,
        lastRun: g.cindy_inbox_lastRun ?? null,
      },
    },
    grow: {} as Record<string, { running: boolean; suspended: boolean; lastRun: string | null }>,
  };
}

/** Convenience: broadcast current cron status to all clients. */
export function broadcastCronStatus(): void {
  broadcastSSE("cron-status", getCronStatuses());
}
