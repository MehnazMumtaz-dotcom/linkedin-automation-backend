import { Router, Request, Response } from "express";
import { extractVanityName, getLinkedInCookies } from "../lib/linkedin";

const router = Router();

// POST /api/scrape — scrape a LinkedIn profile via Python backend
router.post("/", async (req: Request, res: Response): Promise<void> => {
  try {
    const cookieString = await getLinkedInCookies(req);
    if (!cookieString) {
      res.status(401).json({ error: "Not authenticated. Please log in again." });
      return;
    }

    const { profileUrl, postsLimit = 10 } = req.body;

    if (!profileUrl || typeof profileUrl !== "string") {
      res.status(400).json({ error: "Please provide a valid LinkedIn profile URL" });
      return;
    }

    let vanityName: string;
    try {
      vanityName = extractVanityName(profileUrl.trim());
    } catch {
      res.status(400).json({ error: "Invalid LinkedIn profile URL. Expected: https://linkedin.com/in/username" });
      return;
    }

    const limit = Math.min(Math.max(1, Number(postsLimit) || 10), 50);

    const backendUrl = process.env.PYTHON_BACKEND_URL?.replace(/\/$/, "") || "http://127.0.0.1:8000";
    const apiResponse = await fetch(`${backendUrl}/scrape`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "ngrok-skip-browser-warning": "true",
        "User-Agent": "Express-Backend",
      },
      body: JSON.stringify({ cookieString, profileUrl: profileUrl.trim(), limit }),
    });

    if (!apiResponse.ok) {
      const errorText = await apiResponse.text();
      res.status(apiResponse.status).json({ error: `Scraper API error: ${apiResponse.statusText}. Details: ${errorText}` });
      return;
    }

    const result = await apiResponse.json();

    if (result.error) {
      if (result.error.includes("Not logged in") || result.error.includes("authenticate")) {
        res.status(401).json({ error: "Not logged in to LinkedIn. Please re-authenticate." });
        return;
      }
      res.status(500).json({ error: result.error });
      return;
    }

    res.json({
      success: true,
      profile: result.profile,
      posts: result.posts,
      totalPosts: result.posts.length,
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : "Unknown error";
    res.status(500).json({ error: `Scraper error: ${msg}` });
  }
});

// POST /api/scrape-stream — SSE streaming proxy to Python backend
router.post("/stream", async (req: Request, res: Response): Promise<void> => {
  try {
    const cookieString = await getLinkedInCookies(req);
    if (!cookieString) {
      res.status(401).json({ error: "Not authenticated. Please log in again." });
      return;
    }

    const { profileUrl, postsLimit = 10 } = req.body;

    if (!profileUrl || typeof profileUrl !== "string") {
      res.status(400).json({ error: "Please provide a valid LinkedIn profile URL" });
      return;
    }

    const limit = Math.min(Math.max(1, Number(postsLimit) || 10), 50);
    const backendUrl = process.env.PYTHON_BACKEND_URL?.replace(/\/$/, "") || "http://127.0.0.1:8000";

    console.log(`[scrape-stream] Proxying to ${backendUrl}/scrape-stream for ${profileUrl}`);

    const apiResponse = await fetch(`${backendUrl}/scrape-stream`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "ngrok-skip-browser-warning": "true",
        "User-Agent": "Express-Backend",
      },
      body: JSON.stringify({ cookieString, profileUrl: profileUrl.trim(), limit }),
    });

    if (!apiResponse.ok || !apiResponse.body) {
      const errorText = await apiResponse.text().catch(() => "Unknown error");
      console.error(`[scrape-stream] Python backend error: ${apiResponse.status} ${errorText}`);
      res.status(apiResponse.status).json({ error: `Scraper API error: ${apiResponse.statusText}. ${errorText}` });
      return;
    }

    // Set SSE headers
    res.setHeader("Content-Type", "text/event-stream");
    res.setHeader("Cache-Control", "no-cache");
    res.setHeader("Connection", "keep-alive");
    res.setHeader("X-Accel-Buffering", "no"); // Disable Nginx buffering
    res.setHeader("X-Content-Type-Options", "nosniff");
    res.flushHeaders();

    const reader = (apiResponse.body as any).getReader?.();
    if (reader) {
      const decoder = new TextDecoder();
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          const chunk = decoder.decode(value, { stream: true });
          res.write(chunk);
          // Standard Express res doesn't have flush() unless using compression middleware
          // But res.write should be immediate if no buffering is active
        }
      } catch (err: any) {
        if (err.name === 'AbortError' || err.message?.includes('terminated')) {
          console.warn("[scrape-stream] Stream closed by other side (intentional or crash).");
        } else {
          console.error("[scrape-stream] Stream reading error:", err);
        }
      }
    } else {
      // Node.js fetch - apiResponse.body is a ReadableStream (Node native)
      for await (const chunk of apiResponse.body as any) {
        res.write(chunk);
      }
    }

    res.end();
  } catch (err) {
    const msg = err instanceof Error ? err.message : "Unknown error";
    console.error("[scrape-stream] Fatal proxy error:", err);
    if (!res.headersSent) {
      res.status(500).json({ error: `Stream proxy error: ${msg}` });
    } else {
      if (!res.writableEnded) res.end();
    }
  }
});

export default router;
