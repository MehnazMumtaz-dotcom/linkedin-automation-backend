import { Router, Request, Response } from "express";
import OpenAI from "openai";
import { extractVanityName, getLinkedInCookies } from "../lib/linkedin";

const router = Router();

// ── Types ─────────────────────────────────────────────────────────────────────

interface Experience {
  position_title?: string;
  institution_name?: string;
  linkedin_url?: string;
  from_date?: string;
  to_date?: string;
  duration?: string;
  location?: string;
  description?: string;
}

interface Education {
  institution_name?: string;
  degree?: string;
  linkedin_url?: string;
  from_date?: string;
  to_date?: string;
  description?: string;
}

interface Accomplishment {
  category: string;
  title: string;
  issuer?: string;
  issued_date?: string;
  credential_id?: string;
  credential_url?: string;
  description?: string;
}

interface Interest {
  name: string;
  category: string;
  linkedin_url?: string;
}

interface Contact {
  type: string;
  value: string;
  label?: string;
}

interface Profile {
  name: string;
  headline: string;
  location: string;
  profileUrl: string;
  vanityName: string;
  about?: string;
  open_to_work?: boolean;
  profile_picture_url?: string;
  skills?: string[];
  experiences?: Experience[];
  educations?: Education[];
  accomplishments?: Accomplishment[];
  interests?: Interest[];
  contacts?: Contact[];
}

interface Post {
  urn: string;
  text: string;
  reactionsCount: number;
  commentsCount: number;
  repostsCount: number;
  postedDate: string;
  postUrl: string;
  imageUrls: string[];
  videoUrl: string | null;
  articleUrl: string | null;
}

// ── OpenAI ────────────────────────────────────────────────────────────────────

function getOpenAIClient(): OpenAI {
  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) {
    throw new Error("OPENAI_API_KEY environment variable is not set.");
  }
  return new OpenAI({ apiKey });
}

// ── Utility ───────────────────────────────────────────────────────────────────

function stripEmojis(text: string): string {
  return text
    .replace(
      /[\u{1F300}-\u{1F9FF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{1F000}-\u{1F02F}\u{1F0A0}-\u{1F0FF}\u{1F100}-\u{1F64F}\u{1F680}-\u{1F6FF}\u{1F900}-\u{1F9FF}\u{1FA70}-\u{1FAFF}\u{E0020}-\u{E007F}\u{2300}-\u{23FF}\u{2500}-\u{25FF}\u{2B00}-\u{2BFF}\u{200D}\u{FE0F}]/gu,
      "",
    )
    .replace(/\s+/g, " ")
    .trim();
}

function scorePost(p: Post): number {
  return (p.reactionsCount || 0) * 1 + (p.commentsCount || 0) * 3 + (p.repostsCount || 0) * 2;
}

// ── Formatter ─────────────────────────────────────────────────────────────────

function formatExperience(exp: Experience[] = []): string {
  if (!exp.length) return "None provided";
  return exp.map((e, i) => {
    const loc = e.location ? ` | ${e.location}` : "";
    const desc = e.description ? `\n   Description: ${stripEmojis(e.description).slice(0, 400)}` : "";
    return `[${i + 1}] ${e.position_title || "Unknown"} at ${e.institution_name || "Unknown"} (${e.from_date || "?"} – ${e.to_date || "?"}${e.duration ? `, ${e.duration}` : ""}${loc})${desc}`;
  }).join("\n\n");
}

function formatEducation(edu: Education[] = []): string {
  if (!edu.length) return "None provided";
  return edu.map(e => {
    const desc = e.description ? ` — ${stripEmojis(e.description).slice(0, 200)}` : "";
    return `- ${e.degree || "Degree"} at ${e.institution_name || "Unknown"} (${e.from_date || "?"} – ${e.to_date || "?"})${desc}`;
  }).join("\n");
}

function formatAccomplishments(acc: Accomplishment[] = []): string {
  if (!acc.length) return "None provided";
  return acc.map(a => `- [${a.category}] ${a.title}${a.issuer ? ` | Issued by: ${a.issuer}` : ""}${a.issued_date ? ` | ${a.issued_date}` : ""}`).join("\n");
}

function formatSkills(skills: string[] = []): string {
  if (!skills.length) return "None provided";
  return skills.slice(0, 50).join(", ");
}

function formatInterests(interests: Interest[] = []): string {
  if (!interests.length) return "None provided";
  const byCategory: Record<string, string[]> = {};
  for (const i of interests) {
    if (!byCategory[i.category]) byCategory[i.category] = [];
    byCategory[i.category].push(i.name);
  }
  return Object.entries(byCategory).map(([cat, names]) => `${cat}: ${names.join(", ")}`).join("\n");
}

function formatContacts(contacts: Contact[] = []): string {
  if (!contacts.length) return "None provided";
  return contacts.map(c => `- ${c.type}: ${c.value}${c.label ? ` (${c.label})` : ""}`).join("\n");
}

// ── OpenAI-powered deep prospect analysis ─────────────────────────────────────

async function generateProspectReport(
  openai: OpenAI,
  profile: Profile,
  posts: Post[],
): Promise<object> {
  const postTexts = posts.filter((p) => p.text?.trim());
  const topPosts = [...postTexts]
    .sort((a, b) => scorePost(b) - scorePost(a))
    .slice(0, 15);

  const postsSummary = topPosts
    .map((p, i) => {
      // If it has no reaction counts at all, it's highly likely to be a comment they left on someone else's post
      const isComment = p.reactionsCount === undefined;
      const typeLabel = isComment ? "Comment" : "Post";
      const metrics = isComment 
        ? "" 
        : `(${p.reactionsCount} reactions, ${p.commentsCount} comments, ${p.repostsCount} reposts) `;
      return `[${typeLabel} ${i + 1}] ${metrics}Date: ${p.postedDate || "unknown"}\n${stripEmojis(p.text).slice(0, 600)}`;
    })
    .join("\n\n---\n\n");

  const systemPrompt = `You are Ceevee, a prospect research assistant. Your job is to look at a LinkedIn person's profile and their posts, then write a simple, clear summary about them.

The goal: The user wants to message this person and needs to understand them well first. Write everything so a non-expert can read it quickly and understand it.

CRITICAL RULES:
- Do NOT use any emojis.
- Use simple, everyday English. Write short, clear sentences. Avoid big or complicated words.
- If you must use a technical term, briefly explain what it means.
- Be specific — mention actual things from their posts or profile (real quotes, dates, topics).
- Do not make things up. Only write what the data shows.
- Imagine you are explaining this person to a friend who does not know them at all.

Respond with valid JSON only. No markdown, no code fences.

JSON Schema:
{
  "executiveSummary": "3-5 short, simple sentences. Say who this person is, what their job is, what topics they care about, and what makes them stand out. Write it like you are telling a friend about someone you just looked up.",

  "profileAnalysis": {
    "roleLevel": "Their job level in simple terms (e.g. 'Company owner', 'Senior manager', 'Team lead', 'Junior employee')",
    "industryFocus": ["Up to 3 fields or industries they work in, written simply"],
    "areasOfExpertise": ["3-5 things they are clearly good at, based on their posts — use plain words"],
    "estimatedCompanyStage": "How big or mature their company seems (e.g. 'Small startup', 'Growing mid-size company', 'Large established company') — only if you can tell from the data"
  },

  "careerTrajectory": {
    "currentFocus": "What their current job is and what they are working on right now — in 1-2 simple sentences",
    "keyTransitions": "How their career has changed over time, written simply (e.g. 'They started as a developer, then moved into management')",
    "notableCompanies": ["The most important or well-known companies they have worked at"],
    "awardsAndCertifications": ["Any awards, certificates, or big achievements they have earned"],
    "educationBackground": "Where they studied and what they studied — keep it brief and simple"
  },

  "contentAnalysis": {
    "primaryTopics": [
      {
        "topic": "What this topic is about in simple words",
        "frequency": "How often they post about this (e.g. 'They post about this a lot — about 4 out of every 10 posts')",
        "stance": "What they think or believe about this topic, explained simply",
        "evidence": "A real quote or specific example from one of their posts"
      }
    ],
    "contentStyle": "How they write their posts — for example: 'They write short posts with bullet points', 'They tell personal stories', 'They share data and numbers', 'They are direct and to the point'",
    "postingPatterns": "How often they post and what kinds of posts they share (text, photos, articles, etc.)",
    "topPerformingContent": "What kind of posts get the most likes and comments, and give a simple example"
  },

  "professionalInsights": {
    "challengesMentioned": ["Problems or frustrations they have talked about publicly — written simply"],
    "achievementsMentioned": ["Things they are proud of or have achieved that they have shared"],
    "toolsAndTechnologies": ["Apps, tools, or software they mention using — explain briefly what each one is if it is not well known"],
    "networkAndInfluence": "How many people seem to engage with them, and who their audience seems to be — keep it simple"
  },

  "personalityProfile": {
    "communicationStyle": "How they come across when they write — for example: 'They are very direct and confident', 'They like to tell stories', 'They back up what they say with data'",
    "values": ["3-5 things that clearly matter to them, based on what they post about"],
    "petPeeves": ["Things they dislike or complain about in their posts"],
    "motivations": ["What seems to drive them or excite them, based on their posts"]
  },

  "conversationStarters": [
    {
      "approach": "A short name for this message approach (e.g. 'Mention their latest post', 'Ask about their experience')",
      "message": "A full message you can send right away. Keep it friendly and natural. Show that you read their profile. No more than 3-4 sentences. Use simple words.",
      "rationale": "In 1-2 sentences, explain why this message is a good idea — what specific thing from their profile or posts makes it relevant",
      "confidence": 70-99
    }
  ],

  "keyReferences": {
    "quotableLines": ["2-3 memorable lines from their posts that you could bring up in a conversation to show you read their content"],
    "topicsToAvoid": ["Topics or opinions they feel strongly about that could create a bad first impression if you bring them up the wrong way"],
    "commonGround": ["Things you could both relate to or talk about to build a connection"]
  }
}`;

  const userPrompt = `Analyze this LinkedIn prospect in depth.

PROSPECT PROFILE:
Name: ${profile.name}
Headline: ${profile.headline}
Location: ${profile.location || "Not specified"}
Open to Work: ${profile.open_to_work ? "Yes" : "No"}
Profile URL: ${profile.profileUrl}

ABOUT SUMMARY:
${profile.about ? stripEmojis(profile.about) : "None provided"}

CAREER EXPERIENCE:
${formatExperience(profile.experiences)}

EDUCATION:
${formatEducation(profile.educations)}

SKILLS:
${formatSkills(profile.skills)}

ACCOMPLISHMENTS (Certifications, Awards, Publications, etc.):
${formatAccomplishments(profile.accomplishments)}

INTERESTS (Companies, Groups, Topics they follow):
${formatInterests(profile.interests)}

CONTACT INFO:
${formatContacts(profile.contacts)}

THEIR RECENT POSTS AND COMMENTS (${postTexts.length} total, showing top ${topPosts.length} by engagement):

${postsSummary}

Produce a complete prospect intelligence dossier. Be thorough, specific, and analytical. Every section must contain actionable insights backed by evidence from the data above. Generate exactly 4 conversation starters with different approaches.`;

  const completion = await openai.chat.completions.create({
    model: "gpt-4o-mini",
    messages: [
      { role: "system", content: systemPrompt },
      { role: "user", content: userPrompt },
    ],
    temperature: 0.6,
    max_tokens: 4000,
    response_format: { type: "json_object" },
  });

  const raw = completion.choices[0]?.message?.content || "{}";
  let parsed: Record<string, unknown>;

  try {
    parsed = JSON.parse(raw);
  } catch {
    console.error("[ceevee] Failed to parse OpenAI response:", raw);
    throw new Error("Failed to parse AI response");
  }

  return parsed;
}

// ── Helper: send SSE event ────────────────────────────────────────────────────

function sseEvent(res: Response, stage: string, detail: string, pct: number, extra?: Record<string, unknown>) {
  const payload = { stage, detail, pct, ...extra };
  res.write(`data: ${JSON.stringify(payload)}\n\n`);
}

// ── SSE streaming route ────────────────────────────────────────────────────────

router.post("/stream", async (req: Request, res: Response) => {
  try {
    const cookieString = await getLinkedInCookies(req);
    if (!cookieString) {
      res.status(401).json({ error: "Not authenticated. Please log in again." });
      return;
    }

    const { profileUrl, postsLimit = 10 } = req.body;

    if (!profileUrl || typeof profileUrl !== "string") {
      res.status(400).json({ error: "profileUrl is required" });
      return;
    }

    try {
      extractVanityName(profileUrl.trim());
    } catch {
      res.status(400).json({ error: "Invalid LinkedIn profile URL. Expected: https://linkedin.com/in/username" });
      return;
    }

    const limit = Math.min(Math.max(1, Number(postsLimit) || 10), 50);
    const backendUrl = process.env.PYTHON_BACKEND_URL?.replace(/\/$/, "") || "http://127.0.0.1:8000";

    // ── Set SSE headers ──────────────────────────────────────────────────────
    res.setHeader("Content-Type", "text/event-stream");
    res.setHeader("Cache-Control", "no-cache, no-transform");
    res.setHeader("Connection", "keep-alive");
    res.setHeader("X-Accel-Buffering", "no");
    res.flushHeaders();

    // ── Heartbeat to keep the connection alive during long scrapes ───────────
    let heartbeatTimer: NodeJS.Timeout | null = setInterval(() => {
      res.write(`data: ${JSON.stringify({ stage: "heartbeat", pct: 0 })}\n\n`);
    }, 15000);

    const stopHeartbeat = () => {
      if (heartbeatTimer) {
        clearInterval(heartbeatTimer);
        heartbeatTimer = null;
      }
    };

    try {
      // ── Step 1: Stream scraping progress from Python backend ─────────────
      console.log(`[ceevee-stream] Starting scrape for ${profileUrl}`);
      sseEvent(res, "init", "Starting scrape…", 2);

      const scrapeResponse = await fetch(`${backendUrl}/scrape-stream`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "ngrok-skip-browser-warning": "true",
          "User-Agent": "Express-Backend",
        },
        body: JSON.stringify({ cookieString, profileUrl: profileUrl.trim(), limit }),
      });

      if (!scrapeResponse.ok || !scrapeResponse.body) {
        const errText = await scrapeResponse.text().catch(() => "Unknown error");
        sseEvent(res, "error", `Scraper error: ${errText}`, 100);
        stopHeartbeat();
        res.end();
        return;
      }

      // ── Parse SSE stream from Python and collect final result ─────────────
      let scrapeResult: Record<string, unknown> | null = null;
      const reader = (scrapeResponse.body as any).getReader?.();

      if (reader) {
        const decoder = new TextDecoder();
        let buffer = "";

        try {
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split("\n");
            buffer = lines.pop() || "";

            for (const line of lines) {
              const trimmed = line.trim();
              if (!trimmed || !trimmed.startsWith("data: ")) continue;

              try {
                const evt = JSON.parse(trimmed.slice(6)) as Record<string, unknown>;

                if (evt.stage === "result") {
                  scrapeResult = evt.data as Record<string, unknown>;
                } else if (evt.stage !== "heartbeat") {
                  // Forward progress events to the client (scraping is ~0-60% of total)
                  const pct = Math.round(((evt.pct as number) || 0) * 0.6);
                  res.write(`data: ${JSON.stringify({ stage: evt.stage, detail: evt.detail, pct })}\n\n`);
                }
              } catch {
                // ignore parse errors
              }
            }
          }
        } catch (err: any) {
          if (err.name === 'AbortError' || err.message?.includes('terminated')) {
            console.warn("[ceevee-stream] Scrape stream closed by other side.");
          } else {
            console.error("[ceevee-stream] Scrape stream reading error:", err);
          }
        } finally {
          reader.releaseLock?.();
        }
      } else {
        // Node native ReadableStream fallback
        let buffer = "";
        for await (const chunk of scrapeResponse.body as any) {
          buffer += Buffer.isBuffer(chunk) ? chunk.toString("utf-8") : chunk;
          const lines = buffer.split("\n");
          buffer = lines.pop() || "";

          for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed || !trimmed.startsWith("data: ")) continue;
            try {
              const evt = JSON.parse(trimmed.slice(6)) as Record<string, unknown>;
              if (evt.stage === "result") {
                scrapeResult = evt.data as Record<string, unknown>;
              } else if (evt.stage === "error") {
                // Capture the actual error detail from the Python scraper
                scrapeResult = { error: (evt.detail as string) || "Scrape failed" };
                const pct = Math.round(((evt.pct as number) || 0) * 0.6);
                res.write(`data: ${JSON.stringify({ stage: evt.stage, detail: evt.detail, pct })}\n\n`);
              } else if (evt.stage !== "heartbeat") {
                const pct = Math.round(((evt.pct as number) || 0) * 0.6);
                res.write(`data: ${JSON.stringify({ stage: evt.stage, detail: evt.detail, pct })}\n\n`);
              }
            } catch { /* ignore */ }
          }
        }
      }

      if (!scrapeResult || (scrapeResult as any).error) {
        const errMsg = (scrapeResult as any)?.error || "Scrape failed with no result";
        const errMsgLower = (errMsg as string).toLowerCase();
        if (errMsgLower.includes("not logged in") || errMsgLower.includes("authenticate") || errMsgLower.includes("cookies") || errMsgLower.includes("redirect") || errMsgLower.includes("expired")) {
          sseEvent(res, "error", "LinkedIn session expired. Please re-copy fresh cookies from LinkedIn and try again.", 100);
        } else {
          sseEvent(res, "error", errMsg as string, 100);
        }
        stopHeartbeat();
        res.end();
        return;
      }

      const profile = scrapeResult.profile as Profile;
      const originalPosts = (scrapeResult.posts as Post[]) || [];
      const comments = (scrapeResult.comments as Post[]) || [];
      const posts = [...originalPosts, ...comments];

      if (!profile?.name) {
        sseEvent(res, "error", "Could not extract profile data", 100);
        stopHeartbeat();
        res.end();
        return;
      }

      // ── Step 2: Run OpenAI analysis ───────────────────────────────────────
      sseEvent(res, "ai", "Analyzing profile with AI…", 65);
      console.log(`[ceevee-stream] Running AI analysis for ${profile.name} (${posts.length} posts)`);

      const openai = getOpenAIClient();
      const aiReport = await generateProspectReport(openai, profile, posts);

      sseEvent(res, "ai_done", "Report generated", 95);

      // ── Step 3: Send final result ─────────────────────────────────────────
      const finalPayload = {
        success: true,
        profile,
        posts: posts.slice(0, 5).map((p) => ({
          text: p.text?.slice(0, 200),
          reactionsCount: p.reactionsCount,
          commentsCount: p.commentsCount,
          repostsCount: p.repostsCount,
          postedDate: p.postedDate,
          postUrl: p.postUrl,
        })),
        report: aiReport,
        meta: {
          postsAnalyzed: posts.length,
          generatedAt: new Date().toISOString(),
          poweredBy: "openai",
        },
      };

      res.write(`data: ${JSON.stringify({ stage: "result", data: finalPayload, pct: 100 })}\n\n`);
      console.log(`[ceevee-stream] Done for ${profile.name}`);
    } catch (innerErr) {
      console.error("[ceevee-stream] Inner error:", innerErr);
      const msg = innerErr instanceof Error ? innerErr.message : "Unknown error";
      if (!res.writableEnded) {
        sseEvent(res, "error", `Ceevee error: ${msg}`, 100);
      }
    } finally {
      stopHeartbeat();
      if (!res.writableEnded) res.end();
    }
  } catch (outerErr) {
    console.error("[ceevee-stream] Outer error:", outerErr);
    const msg = outerErr instanceof Error ? outerErr.message : "Unknown error";
    if (!res.headersSent) {
      res.status(500).json({ error: `Ceevee stream error: ${msg}` });
    } else if (!res.writableEnded) {
      res.end();
    }
  }
});

// ── Route handler ─────────────────────────────────────────────────────────────

router.post("/", async (req: Request, res: Response) => {
  try {
    const cookieString = await getLinkedInCookies(req);
    if (!cookieString) {
      res.status(401).json({ error: "Not authenticated. Please log in again." });
      return;
    }

    const body = req.body;
    const { profileUrl, postsLimit = 10, profile: existingProfile, posts: existingPosts } = body;

    let profile: Profile;
    let posts: Post[];

    // If pre-scraped data is provided (from scraper page), use it directly
    if (existingProfile && existingPosts && existingPosts.length > 0) {
      console.log(`[ceevee] Using pre-scraped data for ${existingProfile.name}`);
      profile = existingProfile;
      posts = existingPosts;
    } else if (profileUrl) {
      // Otherwise, scrape the profile from the URL
      if (typeof profileUrl !== "string") {
        res.status(400).json({ error: "Invalid profile URL" });
        return;
      }

      try {
        extractVanityName(profileUrl.trim());
      } catch {
        res.status(400).json({ error: "Invalid LinkedIn profile URL. Expected: https://linkedin.com/in/username" });
        return;
      }

      const limit = Math.min(Math.max(1, Number(postsLimit) || 10), 50);
      console.log(`[ceevee] Fetching scrape data via API: ${profileUrl}, limit=${limit}`);

      const backendUrl = process.env.PYTHON_BACKEND_URL?.replace(/\/$/, '') || "http://127.0.0.1:8000";
      const apiResponse = await fetch(`${backendUrl}/scrape`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "ngrok-skip-browser-warning": "true",
          "User-Agent": "Express-Backend",
        },
        body: JSON.stringify({
          cookieString,
          profileUrl: profileUrl.trim(),
          limit,
        }),
      });

      if (!apiResponse.ok) {
        const errorText = await apiResponse.text();
        res.status(apiResponse.status).json({ error: `Scraper API error: ${apiResponse.statusText}. Details: ${errorText}` });
        return;
      }

      const result = await apiResponse.json() as any;

      if (result.error) {
        if (result.error.includes("Not logged in") || result.error.includes("authenticate")) {
          res.status(401).json({ error: "Not logged in to LinkedIn. Please re-authenticate." });
          return;
        }
        res.status(500).json({ error: result.error });
        return;
      }

      profile = result.profile;
      posts = result.posts;
    } else {
      res.status(400).json({ error: "Either profileUrl or profile+posts data is required" });
      return;
    }

    if (!profile || !profile.name) {
      res.status(500).json({ error: "Could not extract profile data" });
      return;
    }

    console.log(`[ceevee] Analyzing ${posts.length} posts for ${profile.name}`);

    // Generate the AI-powered report
    const openai = getOpenAIClient();
    const aiReport = await generateProspectReport(openai, profile, posts);

    console.log(`[ceevee] Report generated successfully for ${profile.name}`);

    res.json({
      success: true,
      profile,
      posts: posts.slice(0, 5).map((p) => ({
        text: p.text?.slice(0, 200),
        reactionsCount: p.reactionsCount,
        commentsCount: p.commentsCount,
        repostsCount: p.repostsCount,
        postedDate: p.postedDate,
        postUrl: p.postUrl,
      })),
      report: aiReport,
      meta: {
        postsAnalyzed: posts.length,
        generatedAt: new Date().toISOString(),
        poweredBy: "openai",
      },
    });
  } catch (err) {
    console.error("[ceevee] Fatal error:", err);
    const msg = err instanceof Error ? err.message : "Unknown error";
    res.status(500).json({ error: `Ceevee error: ${msg}` });
  }
});

export default router;
