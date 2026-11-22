/**
 * LinkedIn data extraction via authenticated HTTP requests.
 * Express-compatible version (no Next.js dependencies).
 */

import { Request } from "express";

export interface LinkedInProfile {
  name: string;
  headline: string;
  location: string;
  profileUrl: string;
  vanityName: string;
}

// ─── DB-aware session getter ──────────────────────────────────────────────────

export async function getLinkedInCookies(req?: Request): Promise<string | null> {
  try {
    const { getBotSession } = await import("./supabase");
    const session = await getBotSession("cindy");
    if (session?.extra_data?.rawCookies) return session.extra_data.rawCookies as string;
    if ((session as any)?.raw_cookies) return (session as any).raw_cookies as string;
  } catch {
    // DB unavailable — fall through to cookie
  }
  // Express cookie (requires cookie-parser middleware)
  return (req?.cookies?.li_session as string | undefined) ?? null;
}

export async function getLinkedInCookiesForCron(fallback?: string | null): Promise<string | null> {
  try {
    const { getBotSession } = await import("./supabase");
    const session = await getBotSession("cindy");
    if (session?.extra_data?.rawCookies) return session.extra_data.rawCookies as string;
    if ((session as any)?.raw_cookies) return (session as any).raw_cookies as string;
  } catch {
    // DB unavailable
  }
  return fallback ?? null;
}

// ─── cookie helpers ────────────────────────────────────────────────────────────

export function parseCookieString(raw: string): Record<string, string> {
  const map: Record<string, string> = {};
  for (const part of raw.split(";")) {
    const idx = part.indexOf("=");
    if (idx === -1) continue;
    const key = part.slice(0, idx).trim();
    const val = part.slice(idx + 1).trim();
    if (key) map[key] = val;
  }
  return map;
}

export function extractJsessionId(cookieString: string): string | null {
  const cookies = parseCookieString(cookieString);
  const raw = cookies["JSESSIONID"];
  if (!raw) return null;
  return raw.replace(/^"(.*)"$/, "$1");
}

export function extractLiAt(cookieString: string): string | null {
  const cookies = parseCookieString(cookieString);
  return cookies["li_at"] ?? null;
}

export function extractVanityName(url: string): string {
  const match = url.match(/linkedin\.com\/in\/([^/?#]+)/i);
  if (!match) throw new Error(`Invalid LinkedIn profile URL: ${url}`);
  return match[1].replace(/\/$/, "");
}

// ─── Human-like header variation helpers ──────────────────────────────────────

const _LI_UAS = [
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
];

function _pickLiUA(): string {
  return _LI_UAS[Math.floor(Math.random() * _LI_UAS.length)];
}

// ─── header builders ───────────────────────────────────────────────────────────

function buildHeaders(
  cookieString: string,
  accept = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
): Record<string, string> {
  const jsessionId = extractJsessionId(cookieString);
  const cookieMap = parseCookieString(cookieString);
  const cookieParts: string[] = [];

  if (cookieMap["li_at"]) cookieParts.push(`li_at=${cookieMap["li_at"]}`);
  if (jsessionId) cookieParts.push(`JSESSIONID="${jsessionId}"`);

  for (const [k, v] of Object.entries(cookieMap)) {
    if (k === "li_at" || k === "JSESSIONID") continue;
    cookieParts.push(`${k}=${v}`);
  }

  const ua = _pickLiUA();
  const chromeVer = ua.match(/Chrome\/([\d.]+)/)?.[1]?.split(".")[0] ?? "124";

  const headers: Record<string, string> = {
    Cookie: cookieParts.join("; "),
    "User-Agent": ua,
    Accept: accept,
    "Accept-Language": "en-US,en;q=0.9",
    Referer: "https://www.linkedin.com/feed/",
    Origin: "https://www.linkedin.com",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": `"Chromium";v="${chromeVer}", "Google Chrome";v="${chromeVer}", "Not-A.Brand";v="99"`,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
  };

  if (jsessionId) {
    headers["csrf-token"] = jsessionId;
    headers["x-li-lang"] = "en_US";
    headers["x-restli-protocol-version"] = "2.0.0";
  }

  return headers;
}

const _LI_AUTH_PATHS = ["/login", "/authwall", "/checkpoint", "/challenge", "/platform-change", "/uas/"];
function _isAuthUrl(url: string): boolean {
  return _LI_AUTH_PATHS.some(p => url.includes(p));
}

async function safeFetch(
  url: string,
  headers: Record<string, string>,
  followRedirects = false
): Promise<Response | null> {
  try {
    const res = await fetch(url, {
      headers,
      redirect: followRedirects ? "follow" : "manual",
    });
    if (!followRedirects && res.status >= 300 && res.status < 400) {
      // Small human-like pause before following platform/IP-change redirects
      await new Promise(r => setTimeout(r, Math.floor(Math.random() * 1200) + 600));
      const retryRes = await fetch(url, { headers, redirect: "follow", signal: AbortSignal.timeout(15000) });
      const finalUrl = retryRes.url || "";
      if (_isAuthUrl(finalUrl) || retryRes.status === 401 || retryRes.status === 403) return null;
      return retryRes;
    }
    if (followRedirects && _isAuthUrl(res.url || "")) return null;
    if (res.status === 401 || res.status === 403) return null;
    return res;
  } catch (err) {
    console.error("safeFetch error:", url, err);
    return null;
  }
}

function decodeHtmlEntities(str: string): string {
  return str
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&#x27;/g, "'")
    .replace(/&#x2F;/g, "/")
    .replace(/&#(\d+);/g, (_, code) => String.fromCharCode(Number(code)))
    .replace(/&#x([0-9a-fA-F]+);/g, (_, hex) => String.fromCharCode(parseInt(hex, 16)));
}

// ─── validate ─────────────────────────────────────────────────────────────────

export async function validateCookie(
  cookieString: string
): Promise<{ valid: boolean; name?: string }> {
  const liAt = extractLiAt(cookieString);
  if (!liAt) return { valid: false };

  const res = await safeFetch(
    "https://www.linkedin.com/feed/",
    buildHeaders(cookieString),
    true
  );

  if (!res) return { valid: false };

  // If the final redirect URL is an auth page, cookies are expired
  if (_isAuthUrl(res.url || "")) return { valid: false };

  const html = await res.text();

  if (
    html.includes("login-form") ||
    html.includes("join-form") ||
    html.includes("authwall") ||
    html.includes('"isAnonymous":true') ||
    html.includes("platform-change") ||
    html.includes("session-expired") ||
    html.includes("reauthenticate")
  ) {
    return { valid: false };
  }

  const name = extractNameFromHtml(html) ?? "LinkedIn User";
  return { valid: true, name };
}

// ─── HTML helpers ──────────────────────────────────────────────────────────────

function extractJsonFromCodeBlocks(html: string): string[] {
  const results: string[] = [];
  const blocks = html.match(/<code[^>]*>([\s\S]*?)<\/code>/gi) ?? [];
  for (const block of blocks) {
    let inner = block.replace(/<code[^>]*>/i, "").replace(/<\/code>/i, "").trim();
    if (inner.startsWith("<!--")) inner = inner.replace(/^<!--\s*/, "").replace(/\s*-->$/, "").trim();
    inner = decodeHtmlEntities(inner);
    if (!inner.startsWith("{") && !inner.startsWith("[")) continue;
    results.push(inner);
  }
  return results;
}

function extractNameFromHtml(html: string): string | null {
  // Try og:title
  const ogMatch = html.match(/<meta[^>]+property="og:title"[^>]+content="([^"]+)"/i);
  if (ogMatch) {
    const name = ogMatch[1].replace(/\s*\|.*$/, "").replace(/\s*-\s*LinkedIn$/, "").trim();
    if (name && name !== "LinkedIn") return name;
  }
  // Try title tag
  const titleMatch = html.match(/<title[^>]*>([^<]+)<\/title>/i);
  if (titleMatch) {
    const name = titleMatch[1].replace(/\s*\|.*$/, "").replace(/\s*-\s*LinkedIn$/, "").trim();
    if (name && name !== "LinkedIn") return name;
  }
  return null;
}

function parseProfileFromHtml(
  html: string,
  vanityName: string,
  profileUrl: string
): LinkedInProfile {
  const name = extractNameFromHtml(html) ?? vanityName;
  const descMatch = html.match(/<meta[^>]+name="description"[^>]+content="([^"]+)"/i);
  const description = descMatch ? decodeHtmlEntities(descMatch[1]) : "";
  const headline = description.split(" - ")[0]?.trim() ?? "";
  const location = description.split(" - ")[1]?.trim() ?? "";

  return { name, headline, location, profileUrl, vanityName };
}

function extractProfileFromEmbeddedJson(
  html: string,
  vanityName: string,
  profileUrl: string
): LinkedInProfile | null {
  const codeBlocks = extractJsonFromCodeBlocks(html);

  for (const jsonStr of codeBlocks) {
    try {
      const data: any = JSON.parse(jsonStr);
      const included = Array.isArray(data?.included) ? data.included : [];

      for (const item of included) {
        const entityUrn: string = item?.entityUrn ?? item?.urn ?? "";
        if (!entityUrn.includes("miniProfile:") && !entityUrn.includes("fs_profile:")) continue;
        if (item?.publicIdentifier && item.publicIdentifier !== vanityName) continue;

        const firstName = item?.firstName ?? "";
        const lastName = item?.lastName ?? "";
        const name = `${firstName} ${lastName}`.trim();
        if (!name) continue;

        const headline = item?.occupation ?? item?.headline ?? "";
        const location = item?.locationName ?? item?.geoLocationName ?? "";

        return { name, headline, location, profileUrl, vanityName };
      }
    } catch { /* not valid JSON */ }
  }

  return null;
}

async function fetchProfileFromApi(
  cookieString: string,
  vanityName: string,
  profileUrl: string
): Promise<LinkedInProfile> {
  const jsessionId = extractJsessionId(cookieString);
  const cookieMap = parseCookieString(cookieString);
  const cookieParts: string[] = [];
  if (cookieMap["li_at"]) cookieParts.push(`li_at=${cookieMap["li_at"]}`);
  if (jsessionId) cookieParts.push(`JSESSIONID="${jsessionId}"`);
  for (const [k, v] of Object.entries(cookieMap)) {
    if (k !== "li_at" && k !== "JSESSIONID") cookieParts.push(`${k}=${v}`);
  }

  const _ua = _pickLiUA();
  const _cv = _ua.match(/Chrome\/([\d.]+)/)?.[1]?.split(".")[0] ?? "124";
  const apiHeaders: Record<string, string> = {
    Cookie: cookieParts.join("; "),
    "csrf-token": jsessionId ?? "ajax:0000000000000000000",
    "User-Agent": _ua,
    Accept: "application/vnd.linkedin.normalized+json+2.1",
    "Accept-Language": "en-US,en;q=0.9",
    Referer: "https://www.linkedin.com/feed/",
    "x-li-lang": "en_US",
    "x-restli-protocol-version": "2.0.0",
    "sec-ch-ua": `"Chromium";v="${_cv}", "Google Chrome";v="${_cv}", "Not-A.Brand";v="99"`,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
  };

  try {
    const res = await fetch(
      `https://www.linkedin.com/voyager/api/identity/profiles/${vanityName}`,
      { headers: apiHeaders, redirect: "follow" }
    );
    if (res.ok) {
      const data = await res.json();
      const profile = data?.profile ?? data;
      const firstName = profile?.firstName ?? "";
      const lastName = profile?.lastName ?? "";
      const name = `${firstName} ${lastName}`.trim() || vanityName;
      const headline = profile?.headline ?? "";
      const location = profile?.locationName ?? "";
      return { name, headline, location, profileUrl, vanityName };
    }
  } catch { /* fall through */ }

  return { name: vanityName, headline: "", location: "", profileUrl, vanityName };
}

export async function fetchProfile(
  cookieString: string,
  vanityName: string
): Promise<LinkedInProfile> {
  const profileUrl = `https://www.linkedin.com/in/${encodeURIComponent(vanityName)}/`;

  const res = await safeFetch(profileUrl, buildHeaders(cookieString), true);

  if (!res?.ok) {
    return await fetchProfileFromApi(cookieString, vanityName, profileUrl);
  }

  const html = await res.text();

  if (html.includes("authwall") || html.includes("login-form") || html.includes('"isAnonymous":true')) {
    return await fetchProfileFromApi(cookieString, vanityName, profileUrl);
  }

  const profile = parseProfileFromHtml(html, vanityName, profileUrl);

  if (profile.name === vanityName || profile.name === "LinkedIn" || !profile.name) {
    const jsonProfile = extractProfileFromEmbeddedJson(html, vanityName, profileUrl);
    if (jsonProfile && jsonProfile.name !== vanityName && jsonProfile.name !== "LinkedIn") {
      return jsonProfile;
    }
    const apiProfile = await fetchProfileFromApi(cookieString, vanityName, profileUrl);
    if (apiProfile.name !== vanityName && apiProfile.name !== "LinkedIn") {
      return apiProfile;
    }
  }

  return profile;
}
