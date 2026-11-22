"""Person Posts scraper for LinkedIn - scrapes posts from a personal profile's activity feed."""

import asyncio
import logging
import re
import random
import sys
from typing import List, Optional
from playwright.async_api import Page

from ..models.post import Post
from ..callbacks import ProgressCallback, SilentCallback
from .base import BaseScraper

logger = logging.getLogger(__name__)


class PersonPostsScraper(BaseScraper):
    """
    Async scraper for LinkedIn person profile posts/activity feed.
    
    Navigates to a person's recent activity page and extracts their posts
    including text, images, engagement metrics, and dates.
    
    Usage:
        async with BrowserManager(headless=False) as browser:
            await browser.load_session("session.json")
            scraper = PersonPostsScraper(browser.page)
            posts = await scraper.scrape("https://linkedin.com/in/username/", limit=10)
    """
    
    def __init__(self, page: Page, callback: Optional[ProgressCallback] = None):
        super().__init__(page, callback or SilentCallback())
    
    async def scrape(self, profile_url: str, limit: int = 10, activity_type: str = "all", on_progress=None) -> List[Post]:
        """
        Scrape posts from a LinkedIn person's activity feed.
        
        Args:
            profile_url: LinkedIn profile URL (e.g. https://linkedin.com/in/username/)
            limit: Maximum number of posts to scrape (default: 10)
            activity_type: Type of activity to scrape ("all", "posts", "comments", "shares")
            on_progress: Optional async callback(stage, detail, pct) for real-time progress
            
        Returns:
            List of Post objects with text, images, engagement data
        """
        logger.info(f"Starting person activity scraping: {profile_url} (type: {activity_type})")
        await self.callback.on_start("person_posts", profile_url)
        if on_progress:
            await on_progress("posts_nav", f"Navigating to {activity_type} activity feed", 5)
        
        # Extract the vanity name for author-filtering during extraction
        vanity_match = re.search(r'linkedin\.com/in/([^/?#]+)', profile_url)
        profile_vanity = vanity_match.group(1).strip('/').lower() if vanity_match else ""
        logger.info(f"Profile vanity for filtering: '{profile_vanity}'")
        
        # Build the activity/posts URL
        activity_url = self._build_activity_url(profile_url, activity_type)
        await self.navigate_and_wait(activity_url)
        await self.callback.on_progress("Navigated to activity page", 10)
        if on_progress:
            await on_progress("posts_nav", "Activity page loaded", 15)
        
        await self.check_rate_limit()
        
        # Wait for posts to load
        await self._wait_for_posts_to_load(on_progress=on_progress)
        await self.callback.on_progress("Posts loaded", 20)
        
        # Scrape posts with scrolling
        # For comments, the post author is someone else — disable author filtering
        filter_vanity = profile_vanity if activity_type != "comments" else ""
        posts = await self._scrape_posts(limit, profile_vanity=filter_vanity, on_progress=on_progress)
        await self.callback.on_progress(f"Scraped {len(posts)} items", 100)
        await self.callback.on_complete("person_posts", posts)
        
        logger.info(f"Successfully scraped {len(posts)} items from {profile_url}")
        return posts
    
    def _build_activity_url(self, profile_url: str, activity_type: str = "all") -> str:
        """Build the activity feed URL from a profile URL."""
        profile_url = profile_url.rstrip('/')
        
        # If already pointing to activity page, return as-is
        if '/recent-activity/' in profile_url:
            return profile_url
        
        # Remove trailing sections like /details/experience etc.
        # Keep only the base profile URL
        match = re.match(r'(https?://[^/]+/in/[^/]+)', profile_url)
        if match:
            base_url = match.group(1)
        else:
            base_url = profile_url
        
        return f"{base_url}/recent-activity/{activity_type}/"
    
    async def _wait_for_posts_to_load(self, timeout: int = 30000, on_progress=None) -> None:
        """Wait for the activity feed posts to load on the page."""
        try:
            await self.page.wait_for_load_state('domcontentloaded', timeout=timeout)
        except Exception as e:
            logger.debug(f"DOM load timeout: {e}")

        # Log current URL for debugging
        current_url = self.page.url
        print(f"[posts-debug] Current URL after nav: {current_url}", file=sys.stderr)

        # Shortened initial wait for dynamic content (1.5 - 3s)
        await self.page.wait_for_timeout(random.randint(1500, 3000))

        # Reduced from 5 attempts to 3 — posts usually load in 1-2 attempts
        for attempt in range(3):
            if on_progress:
                await on_progress("posts_loading", f"Waiting for posts (attempt {attempt+1}/3)", 20 + attempt * 5)
            await self._trigger_lazy_load()

            # Check multiple indicators for posts
            page_info = await self.page.evaluate('''() => {
                const html = document.body.innerHTML;
                return {
                    hasUrnActivity: html.includes('urn:li:activity:'),
                    hasUrnUgcPost: html.includes('urn:li:ugcPost:'),
                    hasUrnShare: html.includes('urn:li:share:'),
                    hasFeedUpdate: !!document.querySelector('.feed-shared-update-v2'),
                    hasOccludable: !!document.querySelector('.occludable-update'),
                    hasScaffold: !!document.querySelector('[data-id]'),
                    hasDataUrn: !!document.querySelector('[data-urn]'),
                    feedContainerCount: document.querySelectorAll('.feed-shared-update-v2, .occludable-update, [data-urn], [data-id]').length,
                    title: document.title,
                    bodyLen: html.length,
                };
            }''')

            print(f"[posts-debug] Attempt {attempt+1}: {page_info}", file=sys.stderr)

            if (page_info.get('hasUrnActivity') or page_info.get('hasUrnUgcPost') or
                page_info.get('hasUrnShare') or page_info.get('hasFeedUpdate') or
                page_info.get('hasOccludable') or page_info.get('feedContainerCount', 0) > 0):
                logger.debug(f"Posts found after attempt {attempt + 1}")
                if on_progress:
                    await on_progress("posts_found", f"Posts detected on attempt {attempt+1}", 35)
                return

            # Tighter wait between attempts (1.0 - 2.0s instead of 1.8 - 3.5s)
            await self.page.wait_for_timeout(random.randint(1000, 2000))

        logger.warning("Posts may not have loaded fully")
    
    async def _trigger_lazy_load(self) -> None:
        """Scroll the page with human-like variable speed to trigger lazy loading.
        Optimized: fewer steps (4-7 instead of 6-12) with tighter delays."""
        scroll_height = await self.page.evaluate('document.documentElement.scrollHeight')
        steps = random.randint(4, 7)
        current = 0
        for i in range(steps):
            # Variable step size - larger steps to cover ground faster
            step = random.randint(350, 700)
            current = min(current + step, scroll_height)
            await self.page.evaluate(f'window.scrollTo(0, {current})')
            await self.page.wait_for_timeout(random.randint(100, 250))

        # Shorter pause at the bottom (400-900ms instead of 800-1800ms)
        await self.page.wait_for_timeout(random.randint(400, 900))

        # Scroll back up slightly to trigger any "see more" type loading
        await self.page.evaluate(f'window.scrollTo(0, {random.randint(200, 600)})')
        await self.page.wait_for_timeout(random.randint(300, 700))
    
    async def _scrape_posts(self, limit: int, profile_vanity: str = "", on_progress=None) -> List[Post]:
        """Scrape posts with scrolling to load more."""
        posts: List[Post] = []
        scroll_count = 0
        max_scrolls = (limit // 3) + 4  # Slightly tighter cap
        prev_count = 0
        stall_count = 0
        
        while len(posts) < limit and scroll_count < max_scrolls:
            # Click all "...more" buttons to expand full post text
            await self._click_all_see_more_buttons()
            
            new_posts = await self._extract_posts_from_page(profile_vanity=profile_vanity)
            
            for post in new_posts:
                if post.urn and not any(p.urn == post.urn for p in posts):
                    posts.append(post)
                    if len(posts) >= limit:
                        break
            
            # Report progress
            if on_progress:
                pct = min(90, 35 + int((len(posts) / max(limit, 1)) * 55))
                await on_progress("posts_scraping", f"Extracted {len(posts)}/{limit} posts", pct)
            print(f"[posts-progress] {len(posts)}/{limit} posts scraped (scroll {scroll_count+1}/{max_scrolls})", file=sys.stderr)
            
            # Early exit if no new posts found after scrolling (stalled)
            if len(posts) == prev_count:
                stall_count += 1
                if stall_count >= 2:
                    print(f"[posts-progress] No new posts after {stall_count} scrolls, stopping early", file=sys.stderr)
                    break
            else:
                stall_count = 0
            prev_count = len(posts)
            
            if len(posts) < limit:
                await self._scroll_for_more_posts()
                scroll_count += 1
                logger.debug(f"Scroll {scroll_count}/{max_scrolls}, posts so far: {len(posts)}")
        
        return posts[:limit]
    
    async def _click_all_see_more_buttons(self) -> None:
        """Click all '...more' / 'see more' buttons on the page to expand truncated posts.
        Optimized: reduced cap from 30 to 15, tighter delays."""
        try:
            # LinkedIn uses multiple selectors for the "see more" button
            see_more_selectors = [
                'button.feed-shared-inline-show-more-text__see-more-less-toggle',
                'button[class*="see-more"]',
                'button:has-text("...more")',
                'button:has-text("see more")',
                'button:has-text("See more")',
            ]
            
            clicked_any = False
            for selector in see_more_selectors:
                try:
                    buttons = self.page.locator(selector)
                    count = await buttons.count()
                    
                    if count > 0:
                        logger.debug(f"Found {count} 'see more' buttons with selector: {selector}")
                        for i in range(min(count, 15)):  # Reduced cap from 30 to 15
                            try:
                                btn = buttons.nth(i)
                                if await btn.is_visible():
                                    await btn.click(timeout=1500)
                                    clicked_any = True
                                    # Tighter pause between clicks (0.1 - 0.4s)
                                    await asyncio.sleep(random.uniform(0.1, 0.4))
                            except Exception:
                                continue
                except Exception:
                    continue
            
            # Only wait if we actually clicked something (0.4 - 0.8s)
            if clicked_any:
                await self.page.wait_for_timeout(random.randint(400, 800))
            logger.debug("Finished clicking 'see more' buttons")
            
        except Exception as e:
            logger.debug(f"Error clicking see more buttons: {e}")
    
    async def _extract_posts_from_page(self, profile_vanity: str = "", target_urn: str = "") -> List[Post]:
        """Extract all visible posts from the current page state using JS.
        
        Uses a multi-strategy approach to handle LinkedIn's evolving DOM:
        1. Try data-urn elements scoped to the main feed
        2. Try feed-shared-update-v2 containers
        3. Try occludable-update containers
        4. Try data-id containers
        5. Fallback: find any containers with activity URNs in innerHTML
        
        Posts are filtered to ensure they belong to the target profile and
        are not navigation elements, sidebars, or recommendations.
        """

        # The JS extraction code — detailed debug version
        posts_data = await self.page.evaluate('''(args) => {
            let [profileVanity, targetUrn] = args;
            const posts = [];
            const seenUrns = new Set();
            const debug = {
                profileVanity: profileVanity,
                targetUrn: targetUrn,
                feedScopeFound: false,
                feedScopeClass: '',
                s1_candidates: 0, s1_added: 0,
                s2_candidates: 0, s2_added: 0,
                s3_candidates: 0, s3_added: 0,
                s4_candidates: 0, s4_added: 0,
                s5_urns_found: 0, s5_candidates: 0, s5_added: 0,
                s6_links: 0, s6_added: 0,
                rejected_nav: 0, rejected_author: 0,
                rejected_no_text: 0, rejected_invalid_urn: 0,
                sample_classes: [],
                total_found: 0
            };

            if (!targetUrn) {
                const currentUrl = window.location.href;
                const urlMatch = currentUrl.match(/activity:(\d{5,})/) || 
                                 currentUrl.match(/posts\/[^-]*-(\d{5,})/) ||
                                 currentUrl.match(/(\d{18,})/);
                if (urlMatch) targetUrn = 'urn:li:activity:' + urlMatch[1];
            }

            // ── Helpers ──────────────────────────────────────────────────────

            function isNavOrSidebar(el) {
                const cls = (el.className || '').toLowerCase();
                const tag = el.tagName ? el.tagName.toLowerCase() : '';
                const role = (el.getAttribute && el.getAttribute('role') || '').toLowerCase();
                if (cls.includes('artdeco-tab') || cls.includes('filter') ||
                    cls.includes('pill') || role === 'tab' || role === 'navigation' ||
                    cls.includes('sidebar') || cls.includes('aside') ||
                    tag === 'nav' || tag === 'aside' || tag === 'header' || tag === 'footer') {
                    return true;
                }
                const txt = (el.innerText || '').trim();
                const NAV_LABELS = ['Posts', 'Comments', 'Videos', 'Images', 'Documents', 'More', 'All activity', 'Articles', 'Show all', 'Home'];
                if (NAV_LABELS.includes(txt)) return true;
                if (txt.length > 0 && txt.length < 50 && (txt.includes('Recent') || txt.includes('Top'))) {
                    if (el.querySelector('select, button')) return true;
                }
                return false;
            }

            function isValidActivityUrn(urn) {
                if (!urn) return false;
                return /urn:li:(?:activity|ugcPost|share):\d{5,}/.test(urn);
            }

            function getPostAuthorVanity(el) {
                const actorSelectors = [
                    'a[data-tracking-control-name*="actor"]',
                    'a[class*="actor__image"]',
                    'a[class*="update-components-actor__meta-link"]',
                    'a[class*="feed-shared-actor__meta-link"]',
                    '.update-components-actor__container a',
                    '.feed-shared-actor__container a',
                    'a[class*="app-aware-link"][href*="/in/"]'
                ];
                for (const sel of actorSelectors) {
                    try {
                        const actorLink = el.querySelector(sel);
                        if (actorLink) {
                            const href = actorLink.getAttribute('href') || '';
                            const m = href.match(/\/in\/([^/?#]+)/);
                            if (m) return m[1].toLowerCase().replace(/\/$/, '');
                        }
                    } catch(e) {}
                }
                return null;
            }

            function findUrn(el) {
                let node = el;
                for (let i = 0; i < 6 && node; i++) {
                    const urn = node.getAttribute && node.getAttribute('data-urn');
                    if (urn && isValidActivityUrn(urn)) return urn;
                    const dataId = node.getAttribute && node.getAttribute('data-id');
                    if (dataId && isValidActivityUrn(dataId)) return dataId;
                    node = node.parentElement;
                }
                const inner = el.innerHTML || '';
                const m = inner.match(/urn:li:(?:activity|ugcPost|share):(\d{5,})/);
                if (m) return m[0];
                const links = el.querySelectorAll('a[href*="feed/update"]');
                for (const link of links) {
                    const href = link.getAttribute('href') || '';
                    const hm = href.match(/urn:li:activity:(\d{5,})/);
                    if (hm) return hm[0];
                }
                return null;
            }

            // Walk UP the DOM from a node to find a "post-like" container
            function walkUpToPostContainer(startEl, maxDepth) {
                let node = startEl;
                for (let i = 0; i < maxDepth && node && node !== document.body; i++) {
                    const cls = (node.className || '').toLowerCase();
                    // A post container usually has these signals
                    if (cls.includes('update') || cls.includes('occludable') ||
                        cls.includes('feed-shared') || cls.includes('scaffold-layout__main') ||
                        node.tagName === 'ARTICLE' ||
                        (node.getAttribute && node.getAttribute('data-urn')) ||
                        (node.getAttribute && node.getAttribute('data-id'))) {
                        return node;
                    }
                    node = node.parentElement;
                }
                // Return 4 levels up from start as best guess
                let up = startEl;
                for (let i = 0; i < 4 && up && up.parentElement; i++) up = up.parentElement;
                return up;
            }

            function extractPostData(el, urn) {
                if (!urn || !isValidActivityUrn(urn)) {
                    debug.rejected_invalid_urn++;
                    return null;
                }
                if (isNavOrSidebar(el)) {
                    debug.rejected_nav++;
                    return null;
                }

                // Author filtering — only skip if we CAN identify the author and it differs
                if (profileVanity) {
                    const author = getPostAuthorVanity(el);
                    if (author && author !== profileVanity.toLowerCase()) {
                        debug.rejected_author++;
                        return null;
                    }
                }

                // ── Text extraction ──────────────────────────────────────────
                let textSegments = [];
                const textSelectors = [
                    '.feed-shared-update-v2__description',
                    '.update-components-text',
                    '.feed-shared-text',
                    '[data-test-id="main-feed-activity-card__commentary"]',
                    '.break-words',
                    '.feed-shared-inline-show-more-text',
                    '.update-components-text__text-view',
                    '.feed-shared-text__text-view',
                    '.update-components-update-v2__commentary',
                    '[class*="update-v2__commentary"]',
                    '[class*="text-view"]',
                    'span[dir="ltr"]',
                    'p'
                ];

                for (const sel of textSelectors) {
                    try {
                        const containers = el.querySelectorAll(sel);
                        containers.forEach(c => {
                            const t = (c.innerText || '').trim();
                            if (t.length > 5 && !textSegments.some(prev => prev.includes(t) || t.includes(prev))) {
                                textSegments.push(t);
                            }
                        });
                    } catch(e) {}
                    if (textSegments.length > 0) break;
                }
                
                let text = textSegments.join('\\n\\n').trim();

                // Fallback: largest text block
                if (!text || text.length < 15) {
                    let maxLen = 0;
                    el.querySelectorAll('div, span, p').forEach(div => {
                        if (isNavOrSidebar(div)) return;
                        const t = (div.innerText || '').trim();
                        if (t.length > maxLen && t.length > 15 &&
                            !t.match(/^(Like|Comment|Repost|Send|Follow|Connect)/i) &&
                            !t.match(/^\\d+\\s*(reaction|comment|repost)/i) &&
                            !t.match(/^\\d+[hdwmy]\\s/) &&
                            !t.includes('followers') &&
                            !t.includes('connections')) {
                            const cls = (div.className || '') + (div.parentElement?.className || '');
                            if (!cls.includes('actor') && !cls.includes('header') &&
                                !cls.includes('cta') && !cls.includes('follow') &&
                                !cls.includes('action')) {
                                text = t;
                                maxLen = t.length;
                            }
                        }
                    });
                }

                if (!text || text.length < 15) {
                    debug.rejected_no_text++;
                    return null;
                }

                // ── Time ─────────────────────────────────────────────────────
                const timeEl = el.querySelector(
                    'time, [class*="actor__sub-description"], ' +
                    '[class*="update-components-actor__sub-description"], ' +
                    '[class*="feed-shared-actor__sub-description"], ' +
                    'span[class*="visually-hidden"]'
                );
                const timeText = timeEl ? (timeEl.getAttribute('datetime') || timeEl.innerText || '') : '';

                // ── Counts ────────────────────────────────────────────────────
                const reactionsEl = el.querySelector(
                    'button[aria-label*="reaction"], [class*="social-details-social-counts__reactions"], ' +
                    'span[class*="reactions-count"]'
                );
                const commentsEl = el.querySelector(
                    'button[aria-label*="comment"], [class*="social-details-social-counts__comments"]'
                );
                const repostsEl = el.querySelector(
                    'button[aria-label*="repost"], [class*="social-details-social-counts__reposts"]'
                );

                // ── Images ────────────────────────────────────────────────────
                const images = [];
                el.querySelectorAll('img[src*="media"], img[src*="dms.licdn"], img[src*="media-exp"]').forEach(img => {
                    if (img.src && !img.src.includes('profile') && !img.src.includes('logo') &&
                        !img.src.includes('static') && !img.src.includes('sprite') &&
                        !img.src.includes('ghost') && img.naturalWidth > 50) {
                        images.push(img.src);
                    }
                });

                // ── Video ─────────────────────────────────────────────────────
                const videoEl = el.querySelector('video source, video[src], [data-sources]');
                const videoUrl = videoEl ? (videoEl.getAttribute('src') || videoEl.getAttribute('data-src') || null) : null;

                // ── Article link ──────────────────────────────────────────────
                let articleUrl = null;
                const articleEl = el.querySelector(
                    'a[class*="feed-shared-article"], a[class*="update-components-article"], ' +
                    'a[data-tracking-control-name*="article"], a[class*="app-aware-link"][href*="http"]'
                );
                if (articleEl) {
                    const href = articleEl.getAttribute('href') || '';
                    if (href && !href.includes('linkedin.com/in/') && !href.includes('/feed/') &&
                        !href.includes('/in/')) {
                        articleUrl = href;
                    }
                }

                return {
                    urn, text: text.substring(0, 10000), timeText,
                    reactions: reactionsEl ? reactionsEl.innerText : '',
                    comments: commentsEl ? commentsEl.innerText : '',
                    reposts: repostsEl ? repostsEl.innerText : '',
                    images, videoUrl, articleUrl
                };
            }

            // ── Scope: prefer main feed area to avoid sidebars ───────────────
            // Order matters — pick the most specific first
            const FEED_SCOPE_SELECTORS = [
                '.scaffold-finite-scroll__content',
                'main .core-rail',
                '.profile-creator-shared-feed-update__container',
                '[class*="profile-creator-shared-feed"]',
                'main section',
                'main',
            ];
            let feedScope = null;
            for (const sel of FEED_SCOPE_SELECTORS) {
                feedScope = document.querySelector(sel);
                if (feedScope) {
                    debug.feedScopeFound = true;
                    debug.feedScopeClass = sel + ' → ' + (feedScope.className || '').substring(0, 60);
                    break;
                }
            }
            if (!feedScope) feedScope = document.body;

            // Collect sample classes for diagnostics
            const sampleEls = document.querySelectorAll('[class]');
            for (let i = 0; i < Math.min(sampleEls.length, 300); i++) {
                const cls = (sampleEls[i].className || '').toString().trim();
                if (cls.includes('update') || cls.includes('feed') || cls.includes('occludable') ||
                    cls.includes('scaffold') || cls.includes('activity')) {
                    if (!debug.sample_classes.includes(cls.substring(0, 80))) {
                        debug.sample_classes.push(cls.substring(0, 80));
                        if (debug.sample_classes.length >= 10) break;
                    }
                }
            }

            // == Strategy 1: data-urn with valid activity URN ==
            const s1Els = feedScope.querySelectorAll('[data-urn]');
            debug.s1_candidates = s1Els.length;
            s1Els.forEach(el => {
                const urn = el.getAttribute('data-urn');
                if (!urn || !isValidActivityUrn(urn) || seenUrns.has(urn)) return;
                seenUrns.add(urn);
                const data = extractPostData(el, urn);
                if (data) { posts.push(data); debug.s1_added++; }
            });

            // == Strategy 2: .feed-shared-update-v2 ==
            if (posts.length === 0) {
                const s2Els = feedScope.querySelectorAll('.feed-shared-update-v2');
                debug.s2_candidates = s2Els.length;
                s2Els.forEach(el => {
                    const urn = findUrn(el);
                    if (!urn || seenUrns.has(urn)) return;
                    seenUrns.add(urn);
                    const data = extractPostData(el, urn);
                    if (data) { posts.push(data); debug.s2_added++; }
                });
            }

            // == Strategy 3: .occludable-update ==
            if (posts.length === 0) {
                const s3Els = feedScope.querySelectorAll('.occludable-update');
                debug.s3_candidates = s3Els.length;
                s3Els.forEach(el => {
                    const urn = findUrn(el);
                    if (!urn || seenUrns.has(urn)) return;
                    seenUrns.add(urn);
                    const data = extractPostData(el, urn);
                    if (data) { posts.push(data); debug.s3_added++; }
                });
            }

            // == Strategy 4: [data-id] with valid activity URN ==
            if (posts.length === 0) {
                const s4Els = feedScope.querySelectorAll('[data-id]');
                debug.s4_candidates = s4Els.length;
                s4Els.forEach(el => {
                    const urn = el.getAttribute('data-id');
                    if (!urn || !isValidActivityUrn(urn) || seenUrns.has(urn)) return;
                    seenUrns.add(urn);
                    const data = extractPostData(el, urn);
                    if (data) { posts.push(data); debug.s4_added++; }
                });
            }

            // == Strategy 5: scan innerHTML for activity URNs, find enclosing container ==
            if (posts.length === 0) {
                const urnRegex = /urn:li:(?:activity|ugcPost|share):(\d{5,})/g;
                const allMatches = [...feedScope.innerHTML.matchAll(urnRegex)];
                let uniqueUrns = [...new Set(allMatches.map(m => m[0]))];
                
                if (targetUrn && !uniqueUrns.includes(targetUrn)) {
                    uniqueUrns.unshift(targetUrn);
                }
                
                debug.s5_urns_found = uniqueUrns.length;
                const CAND_SEL =
                    'article, div[class*="update"], div[class*="occludable"], div[class*="feed-shared"], ' +
                    'div[class*="activity"], li, div[class*="entity"], div[class*="main-content"]';
                const s5Cands = [...feedScope.querySelectorAll(CAND_SEL)];
                
                if (s5Cands.length === 0 && feedScope.tagName === 'MAIN') {
                    s5Cands.push(feedScope);
                }

                debug.s5_candidates = s5Cands.length;
                for (const urn of uniqueUrns) {
                    if (seenUrns.has(urn)) continue;
                    seenUrns.add(urn);
                    for (const cand of s5Cands) {
                        const html = (cand.innerHTML || '');
                        const hasUrnAttr = cand.getAttribute && (cand.getAttribute('data-urn') === urn || cand.getAttribute('data-activity-urn') === urn);
                        if (html.includes(urn) || hasUrnAttr) {
                            const data = extractPostData(cand, urn);
                            if (data) { posts.push(data); debug.s5_added++; break; }
                        }
                    }
                }
            }

            // == Strategy 6: find <a href="...feed/update/..."> links, walk UP to container ==
            if (posts.length === 0) {
                const feedLinks = document.querySelectorAll('a[href*="feed/update/"]');
                debug.s6_links = feedLinks.length;
                for (const link of feedLinks) {
                    const href = link.getAttribute('href') || '';
                    const m = href.match(/urn:li:activity:(\d{5,})/);
                    if (!m) continue;
                    const urn = 'urn:li:activity:' + m[1];
                    if (seenUrns.has(urn)) continue;
                    seenUrns.add(urn);
                    const container = walkUpToPostContainer(link, 10);
                    if (container) {
                        const data = extractPostData(container, urn);
                        if (data) { posts.push(data); debug.s6_added++; }
                    }
                }
            }

            // == Strategy 7: Fallback for direct post pages (take largest content from main) ==
            if (posts.length === 0 && window.location.href.includes('/posts/')) {
                debug.s7_direct_post = true;
                const mainEl = document.querySelector('main');
                if (mainEl) {
                    const data = extractPostData(mainEl, targetUrn || "urn:li:activity:scraped");
                    if (data) {
                        posts.push(data);
                        debug.s7_added = 1;
                    }
                }
            }

            debug.total_found = posts.length;
            return { posts, debug };
        }''', [profile_vanity, target_urn])

        debug_info = posts_data.get('debug', {})
        raw_posts = posts_data.get('posts', [])
        print(
            f"[posts-debug] Extraction result: "
            f"scope='{debug_info.get('feedScopeClass', 'N/A')}' | "
            f"S1={debug_info.get('s1_candidates', 0)}cands/{debug_info.get('s1_added', 0)}added | "
            f"S2={debug_info.get('s2_candidates', 0)}cands/{debug_info.get('s2_added', 0)}added | "
            f"S3={debug_info.get('s3_candidates', 0)}cands/{debug_info.get('s3_added', 0)}added | "
            f"S4={debug_info.get('s4_candidates', 0)}cands/{debug_info.get('s4_added', 0)}added | "
            f"S5={debug_info.get('s5_urns_found', 0)}u/{debug_info.get('s5_candidates', 0)}c/{debug_info.get('s5_added', 0)}a | "
            f"S6={debug_info.get('s6_links', 0)}l/{debug_info.get('s6_added', 0)}a | "
            f"S7={debug_info.get('s7_added', 0)}a | "
            f"rejected: nav={debug_info.get('rejected_nav', 0)} author={debug_info.get('rejected_author', 0)} "
            f"no_text={debug_info.get('rejected_no_text', 0)} invalid_urn={debug_info.get('rejected_invalid_urn', 0)} | "
            f"total={len(raw_posts)}",
            file=sys.stderr
        )
        if debug_info.get('sample_classes'):
            print(f"[posts-debug] sample classes: {debug_info['sample_classes']}", file=sys.stderr)

        result: List[Post] = []
        for data in raw_posts:
            urn = data.get('urn', '')
            # Normalise URN for URL construction
            activity_id = ''
            m = re.search(r'(?:activity|ugcPost|share):(\d+)', urn)
            if m:
                activity_id = m.group(1)
                canonical_urn = f"urn:li:activity:{activity_id}"
            else:
                canonical_urn = urn
                activity_id = urn.split(':')[-1] if ':' in urn else urn

            post = Post(
                linkedin_url=f"https://www.linkedin.com/feed/update/{canonical_urn}/" if activity_id else "",
                urn=canonical_urn,
                text=data['text'],
                posted_date=self._extract_time_from_text(data.get('timeText', '')),
                reactions_count=self._parse_count(data.get('reactions', '')),
                comments_count=self._parse_count(data.get('comments', '')),
                reposts_count=self._parse_count(data.get('reposts', '')),
                image_urls=data.get('images', []),
                video_url=data.get('videoUrl'),
                article_url=data.get('articleUrl')
            )
            result.append(post)

        return result
    
    def _extract_time_from_text(self, text: str) -> Optional[str]:
        """Extract relative time from time element text."""
        if not text:
            return None
        # Match patterns like "2d", "1w", "3 hours ago", "1 month ago"
        match = re.search(
            r'(\d+[hdwmy]|\d+\s*(?:hour|day|week|month|year)s?\s*ago)',
            text, re.IGNORECASE
        )
        if match:
            return match.group(1).strip()
        # Fall back to first part before bullet
        parts = text.split('\u2022')
        if parts:
            return parts[0].strip()
        return None
    
    def _parse_count(self, text: str) -> Optional[int]:
        """Parse engagement count from text like '1,234' or '5 comments'."""
        if not text:
            return None
        try:
            numbers = re.findall(r'[\d,]+', text.replace(',', ''))
            if numbers:
                return int(numbers[0])
        except:
            pass
        return None
    
    async def _scroll_for_more_posts(self) -> None:
        """Scroll down to load more posts with human-like timing.
        Optimized: tighter wait ranges."""
        try:
            # Occasionally use keyboard End, sometimes scroll by pixels - vary the approach
            if random.random() < 0.5:
                await self.page.keyboard.press('End')
            else:
                scroll_by = random.randint(800, 1500)
                await self.page.evaluate(f'window.scrollBy(0, {scroll_by})')

            # Tighter wait: 0.8 - 2.0s (was 1.5 - 3.5s)
            await self.page.wait_for_timeout(random.randint(800, 2000))

            # Also try clicking "Show more" buttons if present
            try:
                show_more = self.page.locator('button:has-text("Show more results"), button:has-text("Show more")')
                if await show_more.count() > 0:
                    await show_more.first.click()
                    await self.page.wait_for_timeout(random.randint(800, 1500))
            except:
                pass
        except Exception as e:
            logger.debug(f"Error scrolling: {e}")
