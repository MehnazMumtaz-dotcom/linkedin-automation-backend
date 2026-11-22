import { Router, Request, Response } from "express";
import OpenAI from "openai";
import { supabase } from "../lib/supabase";
import { extractVanityName, getLinkedInCookies } from "../lib/linkedin";

const router = Router();

function getOpenAIClient(): OpenAI {
  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) throw new Error("OPENAI_API_KEY environment variable is not set.");
  return new OpenAI({ apiKey });
}

function stripEmojis(text: string): string {
  return text.replace(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{2BFF}\u{200D}\u{FE0F}]/gu, "").replace(/\s+/g, " ").trim();
}

// GET /api/cara — list profiles
router.get("/", async (req: Request, res: Response): Promise<void> => {
  try {
    const cookieString = await getLinkedInCookies(req);
    if (!cookieString) { res.status(401).json({ error: "Not authenticated." }); return; }

    const { data: profiles } = await supabase
      .from("profiles")
      .select("*")
      .order("last_updated", { ascending: false })
      .limit(200);

    res.json({ success: true, profiles: profiles ?? [] });
  } catch (err) {
    const msg = err instanceof Error ? err.message : "Unknown error";
    if (!res.headersSent && !res.destroyed) res.status(500).json({ error: msg });
  }
});

// GET /api/cara/personas
router.get("/personas", async (req: Request, res: Response): Promise<void> => {
  try {
    const cookieString = await getLinkedInCookies(req);
    if (!cookieString) { res.status(401).json({ error: "Not authenticated." }); return; }

    const search = req.query.search as string | undefined;
    const limit = parseInt(req.query.limit as string || "50");

    let q = supabase.from("cara_personas").select("*").order("last_updated", { ascending: false }).limit(limit);
    if (search) q = (q as any).or(`name.ilike.%${search}%,profile_url.ilike.%${search}%`);

    const { data: personas } = await q;
    res.json({ success: true, personas: personas ?? [] });
  } catch (err) {
    const msg = err instanceof Error ? err.message : "Unknown error";
    if (!res.headersSent && !res.destroyed) res.status(500).json({ error: msg });
  }
});

// DELETE /api/cara/personas
router.delete("/personas", async (req: Request, res: Response): Promise<void> => {
  try {
    const id = req.query.id as string;
    if (!id) { res.status(400).json({ error: "id is required." }); return; }

    await supabase.from("cara_simulation_sessions").delete().eq("persona_id", id);
    await supabase.from("cara_personas").delete().eq("id", id);
    res.json({ success: true });
  } catch (err) {
    const msg = err instanceof Error ? err.message : "Unknown error";
    if (!res.headersSent && !res.destroyed) res.status(500).json({ error: msg });
  }
});

// GET /api/cara/chat-logs
router.get("/chat-logs", async (req: Request, res: Response): Promise<void> => {
  try {
    const cookieString = await getLinkedInCookies(req);
    if (!cookieString) { res.status(401).json({ error: "Not authenticated." }); return; }

    const personaId = req.query.personaId as string | undefined;

    let q = supabase.from("cara_simulation_sessions").select("*").order("last_activity", { ascending: false }).limit(20);
    if (personaId) q = (q as any).eq("persona_id", personaId);

    const { data: sessions } = await q;
    res.json({ success: true, sessions: sessions ?? [] });
  } catch (err) {
    const msg = err instanceof Error ? err.message : "Unknown error";
    if (!res.headersSent && !res.destroyed) res.status(500).json({ error: msg });
  }
});

function _isLinkedInAuthErr(msg: string): boolean {
  const m = msg.toLowerCase();
  return m.includes("not logged in") || m.includes("authenticate") || m.includes("auth") ||
         m.includes("cookie") || m.includes("expired") || m.includes("redirect") ||
         m.includes("session") || m.includes("login") || m.includes("authwall");
}
const _AUTH_MSG = "LinkedIn session expired or cookies are invalid. Please re-copy fresh cookies from LinkedIn and try again.";

// POST /api/cara/analyze — scrape + generate persona analysis
router.post("/analyze", async (req: Request, res: Response): Promise<void> => {
  const isStream = req.query.stream === "true" || req.headers.accept === "text/event-stream";

  if (!isStream) {
    try {
      const cookieString = await getLinkedInCookies(req);
      if (!cookieString) { res.status(401).json({ error: "Not authenticated." }); return; }
      const { profileUrl, postsLimit = 10, force } = req.body;
      if (!profileUrl || typeof profileUrl !== "string") { res.status(400).json({ error: "profileUrl is required" }); return; }
      try { extractVanityName(profileUrl.trim()); } catch { res.status(400).json({ error: "Invalid LinkedIn profile URL." }); return; }

      const normalizedUrl = profileUrl.trim().replace(/\/$/, "");
      const { data: existingPersona } = await supabase
        .from("cara_personas")
        .select("*")
        .or(`profile_url.eq.${normalizedUrl},profile_url.eq.${normalizedUrl}/`)
        .single();

      if (existingPersona && !force) {
        res.json({ success: true, persona: existingPersona, isExisting: true, message: "Persona already exists in database." });
        return;
      }

      const backendUrl = process.env.PYTHON_BACKEND_URL?.replace(/\/$/, "") || "http://127.0.0.1:8000";
      const response = await fetch(`${backendUrl}/scrape-stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "ngrok-skip-browser-warning": "true", "User-Agent": "Express-Backend" },
        body: JSON.stringify({ cookieString, profileUrl: profileUrl.trim(), limit: Math.min(Math.max(1, Number(postsLimit) || 10), 50) }),
      });
      if (!response.ok) { res.status(response.status).json({ error: `Scraper stream error: ${response.statusText}` }); return; }

      let scrapeResult: any = null;
      const reader = (response.body as any).getReader?.() ?? null;
      let buffer = "";
      if (reader) {
        const decoder = new TextDecoder();
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() || "";
          for (const line of lines) {
            if (line.startsWith("data: ")) {
              try {
                const chunk = JSON.parse(line.slice(6));
                if (chunk.stage === "result") scrapeResult = chunk.data;
                else if (chunk.stage === "error") throw new Error(chunk.detail || "Scraper error");
              } catch (e: any) {
                if (e instanceof SyntaxError) continue;
                throw e;
              }
            }
          }
        }
      }

      if (!scrapeResult || scrapeResult.error) {
        const errMsg: string = scrapeResult?.error || "Failed to get scrape result";
        if (_isLinkedInAuthErr(errMsg)) { res.status(401).json({ error: _AUTH_MSG }); return; }
        res.status(500).json({ error: errMsg });
        return;
      }

      const analysis = await performPersonaAnalysis(scrapeResult.profile, scrapeResult.posts, scrapeResult.comments);
      const now = new Date().toISOString();
      const profile = scrapeResult.profile;

      await _upsertPersona(profile, analysis, scrapeResult.posts.length, now);
      await _upsertProfile(profile, analysis, now);

      const { data: saved } = await supabase.from("cara_personas").select("*").eq("profile_url", profile.profileUrl).single();
      res.json({ success: true, persona: saved });
    } catch (err: any) {
      if (!res.headersSent && !res.destroyed) {
        const msg: string = err.message || "Unknown error";
        if (_isLinkedInAuthErr(msg)) { res.status(401).json({ error: _AUTH_MSG }); return; }
        res.status(500).json({ error: msg });
      }
    }
    return;
  }

  // SSE streaming
  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection", "keep-alive");
  res.setHeader("X-Accel-Buffering", "no");
  res.flushHeaders();

  const sendEvent = (stage: string, message: string, pct: number, data?: any) => {
    res.write(`data: ${JSON.stringify({ stage, message, pct, data })}\n\n`);
  };
  const heartbeat = setInterval(() => {
    res.write(`data: ${JSON.stringify({ stage: "heartbeat", ts: new Date().toISOString() })}\n\n`);
  }, 15000);

  try {
    const cookieString = await getLinkedInCookies(req);
    if (!cookieString) throw new Error("Not authenticated.");
    const { profileUrl, postsLimit = 10, force } = req.body;
    if (!profileUrl) throw new Error("profileUrl is required");
    const normalizedUrl = profileUrl.trim().replace(/\/$/, "");

    const { data: existingPersona } = await supabase
      .from("cara_personas")
      .select("*")
      .or(`profile_url.eq.${normalizedUrl},profile_url.eq.${normalizedUrl}/`)
      .single();

    if (existingPersona && !force) {
      sendEvent("done", "Persona already exists.", 100, { persona: existingPersona, isExisting: true });
      return;
    }

    sendEvent("scrape", "Starting LinkedIn scrape...", 10);
    const backendUrl = process.env.PYTHON_BACKEND_URL?.replace(/\/$/, "") || "http://127.0.0.1:8000";
    const response = await fetch(`${backendUrl}/scrape-stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "ngrok-skip-browser-warning": "true" },
      body: JSON.stringify({ cookieString, profileUrl: profileUrl.trim(), limit: Number(postsLimit) || 10 }),
    });
    if (!response.ok) throw new Error(`Scraper error: ${response.statusText}`);

    let scrapeResult: any = null;
    const reader = (response.body as any).getReader?.() ?? null;
    let scrapeBuffer = "";
    if (reader) {
      const decoder = new TextDecoder();
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        scrapeBuffer += decoder.decode(value, { stream: true });
        const lines = scrapeBuffer.split("\n");
        scrapeBuffer = lines.pop() || "";
        for (const line of lines) {
          if (line.startsWith("data: ")) {
            try {
              const chunk = JSON.parse(line.slice(6));
              if (chunk.stage === "heartbeat") continue;
              if (chunk.stage === "result") scrapeResult = chunk.data;
              else if (chunk.stage === "error") {
                const detail: string = chunk.detail || "Scraper error";
                throw new Error(_isLinkedInAuthErr(detail) ? _AUTH_MSG : detail);
              } else sendEvent("scrape", chunk.detail || "Scraping...", Math.min(50, 10 + (chunk.pct || 0) * 0.4));
            } catch (e: any) {
              if (e instanceof SyntaxError) continue;
              throw e;
            }
          }
        }
      }
    }
    if (!scrapeResult) throw new Error("Scraping failed to return data.");

    sendEvent("analyze", "Analyzing personality & psychology...", 60);
    const analysis = await performPersonaAnalysis(scrapeResult.profile, scrapeResult.posts, scrapeResult.comments);

    sendEvent("save", "Saving to database...", 90);
    const now = new Date().toISOString();
    const profile = scrapeResult.profile;

    await _upsertPersona(profile, analysis, scrapeResult.posts.length, now);
    await _upsertProfile(profile, analysis, now);

    const { data: savedPersona } = await supabase.from("cara_personas").select("*").eq("profile_url", profile.profileUrl).single();
    sendEvent("done", "Analysis complete!", 100, { persona: savedPersona });
  } catch (err: any) {
    if (!res.writableEnded) {
      const msg: string = err.message || "Unknown error occurred";
      sendEvent("error", _isLinkedInAuthErr(msg) ? _AUTH_MSG : msg, 100);
    }
  } finally {
    clearInterval(heartbeat);
    if (!res.writableEnded) res.end();
  }
});

async function _upsertPersona(profile: any, analysis: any, postsAnalyzed: number, now: string) {
  await supabase.from("cara_personas").upsert({
    profile_url: profile.profileUrl,
    name: profile.name,
    headline: profile.headline || "",
    location: profile.location || "",
    analysis,
    posts_analyzed: postsAnalyzed,
    scraped_at: now,
    last_updated: now,
  }, { onConflict: "profile_url" });
}

async function _upsertProfile(profile: any, analysis: any, now: string) {
  await supabase.from("profiles").upsert({
    profile_url: profile.profileUrl,
    vanity_name: profile.vanityName || "",
    name: profile.name,
    headline: profile.headline || "",
    location: profile.location || "",
    executive_summary: analysis.executiveSummary || "",
    last_updated: now,
    scraped_at: now,
  }, { onConflict: "profile_url" });
}

async function performPersonaAnalysis(profile: any, posts: any[], comments: any[]): Promise<any> {
  const openai = getOpenAIClient();
  const topPosts = [...posts.filter((p: any) => p.text?.trim())].sort((a, b) => (b.reactionsCount || 0) - (a.reactionsCount || 0)).slice(0, 15);
  const postsSummary = topPosts.map((p, i) => `[Post ${i + 1}] (${p.reactionsCount} reactions)\n${stripEmojis(p.text).slice(0, 600)}`).join("\n\n---\n\n");
  const commentsSummary = comments.filter((c: any) => c.text?.trim()).slice(0, 10).map((c, i) => `[Comment ${i + 1}]\n${stripEmojis(c.text)}`).join("\n\n---\n\n");

  const systemPrompt = `You are Cara, the elite Client Avatar Simulation Engine. Analyze this LinkedIn prospect's psychology for sales simulation. Return valid JSON matching the schema provided.

    JSON Schema: { "executiveSummary": "3-sentence diagnostic", "communicationStyle": { "linguisticPatterns": [], "interactionModel": "..." }, "personalityProfile": { "tone": "...", "values": [], "motivations": [], "petPeeves": [], "decisionMakingStyle": "..." }, "professionalInsights": { "currentFocus": "...", "areasOfExpertise": [], "challenges": [], "achievements": [] }, "buyerProfile": { "buyerType": "...", "decisionFactors": [], "likelyObjections": [], "warmthLevel": 5, "trustBuilders": [] }, "salesApproach": { "bestApproach": "...", "openingAngles": [], "doThis": [], "avoidThis": [] } }`;

  const completion = await openai.chat.completions.create({
    model: "gpt-4o-mini",
    messages: [
      { role: "system", content: systemPrompt },
      { role: "user", content: `Analyze this LinkedIn prospect:\nName: ${profile.name}\nHeadline: ${profile.headline}\n\nPOSTS:\n${postsSummary}\n\nCOMMENTS:\n${commentsSummary}` },
    ],
    temperature: 0.6,
    max_tokens: 3000,
    response_format: { type: "json_object" },
  });

  return JSON.parse(completion.choices[0]?.message?.content || "{}");
}

// POST /api/cara/generate
router.post("/generate", async (req: Request, res: Response): Promise<void> => {
  try {
    const cookieString = await getLinkedInCookies(req);
    if (!cookieString) { res.status(401).json({ error: "Not authenticated." }); return; }

    const { profile, salesScript, chatHistory, mode } = req.body;
    if (!profile) { res.status(400).json({ error: "Profile data is required." }); return; }

    const resolvedMode = mode || "manual";
    const openai = getOpenAIClient();
    let prompt = "";
    let maxTokens = 800;

    if (resolvedMode === "manual") {
      if (!salesScript) { res.status(400).json({ error: "Sales script is required for manual mode." }); return; }
      prompt = `You are Cara, elite Client Avatar Simulation Engine. Evaluate this sales pitch for prospect ${profile.name} (${profile.headline}). Challenges: ${profile.challengesMentioned?.join(", ")}. Values: ${profile.values?.join(", ")}. Pitch: "${salesScript}". Return JSON: { "response": "...", "analysis": "...", "suggestedScript": "..." }`;
    } else if (resolvedMode === "chat-aware") {
      if (!salesScript) { res.status(400).json({ error: "A proposed message is required." }); return; }
      const hist = (chatHistory || []).map((m: any) => `[${m.role?.toUpperCase()}]: ${m.text}`).join("\n");
      prompt = `You are Cara. Analyze this LIVE LinkedIn conversation for prospect ${profile.name}.\n\nHistory:\n${hist || "No prior history"}\n\nProposed next message: "${salesScript}"\n\nReturn JSON: { "response": "...", "analysis": "...", "suggestedScript": "...", "personaInsights": { "buyerType": "...", "warmthLevel": 5, "objections": [], "triggers": [], "dealBreakers": [] } }`;
      maxTokens = 1200;
    } else if (resolvedMode === "script-improver") {
      const hist = (chatHistory || []).map((m: any) => `[${m.role?.toUpperCase()}]: ${m.text}`).join("\n");
      prompt = `You are Cara, Script Improver. Conversation for ${profile.name}:\n${hist || "No conversation"}\n\nGenerate the perfect next message. Return JSON: { "conversationAnalysis": "...", "suggestedScript": "...", "keyInsights": [], "personaInsights": { "buyerType": "...", "warmthLevel": 5, "objections": [], "triggers": [], "dealBreakers": [] } }`;
      maxTokens = 1200;
    } else {
      res.status(400).json({ error: `Invalid mode: ${resolvedMode}.` }); return;
    }

    const completion = await openai.chat.completions.create({
      model: "gpt-4o-mini",
      messages: [{ role: "system", content: prompt }],
      response_format: { type: "json_object" },
      temperature: 0.8,
      max_tokens: maxTokens,
    });

    const data = JSON.parse(completion.choices[0]?.message?.content || "{}");
    res.json({ success: true, mode: resolvedMode, ...data });
  } catch (err) {
    const msg = err instanceof Error ? err.message : "Unknown error";
    if (!res.headersSent && !res.destroyed) res.status(500).json({ error: msg });
  }
});

// POST /api/cara/simulate
router.post("/simulate", async (req: Request, res: Response): Promise<void> => {
  try {
    const cookieString = await getLinkedInCookies(req);
    if (!cookieString) { res.status(401).json({ error: "Not authenticated." }); return; }

    const { personaId, message, sessionId, conversationHistory } = req.body;
    if (!personaId) { res.status(400).json({ error: "personaId is required." }); return; }
    if (!message?.trim()) { res.status(400).json({ error: "Message is required." }); return; }

    const { data: persona } = await supabase.from("cara_personas").select("*").eq("id", personaId).single();
    if (!persona) { res.status(404).json({ error: "Persona not found." }); return; }

    const analysis = persona.analysis || {};
    const personality = analysis.personalityProfile || {};
    const professional = analysis.professionalInsights || {};
    const buyer = analysis.buyerProfile || {};

    const history = conversationHistory || [];
    const fullHistory = [...history, { role: "user", content: message }];
    const historyText = fullHistory.map((m: any) => `[${m.role === "user" ? "SALES REP" : persona.name.toUpperCase()}]: ${m.content}`).join("\n");

    const systemPrompt = `You are now FULLY embodying the persona of ${persona.name}. You ARE ${persona.name}. Not an AI assistant.

Name: ${persona.name} | Headline: ${persona.headline || "Professional"}
Values: ${(personality.values || []).join(", ") || "N/A"} | Tone: ${personality.tone || "Professional"}
Pet Peeves: ${(personality.petPeeves || []).join(", ") || "N/A"}
Focus: ${professional.currentFocus || "N/A"} | Expertise: ${(professional.areasOfExpertise || []).join(", ") || "N/A"}
Buyer Type: ${buyer.buyerType || "Professional"} | Deal Breakers: ${(buyer.dealBreakers || []).join(", ") || "N/A"}

Conversation: ${historyText || "(Start of conversation)"}

RULES: You ARE ${persona.name}. Match their tone, vocab, length. 1-4 sentences max. No emojis unless they use them. No JSON, no labels.`;

    const openai = getOpenAIClient();
    const completion = await openai.chat.completions.create({
      model: "gpt-4o-mini",
      messages: [{ role: "system", content: systemPrompt }, { role: "user", content: message }],
      temperature: 0.85,
      max_tokens: 500,
    });

    const responseText = completion.choices[0]?.message?.content?.trim() || "Could you tell me more?";
    const currentSessionId = sessionId || `session_${Date.now()}`;
    const now = new Date().toISOString();

    // Store session: conversation_topic = sessionId, simulated_responses = messages array
    const { data: existingSession } = await supabase
      .from("cara_simulation_sessions")
      .select("id, simulated_responses")
      .eq("persona_id", personaId)
      .eq("conversation_topic", currentSessionId)
      .single();

    const newMessages = [
      ...(existingSession?.simulated_responses ?? []),
      { role: "user", content: message, timestamp: now },
      { role: "persona", content: responseText, timestamp: now },
    ];

    if (existingSession) {
      await supabase.from("cara_simulation_sessions")
        .update({ simulated_responses: newMessages, last_activity: now })
        .eq("id", existingSession.id);
    } else {
      await supabase.from("cara_simulation_sessions").insert({
        persona_id: personaId,
        conversation_topic: currentSessionId,
        simulated_responses: newMessages,
        last_activity: now,
      });
    }

    res.json({ success: true, response: responseText, sessionId: currentSessionId, personaName: persona.name, timestamp: now });
  } catch (err) {
    const msg = err instanceof Error ? err.message : "Unknown error";
    if (!res.headersSent && !res.destroyed) res.status(500).json({ error: msg });
  }
});

// GET /api/cara/simulate
router.get("/simulate", async (req: Request, res: Response): Promise<void> => {
  try {
    const cookieString = await getLinkedInCookies(req);
    if (!cookieString) { res.status(401).json({ error: "Not authenticated." }); return; }

    const personaId = req.query.personaId as string | undefined;
    const sessionId = req.query.sessionId as string | undefined;

    let q = supabase.from("cara_simulation_sessions").select("*").order("last_activity", { ascending: false }).limit(20);
    if (personaId) q = (q as any).eq("persona_id", personaId);
    if (sessionId) q = (q as any).eq("conversation_topic", sessionId);

    const { data: sessions } = await q;
    res.json({ success: true, sessions: sessions ?? [], total: sessions?.length ?? 0 });
  } catch (err) {
    const msg = err instanceof Error ? err.message : "Unknown error";
    if (!res.headersSent && !res.destroyed) res.status(500).json({ error: msg });
  }
});

export default router;
