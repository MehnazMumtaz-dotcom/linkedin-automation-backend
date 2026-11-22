import express from "express";
import cors from "cors";
import cookieParser from "cookie-parser";
import dotenv from "dotenv";
import { connectDatabase } from "./db";

// ── Route imports ──────────────────────────────────────────────────────────
// LinkedIn Automation Suite: Scraper, Ceevee, Cara, Cindy + session/safety infra
import authRoutes from "./routes/auth";
import scrapeRoutes from "./routes/scrape";
import ceeveeRoutes from "./routes/ceevee";
import caraRoutes from "./routes/cara";
import cindyRoutes from "./routes/cindy";
import knowledgeBaseRoutes from "./routes/knowledge-base";
import escalationRoutes from "./routes/escalation";
import sessionAlertRoutes from "./routes/session-alert";
import followUpRoutes from "./routes/follow-up";
import notificationRoutes from "./routes/notification";
import cronControlRoutes from "./routes/cron-control";
import debugRoutes from "./routes/debug";
import botsRoutes from "./routes/bots";
import eventsRoutes from "./routes/events";
import storageRoutes from "./routes/storage";
import { startSessionWatcher } from "./lib/sessionWatcher";

dotenv.config();

const app = express();

// ── Middleware ─────────────────────────────────────────────────────────────
const allowedOrigins = [
  "http://localhost:3000",
  "http://127.0.0.1:3000",
  ...(process.env.NEXT_PUBLIC_URL ? [process.env.NEXT_PUBLIC_URL] : []),
];

app.use(
  cors({
    origin: (origin, callback) => {
      if (!origin) return callback(null, true);
      if (allowedOrigins.some((o) => origin.startsWith(o))) {
        return callback(null, true);
      }
      return callback(null, true);
    },
    credentials: true,
    methods: ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allowedHeaders: ["Content-Type", "Authorization", "x-api-key", "Cookie", "ngrok-skip-browser-warning"],
  })
);

app.use(express.json({ limit: "10mb" }));
app.use(express.urlencoded({ extended: true }));
app.use(cookieParser());

// ── Routes ─────────────────────────────────────────────────────────────────
app.use("/api/auth", authRoutes);
app.use("/api/scrape", scrapeRoutes);
// scrape-stream is a sub-route inside scrapeRoutes (POST /stream)
app.use("/api/scrape-stream", (req, res, next) => {
  if (req.path === "/") req.url = "/stream";
  scrapeRoutes(req, res, next);
});

app.use("/api/cara", caraRoutes);
app.use("/api/cindy", cindyRoutes);
app.use("/api/ceevee", ceeveeRoutes);
app.use("/api/ceevee-stream", (req, res, next) => {
  if (req.path === "/") req.url = "/stream";
  ceeveeRoutes(req, res, next);
});

app.use("/api/knowledge-base", knowledgeBaseRoutes);
app.use("/api/escalation", escalationRoutes);
app.use("/api/session-alert", sessionAlertRoutes);
app.use("/api/follow-up", followUpRoutes);
app.use("/api/notification", notificationRoutes);
app.use("/api/cron-control", cronControlRoutes);
app.use("/api/debug", debugRoutes);
app.use("/api/bots", botsRoutes);
app.use("/api/events", eventsRoutes);
app.use("/api/storage", storageRoutes);

// ── Health check ────────────────────────────────────────────────────────────
app.get("/", (_req, res) => res.json({ message: "LinkedIn Automation Suite API is running", version: "1.0.0" }));
app.get("/health", (_req, res) => res.json({ ok: true, ts: new Date().toISOString() }));

// ── Start server ────────────────────────────────────────────────────────────
const PORT = Number(process.env.PORT ?? 3005);

import { warmBotIdCache } from "./lib/supabase";

// Warm Supabase bot ID cache then start server
warmBotIdCache().catch((err) => console.error("[Supabase] Cache warm failed:", err));

connectDatabase().then(() => {
  startSessionWatcher();

  const server = app.listen(PORT, "0.0.0.0", () => {
    console.log(`[LinkedIn Suite] Express backend running on http://localhost:${PORT}`);
    console.log(`[LinkedIn Suite] NODE_ENV = ${process.env.NODE_ENV ?? "development"}`);
  });

  server.timeout = 600000;
  server.keepAliveTimeout = 600000;
  server.headersTimeout = 600000;
  server.requestTimeout = 0;

  const SILENT_SOCKET_ERRORS = new Set([
    "ECONNRESET",
    "EPIPE",
    "ETIMEDOUT",
    "HPE_INVALID_URL",
    "HPE_INVALID_METHOD",
    "HPE_INVALID_HEADER_TOKEN",
    "HPE_HEADER_OVERFLOW",
  ]);

  server.on("connection", (socket) => {
    socket.on("error", (err: NodeJS.ErrnoException) => {
      if (SILENT_SOCKET_ERRORS.has(err.code ?? "")) return;
      console.error("[server] Socket error:", err);
    });
  });
});

// ── Global Error Handler (Ensures JSON responses) ──────────────────────────
app.use((err: any, req: express.Request, res: express.Response, next: express.NextFunction) => {
  console.error("[Global Error]:", err);
  if (!res.headersSent) {
    res.status(err.status || 500).json({
      success: false,
      error: err.message || "Internal Server Error",
    });
  }
});


// ── Global Error Handlers ───────────────────────────────────────────────────
process.on("uncaughtException", (err) => {
  console.error("CRITICAL: Uncaught Exception:", err);
  // In dev we keep running, in prod you might want to exit(1)
});

process.on("unhandledRejection", (reason, promise) => {
  console.error("CRITICAL: Unhandled Rejection at:", promise, "reason:", reason);
});
