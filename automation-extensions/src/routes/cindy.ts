import { Router, Request as ExRequest, Response as ExResponse } from "express";
import OpenAI from "openai";
import { randomUUID, randomBytes } from "crypto";
import { supabase, getBotId, getKnowledgeBase, findOrCreateEscalation, getOrCreateConversation, appendMessage } from "../lib/supabase";
import { extractJsessionId, parseCookieString, getLinkedInCookies } from "../lib/linkedin";
import { broadcastCronStatus } from "../lib/sseEmitter";
import { createSessionAlert } from "../lib/sessionAlert";

const router = Router();

// ─── Python backend base URL ──────────────────────────────────────────────────
function getPythonBase(): string {
  return process.env.PYTHON_BACKEND_URL || "http://127.0.0.1:8000";
}

// ─── Startup: sync Cindy's Python-side cron state into Node.js globals ────────
// Without this, SSE broadcasts "Cindy running=false" until the first GET poll.
setTimeout(async () => {
  try {
    const r = await fetch(`${getPythonBase()}/cindy/inbox/cron`, {
      headers: { "ngrok-skip-browser-warning": "true" },
      signal: AbortSignal.timeout(5000),
    });
    const data = await r.json() as any;
    const g = globalThis as any;
    g.cindy_inbox_running = !!data.running;
    g.cindy_inbox_sessionSuspended = !!data.sessionSuspended;
    g.cindy_inbox_lastRun = data.lastRun ?? null;
    if (data.running) {
      broadcastCronStatus();
      console.log(`[cindy-cron-proxy] startup sync: running=${data.running}, interval=${data.intervalSeconds}s`);
    }
  } catch {
    // Python backend may not be up yet — first GET poll will sync it
  }
}, 3000);

const MY_PROFILE_URN = "urn:li:fsd_profile:ACoAAGUt7KsBAHMQRodG7z6z8EjY3JaOMFu5TmM";
const LINKEDIN_API_URL =
  "https://www.linkedin.com/voyager/api/voyagerMessagingGraphQL/graphql" +
  "?queryId=messengerConversations.0d5e6781bbee71c3e51c8843c6519f48" +
  "&variables=(mailboxUrn:urn%3Ali%3Afsd_profile%3AACoAAGUt7KsBAHMQRodG7z6z8EjY3JaOMFu5TmM)";

// ── Human-like LinkedIn header helpers ───────────────────────────────────────

const CHROME_UAS = [
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
];

const VIEWPORT_PROFILES = [
  { w: 1920, h: 1080, d: 1 },
  { w: 1440, h: 900,  d: 1 },
  { w: 1536, h: 864,  d: 1.25 },
];

const LI_CLIENT_VERSIONS = ["1.13.42791", "1.13.42890", "1.13.43012", "1.13.43150"];

function pickRandom<T>(arr: T[]): T {
  return arr[Math.floor(Math.random() * arr.length)];
}

function buildLiTrack(): string {
  const vp = pickRandom(VIEWPORT_PROFILES);
  const ver = pickRandom(LI_CLIENT_VERSIONS);
  return JSON.stringify({
    clientVersion: ver, mpVersion: ver, osName: "web",
    timezoneOffset: 5, timezone: "Asia/Karachi",
    deviceFormFactor: "DESKTOP", mpName: "voyager-web",
    displayDensity: vp.d, displayWidth: vp.w, displayHeight: vp.h,
  });
}

function buildLinkedInHeaders(cookieString: string, csrfToken: string) {
  const ua = pickRandom(CHROME_UAS);
  const chromeVer = ua.match(/Chrome\/([\d.]+)/)?.[1]?.split(".")[0] ?? "124";
  return {
    Cookie: cookieString,
    "csrf-token": csrfToken,
    Accept: "application/graphql",
    "x-restli-protocol-version": "2.0.0",
    "x-li-lang": "en_US",
    "x-li-track": buildLiTrack(),
    Referer: "https://www.linkedin.com/messaging/",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "sec-fetch-dest": "empty",
    "sec-ch-ua": `"Chromium";v="${chromeVer}", "Google Chrome";v="${chromeVer}", "Not-A.Brand";v="99"`,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": ua,
  };
}

function buildSendHeaders(cookieString: string, csrfToken: string) {
  const ua = pickRandom(CHROME_UAS);
  const chromeVer = ua.match(/Chrome\/([\d.]+)/)?.[1]?.split(".")[0] ?? "124";
  return {
    Cookie: cookieString,
    "csrf-token": csrfToken,
    "Content-Type": "text/plain;charset=UTF-8",
    Accept: "application/json",
    "x-restli-protocol-version": "2.0.0",
    "x-li-lang": "en_US",
    "x-li-track": buildLiTrack(),
    Referer: "https://www.linkedin.com/messaging/",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "sec-fetch-dest": "empty",
    "sec-ch-ua": `"Chromium";v="${chromeVer}", "Google Chrome";v="${chromeVer}", "Not-A.Brand";v="99"`,
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-mobile": "?0",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": ua,
    "x-li-sync": "=true",
    origin: "https://www.linkedin.com",
  };
}

function humanDelay(min = 500, max = 2500): Promise<void> {
  return new Promise((r) => setTimeout(r, Math.floor(Math.random() * (max - min + 1)) + min));
}

const CINDY_AUTH_PATHS = ["/login", "/authwall", "/checkpoint", "/challenge", "/platform-change", "/uas/"];
function isCindyAuthUrl(url: string): boolean {
  return CINDY_AUTH_PATHS.some(p => url.includes(p));
}

async function safeFetch(url: string, options: RequestInit = {}): Promise<Response | null> {
  try {
    const res = await fetch(url, { ...options, redirect: "manual" });
    if (res.status >= 300 && res.status < 400) {
      await new Promise(r => setTimeout(r, Math.floor(Math.random() * 1500) + 800));
      try {
        const retry = await fetch(url, { ...options, redirect: "follow", signal: AbortSignal.timeout(15000) });
        const finalUrl = retry.url || "";
        if (isCindyAuthUrl(finalUrl) || retry.status === 401 || retry.status === 403) return null;
        const ct = retry.headers.get("content-type") || "";
        if (retry.ok && !ct.includes("json") && !ct.includes("graphql")) return null;
        return retry;
      } catch { return null; }
    }
    if (res.status === 401 || res.status === 403) return null;
    return res;
  } catch (err) {
    console.error("[safeFetch-cindy]", err);
    return null;
  }
}

async function getKnowledgeContext(): Promise<string> {
  try {
    const entries = await getKnowledgeBase("cindy");
    if (!entries.length) return "";
    return "\n\nCOMPANY KNOWLEDGE BASE:\n" + entries.map((e: any) => `[${e.type.toUpperCase()}] ${e.title}:\n${e.content}`).join("\n\n");
  } catch { return ""; }
}

async function createEscalation(params: { conversationId: string; senderName: string; lastMessage: string; reason: string }): Promise<void> {
  try {
    await findOrCreateEscalation("cindy", params.conversationId, {
      platform: "LinkedIn",
      senderName: params.senderName,
      lastMessage: params.lastMessage,
      reason: params.reason,
    });
  } catch (err) { console.error("[cindy-escalation]", err); }
}

async function sendLinkedInMessage(cookieString: string, csrfToken: string, conversationUrn: string, replyText: string): Promise<boolean> {
  const rawThreadId = conversationUrn.replace("urn:li:messagingThread:", "");
  const fullConversationUrn = `urn:li:msg_conversation:(${MY_PROFILE_URN},${rawThreadId})`;
  const sendBody = JSON.stringify({
    message: { body: { attributes: [], text: replyText }, renderContentUnions: [], conversationUrn: fullConversationUrn, originToken: randomUUID() },
    mailboxUrn: MY_PROFILE_URN,
    trackingId: randomBytes(16).toString("binary"),
    dedupeByClientGeneratedToken: false,
  });
  const sendRes = await safeFetch(
    "https://www.linkedin.com/voyager/api/voyagerMessagingDashMessengerMessages?action=createMessage",
    { method: "POST", headers: buildSendHeaders(cookieString, csrfToken), body: sendBody }
  );
  return !!(sendRes?.ok);
}

// ── Routes ────────────────────────────────────────────────────────────────────

// GET /api/cindy — list profiles
router.get("/", async (req: ExRequest, res: ExResponse): Promise<void> => {
  try {
    const cookieString = await getLinkedInCookies(req);
    if (!cookieString) { res.status(401).json({ error: "Not authenticated." }); return; }
    const { data: profiles } = await supabase.from("profiles").select("*").order("last_updated", { ascending: false });
    res.json({ success: true, profiles: profiles ?? [] });
  } catch (err) { if (!res.headersSent && !res.destroyed) res.status(500).json({ error: err instanceof Error ? err.message : "Unknown error" }); }
});

// POST /api/cindy/generate — generate AI reply (UI-triggered, stays in Node.js)
router.post("/generate", async (req: ExRequest, res: ExResponse): Promise<void> => {
  try {
    const cookieString = await getLinkedInCookies(req);
    if (!cookieString) { res.status(401).json({ error: "Not authenticated." }); return; }

    const { profile, prospectMessage } = req.body;
    if (!profile) { res.status(400).json({ error: "Profile data is required." }); return; }
    if (!prospectMessage) { res.status(400).json({ error: "Prospect message is required." }); return; }

    const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });
    const knowledgeContext = await getKnowledgeContext();

    const prompt = `You are Cindy, professional Customer Service and Sales Support Bot for Devs Colab.\n\n${knowledgeContext}\n\nProspect Message: "${prospectMessage}"\nProfile: ${profile.name} - ${profile.headline || "N/A"}\n\nRULES:\n- SOCIAL CHAT: For greetings, small talk, pleasantries, casual questions — respond naturally. Set needsEscalation false.\n- KB ESCALATION: If a Knowledge Base entry for the topic says 'Send Notification for Human Approval', 'notify', 'escalate', or 'human review' — set needsEscalation true with the reason.\n- UNKNOWN BUSINESS QUESTION: For business questions (pricing, services, discounts, technical details) NOT in the Knowledge Base at all — set needsEscalation true.\n- KNOWN, NO ESCALATION: For business questions the KB fully answers (with no escalation instruction) — answer from KB content, set needsEscalation false.\n- Never invent prices, commitments, or technical details.\n\nReturn JSON: { "reply": "...", "needsEscalation": false, "escalationReason": "" }`;

    const completion = await openai.chat.completions.create({ model: "gpt-4o-mini", messages: [{ role: "system", content: prompt }], response_format: { type: "json_object" }, temperature: 0.7, max_tokens: 700 });
    const data = JSON.parse(completion.choices[0]?.message?.content || "{}");
    res.json({ success: true, reply: data.reply, needsEscalation: data.needsEscalation === true, escalationReason: data.escalationReason || "" });
  } catch (err) { if (!res.headersSent && !res.destroyed) res.status(500).json({ error: err instanceof Error ? err.message : "Unknown error" }); }
});

// GET /api/cindy/inbox — fetch unread conversations (Node.js, used by UI)
router.get("/inbox", async (req: ExRequest, res: ExResponse): Promise<void> => {
  try {
    const cookieString = await getLinkedInCookies(req);
    if (!cookieString) { res.status(401).json({ error: "Not authenticated." }); return; }

    const csrfToken = extractJsessionId(cookieString) || "";
    if (!csrfToken) { res.status(400).json({ error: "Could not extract JSESSIONID from cookies." }); return; }

    const apiRes = await safeFetch(LINKEDIN_API_URL, { headers: buildLinkedInHeaders(cookieString, csrfToken) });
    if (!apiRes) {
      const cindyBotId = await getBotId("cindy").catch(() => null);
      if (cindyBotId) {
        await supabase.from("bot_sessions").update({ status: "expired" }).eq("bot_id", cindyBotId).eq("session_type", "li_session");
        await createSessionAlert("cindy", "LinkedIn");
      }
      res.status(401).json({ error: "LinkedIn rejected the request — cookies may be expired." });
      return;
    }
    if (!apiRes.ok) { res.status(apiRes.status === 401 ? 401 : 502).json({ error: `LinkedIn API returned ${apiRes.status}` }); return; }

    const data = await apiRes.json();
    let elements: any[] = data?.data?.messengerConversationsBySyncToken?.elements ?? data?.messengerConversationsBySyncToken?.elements ?? [];
    const unread = elements.filter(conv => conv.unreadCount > 0 && conv.read !== true).map(conv => {
      const messages = (conv.messages?.elements || []).sort((a: any, b: any) => (b.deliveredAt || 0) - (a.deliveredAt || 0));
      const latest = messages[0];
      if (!latest) return null;
      const senderUrn = latest.actor?.hostIdentityUrn || latest.sender?.hostIdentityUrn || "";
      if (senderUrn === MY_PROFILE_URN) return null;
      const messageText = latest.body?.text || "";
      if (!messageText.trim()) return null;
      return {
        conversationUrn: conv.backendUrn,
        messageText,
        senderName: `${latest.actor?.participantType?.member?.firstName?.text || "there"} ${latest.actor?.participantType?.member?.lastName?.text || ""}`.trim(),
        senderUrn,
        deliveredAt: latest.deliveredAt || Date.now(),
        status: "unread",
      };
    }).filter(Boolean);

    res.json({ success: true, unread, totalFetched: unread.length, checkedAt: new Date().toISOString() });
  } catch (err) { if (!res.headersSent && !res.destroyed) res.status(500).json({ error: err instanceof Error ? err.message : "Unknown error" }); }
});

// POST /api/cindy/inbox — generate + send AI reply (UI-triggered, stays in Node.js)
router.post("/inbox", async (req: ExRequest, res: ExResponse): Promise<void> => {
  try {
    const cookieString = await getLinkedInCookies(req);
    if (!cookieString) { res.status(401).json({ error: "Not authenticated." }); return; }

    const csrfToken = extractJsessionId(cookieString) || "";
    if (!csrfToken) { res.status(400).json({ error: "Could not extract JSESSIONID from cookies." }); return; }

    const { conversationUrn, messageText, senderName, autoSend } = req.body;
    if (!conversationUrn || !messageText) { res.status(400).json({ error: "conversationUrn and messageText are required." }); return; }

    const openaiKey = process.env.OPENAI_API_KEY;
    if (!openaiKey) { res.status(500).json({ error: "OPENAI_API_KEY not configured." }); return; }

    const knowledgeCtx = await getKnowledgeContext();
    const systemPrompt =
      "You are Cindy, a professional LinkedIn assistant for Ammar Sharif at SparkoSol. Reply briefly, warmly and professionally. Keep replies under 3 sentences. No emojis.\n\n"
      + knowledgeCtx
      + "\n\n--- ESCALATION RULES (READ CAREFULLY) ---\n"
      + "RULE 1 — SOCIAL CHAT: For greetings, small talk, pleasantries, or clearly personal/casual questions — respond naturally WITHOUT ##ESCALATE##.\n\n"
      + "RULE 2 — KB SAYS ESCALATE: If a Knowledge Base entry for the topic says anything like 'Send Notification for Human Approval', 'notify', 'escalate', 'human review', or 'get back to you' — you MUST include ##ESCALATE## at the end: \"<your warm reply> ##ESCALATE## <short reason>\"\n\n"
      + "RULE 3 — UNKNOWN BUSINESS QUESTION: For business questions (pricing, discounts, services, technical details) NOT in the Knowledge Base — reply: \"Let me confirm this for you — our team will follow up shortly. ##ESCALATE## <short reason>\"\n\n"
      + "RULE 4 — KNOWN, NO ESCALATION: For business questions the Knowledge Base answers completely (no escalation instruction) — answer using KB content, no ##ESCALATE## needed.\n\n"
      + "IMPORTANT: ##ESCALATE## is stripped before sending — the prospect never sees it. When in doubt, escalate.";

    const aiRes = await fetch("https://api.openai.com/v1/chat/completions", {
      method: "POST",
      headers: { Authorization: `Bearer ${openaiKey}`, "Content-Type": "application/json" },
      body: JSON.stringify({ model: "gpt-4o-mini", max_tokens: 150, messages: [{ role: "system", content: systemPrompt }, { role: "user", content: `Reply to this LinkedIn message from ${senderName}: ${messageText}` }] }),
    });
    const aiData = await aiRes.json() as any;
    let rawReply = (aiData.choices?.[0]?.message?.content?.trim() as string) || "Thank you for reaching out!";

    const needsEscalation = rawReply.includes("##ESCALATE##");
    if (needsEscalation) {
      const parts = rawReply.split("##ESCALATE##");
      rawReply = parts[0].trim();
      await createEscalation({ conversationId: conversationUrn, senderName: senderName || "LinkedIn User", lastMessage: messageText, reason: (parts[1] || "Out-of-context query").trim() });
    }

    const replyText = rawReply.replace(/"/g, "'").replace(/\n/g, " ").replace(/\r/g, "");

    if (autoSend === false) { res.json({ success: true, reply: replyText, sent: false, conversationUrn }); return; }

    const sent = await sendLinkedInMessage(cookieString, csrfToken, conversationUrn, replyText);
    if (!sent) { res.json({ success: false, reply: replyText, sent: false, error: "LinkedIn rejected the send request." }); return; }

    try {
      const convId = await getOrCreateConversation("cindy", conversationUrn, "", senderName || "Unknown");
      await appendMessage(convId, "prospect", messageText, "linkedin_inbox");
      await appendMessage(convId, "cindy", replyText, "cindy_auto");
    } catch { /* non-fatal */ }

    res.json({ success: true, reply: replyText, sent: true, conversationUrn, sentAt: new Date().toISOString() });
  } catch (err) { if (!res.headersSent && !res.destroyed) res.status(500).json({ error: err instanceof Error ? err.message : "Unknown error" }); }
});

// ── Inbox cron — fully delegated to Python Playwright backend ─────────────────
// The cron tick, cookie management, message sending, follow-ups all run in
// Python using a Playwright browser context (mirrors the Scraper bot approach).

// GET /api/cindy/inbox/cron — proxy status from Python
router.get("/inbox/cron", async (_req: ExRequest, res: ExResponse): Promise<void> => {
  try {
    const pyBase = getPythonBase();
    console.log(`[cindy-cron-proxy] GET status → ${pyBase}/cindy/inbox/cron`);
    const r = await fetch(`${pyBase}/cindy/inbox/cron`, {
      headers: { "ngrok-skip-browser-warning": "true" },
      signal: AbortSignal.timeout(10000),
    });
    const data = await r.json() as any;

    // Sync Node.js globals so SSE broadcasts the correct Cindy status
    const g = globalThis as any;
    const wasRunning = !!g.cindy_inbox_running;
    g.cindy_inbox_running = !!data.running;
    g.cindy_inbox_sessionSuspended = !!data.sessionSuspended;
    g.cindy_inbox_lastRun = data.lastRun ?? null;
    if (wasRunning !== g.cindy_inbox_running) {
      broadcastCronStatus();
    }

    res.json(data);
  } catch (err) {
    console.error("[cindy-cron-proxy] GET error:", err);
    res.status(502).json({ running: false, sessionSuspended: false, lastRun: null, logs: [], processedCount: 0, error: "Python backend unreachable" });
  }
});

// POST /api/cindy/inbox/cron — proxy actions to Python
router.post("/inbox/cron", async (req: ExRequest, res: ExResponse): Promise<void> => {
  const { action, conversationUrn, messageText, senderName } = req.body;
  console.log(`[cindy-cron-proxy] POST action=${action}`);

  try {
    const pyBase = getPythonBase();
    const r = await fetch(`${pyBase}/cindy/inbox/cron`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "ngrok-skip-browser-warning": "true" },
      body: JSON.stringify({ action, conversationUrn, messageText, senderName }),
      signal: AbortSignal.timeout(60000), // allow time for Playwright warm-up on send_followup
    });
    const data = await r.json() as any;

    // Keep Node.js globals in sync so SSE reflects the latest Cindy state
    if (action === "start" || action === "stop") {
      const g = globalThis as any;
      g.cindy_inbox_running = action === "start";
      broadcastCronStatus();
    }

    res.json(data);
  } catch (err) {
    console.error("[cindy-cron-proxy] POST error:", err);
    // For send_followup, return the expected shape
    if (action === "send_followup") {
      res.json({ sent: false, error: "Python backend unreachable" });
    } else {
      res.status(502).json({ success: false, error: "Python backend unreachable" });
    }
  }
});

// GET /api/cindy/conversations — fetch stored conversation history from Supabase
router.get("/conversations", async (_req: ExRequest, res: ExResponse): Promise<void> => {
  try {
    const botId = await getBotId("cindy");
    const { data: conversations } = await supabase
      .from("conversations")
      .select("*, messages(*)")
      .eq("bot_id", botId)
      .order("last_activity", { ascending: false })
      .limit(50);
    res.json({ success: true, conversations: conversations ?? [] });
  } catch (err) {
    if (!res.headersSent && !res.destroyed) res.status(500).json({ error: err instanceof Error ? err.message : "Unknown error" });
  }
});

export default router;
