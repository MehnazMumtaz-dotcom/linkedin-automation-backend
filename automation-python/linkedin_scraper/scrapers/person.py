"""Person/Profile scraper for LinkedIn."""

import logging
import sys
from typing import Optional
from urllib.parse import urljoin
from playwright.async_api import Page

from .base import BaseScraper
from ..models import Person, Experience, Education, Accomplishment, Interest, Contact
from ..callbacks import ProgressCallback
from ..core.exceptions import ScrapingError

logger = logging.getLogger(__name__)


class PersonScraper(BaseScraper):
    """Async scraper for LinkedIn person profiles."""

    def __init__(self, page: Page, callback: Optional[ProgressCallback] = None):
        """
        Initialize person scraper.

        Args:
            page: Playwright page object
            callback: Progress callback
        """
        super().__init__(page, callback)

    async def scrape(self, linkedin_url: str) -> Person:
        """
        Scrape a LinkedIn person profile.

        Args:
            linkedin_url: LinkedIn profile URL

        Returns:
            Person object with all scraped data

        Raises:
            AuthenticationError: If not logged in
            ScrapingError: If scraping fails
        """
        await self.callback.on_start("person", linkedin_url)

        # Extract vanity name for Voyager API calls
        vanity_name = linkedin_url.rstrip('/').split('/')[-1]

        # ── Network interception: capture LinkedIn's own internal API responses ──
        # LinkedIn's React app now uses GraphQL (/voyager/api/graphql) for ALL
        # profile data (positions, educations, skills). We capture every graphql
        # response and also the old REST endpoints as a fallback.
        captured_api: dict = {}
        captured_urls: list = []
        captured_graphql: list = []

        async def _on_api_response(response) -> None:
            url = response.url
            if "linkedin.com/voyager/api" not in url:
                return
            path = url.split("?")[0].split("linkedin.com")[-1]
            captured_urls.append(path)
            try:
                # NEW: capture ALL graphql responses — profile data comes through here now
                if "graphql" in path:
                    body = await response.json()
                    captured_graphql.append(body)
                    return
                # Old REST endpoints (may still work on some accounts)
                if any(k in url for k in ("PositionGroup", "position", "Position", "profilePositions")):
                    captured_api.setdefault("positions_raw", [])
                    captured_api["positions_raw"].append(await response.json())
                elif any(k in url for k in ("Education", "education", "profileEducations")):
                    captured_api.setdefault("educations_raw", [])
                    captured_api["educations_raw"].append(await response.json())
                elif any(k in url for k in ("Skill", "skill", "profileSkills")):
                    captured_api.setdefault("skills_raw", [])
                    captured_api["skills_raw"].append(await response.json())
                elif "profileView" in url or "fullProfile" in url or "FullProfile" in url:
                    captured_api["full_profile"] = await response.json()
            except Exception:
                pass

        self.page.on("response", _on_api_response)

        try:
            # Navigate to profile — LinkedIn's JS will fire the API calls we intercept
            await self.navigate_and_wait(linkedin_url)
            await self.callback.on_progress("Navigated to profile", 10)

            # Now check if logged in
            await self.ensure_logged_in()

            # Wait for main content AND let background API calls settle
            await self.page.wait_for_selector("main", timeout=10000)
            try:
                await self.page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass
            await self.wait_and_focus(1.0)
            print(f"[person-scraper][intercept] captured API urls: {captured_urls}", file=sys.stderr)

            # Get name and location
            name, location = await self._get_name_and_location()
            await self.callback.on_progress(f"Got name: {name}", 10)

            # Get headline
            headline = None
            try:
                headline = await self._get_headline()
            except: pass

            # Get profile picture
            profile_picture_url = None
            try:
                profile_picture_url = await self._get_profile_picture()
            except: pass

            # Check open to work
            open_to_work = False
            try:
                open_to_work = await self._check_open_to_work()
            except: pass

            # Get about
            about = None
            try:
                about = await self._get_about()
                await self.callback.on_progress("Got about section", 15)
            except: pass

            # Get current company from top card (while still on main profile page)
            current_company = None
            try:
                current_company = await self._get_current_company_from_header()
            except: pass

            # Scroll so LinkedIn's JS lazy-loads all section API calls
            try:
                await self.scroll_page_to_half()
                await self.scroll_page_to_bottom(pause_time=0.6, max_scrolls=8)
                await self.wait_and_focus(1.5)
                try:
                    await self.page.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    pass
            except: pass

            print(f"[person-scraper][intercept] final captured urls: {captured_urls}", file=sys.stderr)
            print(f"[person-scraper][intercept] keys: pos={len(captured_api.get('positions_raw',[]))}, edu={len(captured_api.get('educations_raw',[]))}, skill={len(captured_api.get('skills_raw',[]))}, graphql={len(captured_graphql)}, full={'yes' if 'full_profile' in captured_api else 'no'}", file=sys.stderr)

            experiences: list[Experience] = []
            educations: list[Education] = []
            skills: list[str] = []

            # ── Strategy 1a: parse intercepted REST network responses ─────────────
            if captured_api:
                await self.callback.on_progress("Parsing intercepted REST data...", 22)
                experiences, educations, skills = self._parse_intercepted_api(captured_api)
                print(f"[person-scraper][intercept] REST parsed: exp={len(experiences)}, edu={len(educations)}, skills={len(skills)}", file=sys.stderr)

            # ── Strategy 1b: parse intercepted GraphQL responses (modern LinkedIn) ─
            if not experiences and not educations:
                if captured_graphql:
                    await self.callback.on_progress("Parsing GraphQL responses...", 24)
                    gql_exp, gql_edu, gql_skills = self._parse_graphql_responses(captured_graphql)
                    print(f"[person-scraper][intercept] GraphQL parsed: exp={len(gql_exp)}, edu={len(gql_edu)}, skills={len(gql_skills)}", file=sys.stderr)
                    if gql_exp:
                        experiences = gql_exp
                    if gql_edu:
                        educations = gql_edu
                    if gql_skills and not skills:
                        skills = gql_skills

            # ── Strategy 2: manual Voyager API call (if interception got nothing) ─
            if not experiences and not educations:
                await self.callback.on_progress("Trying Voyager API...", 26)
                voyager_data = await self._scrape_profile_via_voyager(vanity_name)
                if voyager_data.get("experience") or voyager_data.get("education") or voyager_data.get("skills"):
                    for e in voyager_data.get("experience", []):
                        if e.get("title") or e.get("company"):
                            experiences.append(Experience(
                                position_title=e.get("title") or None,
                                institution_name=e.get("company") or None,
                                from_date=e.get("from") or None,
                                to_date=e.get("to") or None,
                                location=e.get("location") or None,
                                description=e.get("description") or None,
                            ))
                    for edu in voyager_data.get("education", []):
                        school = edu.get("school", "")
                        if school:
                            degree_parts = [edu.get("degree"), edu.get("fieldOfStudy")]
                            degree = " - ".join(p for p in degree_parts if p) or None
                            educations.append(Education(
                                institution_name=school,
                                degree=degree,
                                from_date=edu.get("from") or None,
                                to_date=edu.get("to") or None,
                            ))
                    if not skills:
                        skills = [s for s in voyager_data.get("skills", []) if s and len(s) > 1]

            # ── Strategy 2b: scrape detail sub-pages directly via HTML ─────────────
            # Navigate to /details/experience/ and /details/education/ pages
            if not experiences:
                await self.callback.on_progress("Scraping experience detail page...", 28)
                try:
                    experiences = await self._get_experiences(linkedin_url)
                    print(f"[person-scraper] detail page exp: {len(experiences)}", file=sys.stderr)
                except Exception as e:
                    print(f"[person-scraper] detail page exp error: {e}", file=sys.stderr)

            if not educations:
                await self.callback.on_progress("Scraping education detail page...", 30)
                try:
                    educations = await self._get_educations(linkedin_url)
                    print(f"[person-scraper] detail page edu: {len(educations)}", file=sys.stderr)
                except Exception as e:
                    print(f"[person-scraper] detail page edu error: {e}", file=sys.stderr)

            # ── Strategy 3: HTML extraction from main profile page ────────────────
            if not experiences and not educations and not skills:
                # Navigate back to main profile, scroll to load lazy sections
                try:
                    await self.navigate_and_wait(linkedin_url)
                    await self.page.wait_for_selector("main", timeout=8000)
                    await self.wait_and_focus(2)
                    await self.scroll_page_to_half()
                    await self.scroll_page_to_bottom(pause_time=0.7, max_scrolls=8)
                    await self.wait_and_focus(2)
                    try:
                        await self.page.wait_for_load_state("networkidle", timeout=6000)
                    except Exception:
                        pass
                except Exception:
                    pass
                print(f"[person-scraper] All API strategies failed, trying main page HTML. bodyLen={await self.page.evaluate('() => document.body.innerHTML.length')}", file=sys.stderr)
                await self.callback.on_progress("Extracting from page HTML...", 32)
                sections_data = {}
                try:
                    sections_data = await self._extract_all_sections_from_main_page()
                except Exception as e:
                    print(f"[person-scraper] HTML extraction error: {e}", file=sys.stderr)
                for item in sections_data.get("experience", []):
                    exp = self._parse_experience_from_texts(item.get("texts", []), item.get("href"))
                    if exp:
                        experiences.append(exp)
                for item in sections_data.get("education", []):
                    edu = self._parse_education_from_texts(item.get("texts", []), item.get("href"))
                    if edu:
                        educations.append(edu)
                for item in sections_data.get("skills", []):
                    texts = item.get("texts", [])
                    if texts and len(texts[0]) > 1:
                        sn = texts[0].strip()
                        if sn.lower() not in ("skills", "add", "show all", "endorse"):
                            skills.append(sn)

            if not skills:
                try:
                    skills = await self._get_skills(linkedin_url)
                except Exception:
                    pass

            await self.callback.on_progress(f"Sections: {len(experiences)} exp, {len(educations)} edu, {len(skills)} skills", 40)

            # Get interests
            interests = []
            try:
                await self.callback.on_progress("Scraping interests...", 58)
                interests = await self._get_interests(linkedin_url)
                await self.callback.on_progress(f"Got {len(interests)} interests", 65)
            except: pass

            # Get accomplishments
            accomplishments = []
            try:
                await self.callback.on_progress("Scraping certifications & accomplishments...", 68)
                accomplishments = await self._get_accomplishments(linkedin_url)
                await self.callback.on_progress(f"Got {len(accomplishments)} accomplishments", 85)
            except: pass

            # Get contacts
            contacts = []
            try:
                await self.callback.on_progress("Scraping contact info...", 88)
                contacts = await self._get_contacts(linkedin_url)
                await self.callback.on_progress(f"Got {len(contacts)} contacts", 95)
            except: pass

            # Derive current_company from experiences if header extraction missed it
            if not current_company and experiences:
                for exp in experiences:
                    if exp.to_date and exp.to_date.lower() in ("present", ""):
                        current_company = exp.institution_name
                        break
                if not current_company:
                    current_company = experiences[0].institution_name

            person = Person(
                linkedin_url=linkedin_url,
                name=name,
                headline=headline,
                location=location,
                about=about,
                current_company=current_company,
                open_to_work=open_to_work,
                profile_picture_url=profile_picture_url,
                skills=skills,
                experiences=experiences,
                educations=educations,
                interests=interests,
                accomplishments=accomplishments,
                contacts=contacts,
            )

            await self.callback.on_progress("Scraping complete", 100)
            await self.callback.on_complete("person", person)

            return person

        except Exception as e:
            await self.callback.on_error(e)
            raise ScrapingError(f"Failed to scrape person profile: {e}")

    async def _get_name_and_location(self) -> tuple[str, Optional[str]]:
        """Extract name and location from profile."""
        try:
            # Try multiple selectors for name - LinkedIn's layout varies
            name = None
            name_selectors = [
                "h1",
                "h1.text-heading-xlarge",
                ".pv-top-card--list .text-heading-xlarge",
                ".pv-text-details__left-panel h1",
                "section.artdeco-card h1",
                ".pv-top-card-section__name",
                '[data-anonymize="person-name"]',
                "div.ph5.pb5 > div.mt2 > div > ul > li:first-child", # Older layout
            ]
            for selector in name_selectors:
                extracted = await self.safe_extract_text(selector, default="", timeout=3000)
                extracted = extracted.strip()
                if extracted and extracted != "Unknown" and len(extracted) > 1:
                    name = extracted
                    break
            
            # Fallback: try page title - format is "Name - Title | LinkedIn"
            if not name or name == "Unknown":
                try:
                    page_title = await self.page.title()
                    if page_title and "LinkedIn" in page_title:
                        # format is often "Name - Headline | LinkedIn" or "Name | LinkedIn"
                        cleaned = page_title.split("|")[0].strip()
                        if " - " in cleaned:
                            candidate = cleaned.split(" - ")[0].strip()
                        else:
                            candidate = cleaned
                        
                        if candidate and len(candidate) > 1 and "LinkedIn" not in candidate:
                            name = candidate
                            logger.info(f"Got name from page title: {name}")
                except Exception as e:
                    logger.debug(f"Error getting name from page title: {e}")
            
            # Fallback: try og:title meta tag
            if not name or name == "Unknown":
                try:
                    og_title = await self.page.evaluate('''() => {
                        const meta = document.querySelector('meta[property="og:title"]');
                        return meta ? meta.getAttribute('content') : null;
                    }''')
                    if og_title:
                        import re
                        cleaned = re.sub(r'\s*\|\s*LinkedIn\s*$', '', og_title).strip()
                        if cleaned:
                            if " - " in cleaned:
                                parts = cleaned.split(" - ")
                                candidate = parts[0].strip()
                            else:
                                candidate = cleaned
                            if candidate and len(candidate) > 1 and candidate != "LinkedIn":
                                name = candidate
                except Exception as e:
                    logger.debug(f"Error getting name from og:title: {e}")
            
            # If still Unknown/None, return None to allow backend to fallback to vanity name correctly
            if name == "Unknown":
                 name = None
            
            # Try multiple selectors for location
            location_selectors = [
                ".text-body-small.inline.t-black--light.break-words",
                ".pv-top-card--list .text-body-small",
                '[data-anonymize="location"]',
                ".pv-text-details__left-panel .text-body-small",
            ]
            location = ""
            for selector in location_selectors:
                extracted = await self.safe_extract_text(selector, default="", timeout=2000)
                extracted = extracted.strip()
                if extracted:
                    location = extracted
                    break
            
            return name, location if location else None
        except Exception as e:
            logger.warning(f"Error getting name/location: {e}")
            return None, None

    async def _get_headline(self) -> Optional[str]:
        """Extract the actual LinkedIn headline (text directly below name)."""
        headline_selectors = [
            "h1 ~ div.text-body-medium",
            "h1.text-heading-xlarge + div.text-body-medium",
            ".text-body-medium.break-words",
            ".pv-top-card--list .text-body-medium",
            ".pv-text-details__left-panel .text-body-medium",
            ".pv-text-details__left-panel > div:nth-child(2)",
            '[data-generated-suggestion-target]',
            "h2.text-body-medium",
        ]
        for selector in headline_selectors:
            try:
                text = await self.safe_extract_text(selector, default="", timeout=2000)
                text = text.strip()
                if text and len(text) > 2 and "LinkedIn" not in text:
                    return text
            except: pass
        
        # Last resort: try to find the second major heading in the top card
        try:
            text = await self.page.evaluate('''() => {
                const name = document.querySelector('h1')?.innerText;
                const allHeadings = Array.from(document.querySelectorAll('h1, h2, div.text-body-medium, div.text-body-small'));
                for (const el of allHeadings) {
                    const t = el.innerText.trim();
                    if (t && t !== name && t.length > 5 && t.length < 300 && !t.includes('LinkedIn')) return t;
                }
                return null;
            }''')
            if text: return text.strip()
        except: pass

        return None

    async def _get_profile_picture(self) -> Optional[str]:
        """Extract profile picture URL."""
        try:
            src = await self.page.evaluate('''() => {
                // Priority order: most specific profile photo selectors first
                const selectors = [
                    'img.pv-top-card-profile-picture__image',
                    '.pv-top-card-profile-picture img',
                    '.pv-top-card-profile-picture__container img',
                    '.pv-member-photo__photo img',
                    '.profile-photo-edit__preview',
                    'img.pv-top-card__photo',
                    '.presence-entity__image',
                    'img[data-delayed-url*="dms/image"]',
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el && el.src && el.src.startsWith('http')) return el.src;
                }
                // Fallback: any LinkedIn CDN image large enough to be a profile photo
                const allImgs = document.querySelectorAll('img[src*="media.licdn.com/dms/image"]');
                for (const img of allImgs) {
                    if ((img.naturalWidth || img.width || 0) > 50) return img.src;
                }
                // Last resort: legacy profile-displayphoto pattern
                const legacy = document.querySelector('img[src*="profile-displayphoto"]');
                return legacy ? legacy.src : null;
            }''')
            if src:
                return src
        except Exception as e:
            logger.debug(f"Error getting profile picture: {e}")
        return None

    async def _get_current_company_from_header(self) -> Optional[str]:
        """Extract current company name from the profile top card company links."""
        try:
            return await self.page.evaluate('''() => {
                // Look for company links visible in the top card section
                const topCardSelectors = [
                    '.pv-top-card',
                    '.ph5.pb5',
                    'section.artdeco-card:first-of-type',
                    'main > div:first-child',
                ];
                for (const sel of topCardSelectors) {
                    const card = document.querySelector(sel);
                    if (!card) continue;
                    const links = card.querySelectorAll('a[href*="/company/"]');
                    for (const link of links) {
                        const text = (link.innerText || link.textContent || '').trim();
                        if (text && text.length > 1 && text.length < 100) return text;
                    }
                }
                // Fallback: any company link near the top of the page
                const allCompanyLinks = document.querySelectorAll('a[href*="/company/"]');
                for (const link of Array.from(allCompanyLinks).slice(0, 5)) {
                    const text = (link.innerText || link.textContent || '').trim();
                    if (text && text.length > 1 && text.length < 100) return text;
                }
                return null;
            }''')
        except Exception:
            return None

    async def _check_open_to_work(self) -> bool:
        """Check if profile has open to work badge."""
        try:
            # Look for open to work indicator
            img_title = await self.get_attribute_safe(
                ".pv-top-card-profile-picture img", "title", default=""
            )
            return "#OPEN_TO_WORK" in img_title.upper()
        except:
            return False

    async def _get_about(self) -> Optional[str]:
        """Extract about section."""
        try:
            about_text = await self.page.evaluate(r'''() => {
                // Strategy 0: Look for "About" card specifically
                const aboutCard = document.querySelector('section[id*="about"], [data-view-name="profile-card"]:has(h2:contains("About"))');
                
                // Strategy 1: profile-card with "About" heading
                const cards = document.querySelectorAll('[data-view-name="profile-card"]');
                for (const card of cards) {
                    const headings = card.querySelectorAll('h2, h3, span');
                    let isAbout = false;
                    for (const h of headings) {
                        if ((h.textContent || '').trim().toLowerCase() === 'about') {
                            isAbout = true;
                            break;
                        }
                    }
                    
                    if (isAbout) {
                        // Look for "show more" and click it if possible (though evaluate is read-only usually, we just find the text)
                        const showMore = card.querySelector('.inline-show-more-text__button, [aria-label*="see more"]');
                        // Capture all text in the readable span
                        const mainTextEl = card.querySelector('.inline-show-more-text, .pv-about-section__summary-text, .display-flex.flex-column.full-width');
                        if (mainTextEl) {
                            return (mainTextEl.innerText || '').trim();
                        }

                        const spans = card.querySelectorAll('span[aria-hidden="true"]');
                        let collected = [];
                        for (const span of spans) {
                            const text = (span.textContent || '').trim();
                            if (text && text !== 'About' && text.length > 5) {
                                collected.push(text);
                            }
                        }
                        if (collected.length > 0) return collected.join('\n\n');
                    }
                }

                // Strategy 2: generic patterns
                const summary = document.querySelector('.pv-about-section__summary-text, #about ~ div, .inline-show-more-text');
                if (summary) return (summary.innerText || '').trim();

                return null;
            }''')
            if about_text:
                # Clean up "see more" or "show more" artifacts
                import re
                cleaned = re.sub(r'\.\.\.see more$', '', about_text, flags=re.IGNORECASE).strip()
                cleaned = re.sub(r'\s*see more$', '', cleaned, flags=re.IGNORECASE).strip()
                return cleaned if len(cleaned) > 10 else None
            return None
        except Exception as e:
            logger.debug(f"Error getting about section: {e}")
            return None

    def _parse_intercepted_api(self, captured: dict) -> tuple[list[Experience], list[Education], list[str]]:
        """
        Parse the raw JSON data intercepted from LinkedIn's own internal API calls
        while it loaded the profile page. Handles both old (normalized+json) and
        new (elements array) response formats.
        """
        experiences: list[Experience] = []
        educations: list[Education] = []
        skills: list[str] = []
        seen_skills: set[str] = set()

        def parse_date(d: dict | None) -> str | None:
            if not d:
                return None
            month = d.get("month", "")
            year = d.get("year", "")
            if month and year:
                return f"{month}/{year}"
            return str(year) if year else None

        # ── Positions ────────────────────────────────────────────────────────────
        for raw in captured.get("positions_raw", []):
            print(f"[intercept][pos] keys={list(raw.keys())[:8]}", file=sys.stderr)
            items = []
            # New format: {elements: [...]}
            if "elements" in raw:
                for group in raw["elements"]:
                    company = group.get("companyName") or (group.get("company") or {}).get("name", "")
                    sub = group.get("positions", {}).get("elements") or [group]
                    for pos in sub:
                        items.append({
                            "title": pos.get("title", ""),
                            "company": pos.get("companyName") or company,
                            "from": parse_date(pos.get("dateRange", {}).get("start") or (pos.get("timePeriod") or {}).get("startDate")),
                            "to": parse_date(pos.get("dateRange", {}).get("end") or (pos.get("timePeriod") or {}).get("endDate")) or "Present",
                            "location": pos.get("locationName") or pos.get("geoLocationName", ""),
                            "description": pos.get("description", ""),
                        })
            # Old normalized format: {data: {...}, included: [...]}
            if "included" in raw:
                for item in raw["included"]:
                    t = item.get("$type", "")
                    if "Position" in t and "Group" not in t:
                        tp = item.get("timePeriod", {})
                        items.append({
                            "title": item.get("title", ""),
                            "company": item.get("companyName", ""),
                            "from": parse_date(tp.get("startDate")),
                            "to": parse_date(tp.get("endDate")) or "Present",
                            "location": item.get("locationName", ""),
                            "description": item.get("description", ""),
                        })

            for e in items:
                if e.get("title") or e.get("company"):
                    experiences.append(Experience(
                        position_title=e["title"] or None,
                        institution_name=e["company"] or None,
                        from_date=e.get("from"),
                        to_date=e.get("to"),
                        location=e.get("location") or None,
                        description=e.get("description") or None,
                    ))

        # ── Educations ────────────────────────────────────────────────────────────
        for raw in captured.get("educations_raw", []):
            print(f"[intercept][edu] keys={list(raw.keys())[:8]}", file=sys.stderr)
            items = raw.get("elements") or raw.get("included") or []
            for edu in items:
                t = edu.get("$type", "")
                if items is raw.get("included") and "Education" not in t:
                    continue
                school = edu.get("schoolName") or (edu.get("school") or {}).get("name", "")
                if not school:
                    continue
                dr = edu.get("dateRange") or edu.get("timePeriod") or {}
                educations.append(Education(
                    institution_name=school,
                    degree=edu.get("degreeName") or None,
                    from_date=str(dr.get("start", {}).get("year", "") or dr.get("startDate", {}).get("year", "") or "") or None,
                    to_date=str(dr.get("end", {}).get("year", "") or dr.get("endDate", {}).get("year", "") or "") or None,
                    description=(edu.get("fieldOfStudy") or "") + (" · " + edu.get("grade") if edu.get("grade") else "") or None,
                ))

        # ── Skills ────────────────────────────────────────────────────────────────
        for raw in captured.get("skills_raw", []):
            print(f"[intercept][skill] keys={list(raw.keys())[:8]}", file=sys.stderr)
            items = raw.get("elements") or raw.get("included") or []
            for sk in items:
                name = sk.get("name") or (sk.get("skill") or {}).get("name", "")
                if name and name not in seen_skills and len(name) > 1:
                    seen_skills.add(name)
                    skills.append(name)

        # ── Full profile fallback ─────────────────────────────────────────────────
        if not experiences and not educations and "full_profile" in captured:
            fp = captured["full_profile"]
            for item in fp.get("included", []):
                t = item.get("$type", "")
                if "Position" in t and "Group" not in t:
                    tp = item.get("timePeriod", {})
                    experiences.append(Experience(
                        position_title=item.get("title"),
                        institution_name=item.get("companyName"),
                        from_date=parse_date(tp.get("startDate")),
                        to_date=parse_date(tp.get("endDate")) or "Present",
                        location=item.get("locationName"),
                    ))
                elif "Education" in t:
                    school = item.get("schoolName", "")
                    if school:
                        educations.append(Education(institution_name=school, degree=item.get("degreeName")))

        return experiences, educations, skills

    def _parse_graphql_responses(self, graphql_responses: list) -> tuple[list[Experience], list[Education], list[str]]:
        """
        Parse LinkedIn GraphQL responses to extract experience, education, and skills.
        LinkedIn's modern profile page loads all data via /voyager/api/graphql.
        We deep-scan every response body for known entity shapes (duck typing).
        """
        experiences: list[Experience] = []
        educations: list[Education] = []
        skills: list[str] = []
        seen_exp: set[str] = set()
        seen_edu: set[str] = set()
        seen_skill: set[str] = set()

        def parse_date(d) -> str | None:
            if not d or not isinstance(d, dict):
                return None
            month = d.get("month", "")
            year = d.get("year", "")
            if month and year:
                return f"{month}/{year}"
            return str(year) if year else None

        def walk(obj, depth=0):
            if depth > 12:
                return
            if isinstance(obj, dict):
                yield obj
                for v in obj.values():
                    yield from walk(v, depth + 1)
            elif isinstance(obj, list):
                for item in obj:
                    yield from walk(item, depth + 1)

        # Debug: dump graphql responses to a file to analyze their structure
        try:
            with open("graphql_debug.json", "w") as f:
                import json
                json.dump(graphql_responses, f)
        except Exception:
            pass

        for response in graphql_responses:
            for node in walk(response):
                if not isinstance(node, dict):
                    continue

                # ── Experience / Position (Duck Typing) ────────────────────────
                # A position node usually has 'title' or 'positionTitle' and a 'companyName'
                title = node.get("title") or node.get("positionTitle") or ""
                _co = node.get("company")
                _ct = node.get("companyTag")
                company = (
                    node.get("companyName")
                    or ((_co.get("name") if isinstance(_co, dict) else None) or "")
                    or ((_ct.get("name") if isinstance(_ct, dict) else None) or "")
                    or ""
                )
                if title and company:
                    _dr = node.get("dateRange") or node.get("timePeriod")
                    dr = _dr if isinstance(_dr, dict) else {}
                    from_d = parse_date(dr.get("start") or dr.get("startDate"))
                    to_d = parse_date(dr.get("end") or dr.get("endDate")) or "Present"
                    loc = node.get("locationName") or node.get("geoLocationName") or ""
                    desc = node.get("description") or ""
                    key = f"{title}|{company}"
                    if key not in seen_exp:
                        seen_exp.add(key)
                        experiences.append(Experience(
                            position_title=title or None,
                            institution_name=company or None,
                            from_date=from_d,
                            to_date=to_d,
                            location=loc or None,
                            description=desc or None,
                        ))

                # ── Education (Duck Typing) ────────────────────────────────────
                # An education node has 'schoolName' and optionally 'degreeName'
                _sc = node.get("school")
                school = (
                    node.get("schoolName")
                    or ((_sc.get("name") if isinstance(_sc, dict) else None) or "")
                    or ""
                )
                if school and (node.get("degreeName") or node.get("fieldOfStudy") or node.get("timePeriod") or node.get("dateRange")):
                    degree = node.get("degreeName") or node.get("degree") or None
                    field = node.get("fieldOfStudy") or None
                    _dr = node.get("dateRange") or node.get("timePeriod")
                    dr = _dr if isinstance(_dr, dict) else {}
                    from_y = str((dr.get("start") or dr.get("startDate") or {}).get("year", "") or "")
                    to_y = str((dr.get("end") or dr.get("endDate") or {}).get("year", "") or "")
                    
                    # Prevent False Positives (like "New York University" as a company)
                    if not title or not company:
                        key = f"{school}|{degree}"
                        if key not in seen_edu:
                            seen_edu.add(key)
                            educations.append(Education(
                                institution_name=school,
                                degree=degree,
                                from_date=from_y or None,
                                to_date=to_y or None,
                                description=field,
                            ))

                # ── Skills (Duck Typing) ───────────────────────────────────────
                # A skill usually comes as a 'name' under a parent that indicates it's a skill
                name = node.get("name") or (node.get("skill") or {}).get("name") or ""
                if name and len(name) > 1 and len(name) < 100:
                    node_type = node.get("__typename", "") or node.get("$type", "")
                    if "Skill" in str(node_type) or "skill" in str(node_type).lower():
                        if name not in seen_skill:
                            seen_skill.add(name)
                            skills.append(name)

        print(f"[person-scraper][graphql] total graphql responses scanned: {len(graphql_responses)}", file=sys.stderr)
        return experiences, educations, skills

    async def _scrape_profile_via_voyager(self, vanity_name: str) -> dict:
        """
        Fetch structured profile data from LinkedIn's current Voyager Dash API.
        Uses separate endpoints for positions, education, and skills.
        """
        try:
            data = await self.page.evaluate(r'''async (vanityName) => {
                // Extract CSRF token — LinkedIn stores it in JSESSIONID cookie (not HttpOnly)
                function getCsrf() {
                    const raw = document.cookie;
                    // JSESSIONID="ajax:1234" or JSESSIONID=ajax:1234
                    const m = raw.match(/JSESSIONID=(?:"?)?(ajax:[^";&\s]+)/);
                    return m ? m[1] : '';
                }
                const csrf = getCsrf();

                const baseHeaders = {
                    'Accept': 'application/vnd.linkedin.normalized+json+2.1',
                    'X-RestLi-Protocol-Version': '2.0.0',
                    'csrf-token': csrf,
                    'X-Li-Lang': 'en_US',
                    'X-Li-Track': '{"clientVersion":"1.13.18","osName":"web","timezoneOffset":5,"timezone":"Asia/Karachi","deviceFormFactor":"DESKTOP","mpName":"voyager-web"}',
                };

                async function fetchDash(endpoint) {
                    const r = await fetch(endpoint, { headers: baseHeaders });
                    const text = await r.text();
                    return { status: r.status, text: text.substring(0, 500), json: r.ok ? JSON.parse(text) : null };
                }

                const enc = encodeURIComponent(vanityName);
                const baseQ = `q=memberIdentity&memberIdentity=${enc}`;
                const decoPre = 'com.linkedin.voyager.dash.deco.identity.profile.v2';

                const [expRes, eduRes, skillRes] = await Promise.all([
                    fetchDash(`/voyager/api/identity/dash/profilePositionGroups?${baseQ}&decorationId=${decoPre}.FullProfilePositionGroup-64`),
                    fetchDash(`/voyager/api/identity/dash/profileEducations?${baseQ}&decorationId=${decoPre}.FullProfileEducation-32`),
                    fetchDash(`/voyager/api/identity/dash/profileSkills?${baseQ}&decorationId=${decoPre}.FullProfileSkill-32`),
                ]);

                const experience = [];
                const education = [];
                const skills = [];

                // ── Parse experience (position groups) ────────────────────────────
                if (expRes.json?.elements) {
                    for (const group of expRes.json.elements) {
                        const companyName = group.companyName || group.company?.name || '';
                        const positions = group.positions?.elements || group.positionHistory || [group];
                        for (const pos of positions) {
                            const start = pos.dateRange?.start || pos.timePeriod?.startDate;
                            const end = pos.dateRange?.end || pos.timePeriod?.endDate;
                            experience.push({
                                title: pos.title || group.title || '',
                                company: pos.companyName || companyName,
                                from: start ? `${start.month ? start.month + '/' : ''}${start.year || ''}` : '',
                                to: end ? `${end.month ? end.month + '/' : ''}${end.year || ''}` : 'Present',
                                location: pos.locationName || pos.geoLocationName || '',
                                description: pos.description || '',
                            });
                        }
                    }
                }

                // Also check included array for Position items (normalized+json format)
                const included = expRes.json?.included || [];
                for (const item of included) {
                    const t = item['$type'] || '';
                    if (t.includes('Position') && !t.includes('Group')) {
                        const start = item.timePeriod?.startDate;
                        const end = item.timePeriod?.endDate;
                        experience.push({
                            title: item.title || '',
                            company: item.companyName || '',
                            from: start ? `${start.month ? start.month + '/' : ''}${start.year || ''}` : '',
                            to: end ? `${end.month ? end.month + '/' : ''}${end.year || ''}` : 'Present',
                            location: item.locationName || '',
                            description: item.description || '',
                        });
                    }
                }

                // ── Parse education ────────────────────────────────────────────────
                const eduItems = (eduRes.json?.elements || []).concat(eduRes.json?.included || []);
                for (const edu of eduItems) {
                    const t = edu['$type'] || '';
                    if (eduRes.json?.elements && !eduRes.json.elements.includes(edu) && !t.includes('Education')) continue;
                    const school = edu.schoolName || edu.school?.name || '';
                    if (!school) continue;
                    const start = edu.dateRange?.start || edu.timePeriod?.startDate;
                    const end = edu.dateRange?.end || edu.timePeriod?.endDate;
                    education.push({
                        school,
                        degree: edu.degreeName || '',
                        fieldOfStudy: edu.fieldOfStudy || '',
                        from: start?.year?.toString() || '',
                        to: end?.year?.toString() || '',
                        grade: edu.grade || '',
                    });
                }

                // ── Parse skills ───────────────────────────────────────────────────
                const skillItems = (skillRes.json?.elements || []).concat(skillRes.json?.included || []);
                for (const sk of skillItems) {
                    const name = sk.name || sk.skill?.name || '';
                    if (name && name.length > 1) skills.push(name);
                }

                return {
                    experience,
                    education,
                    skills,
                    csrf: csrf ? csrf.substring(0, 15) + '...' : '(empty)',
                    expStatus: expRes.status,
                    eduStatus: eduRes.status,
                    skillStatus: skillRes.status,
                    expSample: expRes.text,
                    eduSample: eduRes.text,
                };
            }''', vanity_name)

            print(f"[person-scraper][voyager] csrf={data.get('csrf')}", file=sys.stderr)
            print(f"[person-scraper][voyager] status: exp={data.get('expStatus')}, edu={data.get('eduStatus')}, skill={data.get('skillStatus')}", file=sys.stderr)
            print(f"[person-scraper][voyager] exp={len(data.get('experience', []))}, edu={len(data.get('education', []))}, skills={len(data.get('skills', []))}", file=sys.stderr)
            if data.get('expStatus') not in (200, None):
                print(f"[person-scraper][voyager] exp sample: {data.get('expSample', '')[:300]}", file=sys.stderr)
            if data.get('eduStatus') not in (200, None):
                print(f"[person-scraper][voyager] edu sample: {data.get('eduSample', '')[:300]}", file=sys.stderr)
            return data
        except Exception as e:
            print(f"[person-scraper][voyager] exception: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc(file=sys.stderr)
            return {}

    async def _extract_all_sections_from_main_page(self) -> dict:
        """
        Extract experience, education and skills from the main profile page in one JS call.
        Tries multiple selector strategies to handle LinkedIn DOM changes.
        """
        return await self.page.evaluate(r'''() => {
            const result = { experience: [], education: [], skills: [] };
            const sectionSeen = { experience: new Set(), education: new Set(), skills: new Set() };

            function classify(headingText) {
                const h = (headingText || '').trim().toLowerCase();
                if (h.includes('experience')) return 'experience';
                if (h.includes('education')) return 'education';
                if (h.includes('skill')) return 'skills';
                return null;
            }

            function getTexts(el) {
                const spans = el.querySelectorAll('span[aria-hidden="true"]');
                const texts = [];
                const seen = new Set();
                if (spans.length > 0) {
                    for (const s of spans) {
                        const t = (s.textContent || '').trim();
                        if (t && t.length > 0 && t.length < 300 && !seen.has(t)) {
                            seen.add(t); texts.push(t);
                        }
                    }
                }
                if (texts.length === 0) {
                    const raw = (el.innerText || '').trim();
                    const lines = raw.split('\n').map(l => l.trim()).filter(l => l.length > 1 && l.length < 300);
                    for (const l of lines) { if (!seen.has(l)) { seen.add(l); texts.push(l); } }
                }
                return texts;
            }

            function addItem(sectionKey, texts, href) {
                if (!texts.length) return;
                const key = texts[0] + '|' + (texts[1] || '');
                if (sectionSeen[sectionKey].has(key)) return;
                sectionSeen[sectionKey].add(key);
                result[sectionKey].push({ texts, href: href || null });
            }

            function processSection(container, sectionKey) {
                // Try profile-component-entity items first
                const entities = container.querySelectorAll('[data-view-name="profile-component-entity"]');
                if (entities.length > 0) {
                    for (const entity of entities) {
                        const texts = getTexts(entity);
                        addItem(sectionKey, texts, entity.querySelector('a')?.getAttribute('href'));
                    }
                    return;
                }
                // Fallback: any list items
                const listItems = container.querySelectorAll(
                    'li.pvs-list__paged-list-item, li.artdeco-list__item, li[class*="pvs-"], .pvs-list > li, li'
                );
                for (const item of listItems) {
                    const texts = getTexts(item);
                    addItem(sectionKey, texts, item.querySelector('a')?.getAttribute('href'));
                }
            }

            // Strategy 1: [data-view-name="profile-card"] sections
            for (const card of document.querySelectorAll('[data-view-name="profile-card"]')) {
                const headingEl = card.querySelector('h2, h3, [data-generated-suggestion-target]');
                if (!headingEl) continue;
                const sectionKey = classify(headingEl.textContent);
                if (!sectionKey) continue;

                if (sectionKey === 'skills') {
                    const skillLinks = card.querySelectorAll('a[href*="/details/skills/"]');
                    for (const link of skillLinks) {
                        for (const sp of link.querySelectorAll('span[aria-hidden="true"]')) {
                            const text = (sp.textContent || '').trim();
                            if (text && text.length > 1 && text.length < 100) {
                                addItem('skills', [text], link.getAttribute('href'));
                                break;
                            }
                        }
                    }
                    if (result.skills.length === 0) processSection(card, 'skills');
                } else {
                    processSection(card, sectionKey);
                }
            }

            // Strategy 2: artdeco-card / section elements (LinkedIn layout variant)
            if (result.experience.length === 0 && result.education.length === 0) {
                const containers = document.querySelectorAll(
                    'section[class*="artdeco"], div[class*="artdeco-card"], section.pv-profile-section, main section'
                );
                for (const container of containers) {
                    const headingEl = container.querySelector('h2, h3');
                    if (!headingEl) continue;
                    const sectionKey = classify(headingEl.textContent);
                    if (!sectionKey) continue;
                    processSection(container, sectionKey);
                }
            }

            // Strategy 3: scan all headings on the page, use the section after each
            if (result.experience.length === 0 && result.education.length === 0) {
                for (const h of document.querySelectorAll('h2, h3')) {
                    const sectionKey = classify(h.textContent);
                    if (!sectionKey) continue;
                    // Collect the nearest ancestor that contains list items
                    let ancestor = h.parentElement;
                    for (let i = 0; i < 5; i++) {
                        if (!ancestor) break;
                        const items = ancestor.querySelectorAll('li');
                        if (items.length > 0) {
                            processSection(ancestor, sectionKey);
                            break;
                        }
                        ancestor = ancestor.parentElement;
                    }
                }
            }

            return result;
        }''')

    def _parse_experience_from_texts(self, texts: list[str], href: Optional[str]) -> Optional[Experience]:
        """Parse an Experience from a list of aria-hidden span texts extracted from the main profile page."""
        if not texts or len(texts) < 1:
            return None
        position_title = texts[0].strip()
        if len(position_title) < 2 or position_title.lower() in ("experience", "education", "skills", "add"):
            return None

        company_raw = texts[1].strip() if len(texts) > 1 else ""
        company_name = company_raw.split("·")[0].strip() if "·" in company_raw else company_raw

        work_times = texts[2].strip() if len(texts) > 2 else ""
        location = texts[3].strip() if len(texts) > 3 else ""

        from_date, to_date, duration = self._parse_work_times(work_times)

        return Experience(
            position_title=position_title,
            institution_name=company_name if company_name else None,
            linkedin_url=href,
            from_date=from_date,
            to_date=to_date,
            duration=duration,
            location=location if location else None,
            description=None,
        )

    def _parse_education_from_texts(self, texts: list[str], href: Optional[str]) -> Optional[Education]:
        """Parse an Education from a list of aria-hidden span texts extracted from the main profile page."""
        if not texts:
            return None
        institution_name = texts[0].strip()
        if len(institution_name) < 2 or institution_name.lower() in ("education", "add", "show all"):
            return None

        degree = None
        times = ""
        if len(texts) == 1:
            pass
        elif len(texts) == 2:
            second = texts[1].strip()
            if any(c.isdigit() for c in second) and (" - " in second or len(second) <= 12):
                times = second
            else:
                degree = second
        else:
            degree = texts[1].strip()
            times = texts[2].strip()

        from_date, to_date = self._parse_education_times(times)
        return Education(
            institution_name=institution_name,
            degree=degree if degree else None,
            linkedin_url=href,
            from_date=from_date,
            to_date=to_date,
            description=None,
        )

    async def _extract_detail_page_items_js(self) -> list[dict]:
        """
        JS-based extraction of profile detail page items (experience, education, skills).
        Handles flat single-role items AND company-group items (nested roles under one company).
        Returns list of {texts, href, companyName?} dicts.
        """
        try:
            return await self.page.evaluate(r'''() => {
                const results = [];
                const globalSeen = new Set();

                // Walk DOM and collect aria-hidden spans, optionally stopping before `stopAt`
                function collectSpans(el, stopAt) {
                    const texts = [];
                    const seen = new Set();
                    const walker = document.createTreeWalker(el, NodeFilter.SHOW_ELEMENT);
                    let node = walker.nextNode();
                    while (node) {
                        if (stopAt && (node === stopAt || stopAt.contains(node))) break;
                        if (node.tagName === 'SPAN' && node.getAttribute('aria-hidden') === 'true') {
                            const t = (node.textContent || '').trim();
                            if (t && t.length > 0 && t.length < 300 && !seen.has(t)) {
                                seen.add(t);
                                texts.push(t);
                            }
                        }
                        node = walker.nextNode();
                    }
                    return texts;
                }

                function getTexts(el) {
                    const texts = collectSpans(el, null);
                    if (texts.length === 0) {
                        // innerText fallback
                        const raw = (el.innerText || '').trim();
                        const seen = new Set();
                        for (const l of raw.split('\n').map(s => s.trim()).filter(s => s.length > 1 && s.length < 300)) {
                            if (!seen.has(l)) { seen.add(l); texts.push(l); }
                        }
                    }
                    return texts;
                }

                function addResult(texts, href, companyName) {
                    if (!texts.length) return;
                    const key = (texts[0] || '') + '|' + (texts[1] || '');
                    if (globalSeen.has(key)) return;
                    globalSeen.add(key);
                    const entry = { texts, href: href || null };
                    if (companyName) entry.companyName = companyName;
                    results.push(entry);
                }

                // Read the company name from spans appearing BEFORE the nested list
                function companyBefore(topEl, nestedEl) {
                    const walker = document.createTreeWalker(topEl, NodeFilter.SHOW_ELEMENT);
                    let node = walker.nextNode();
                    while (node) {
                        if (node === nestedEl || nestedEl.contains(node)) break;
                        if (node.tagName === 'SPAN' && node.getAttribute('aria-hidden') === 'true') {
                            const t = (node.textContent || '').trim();
                            // Skip "N positions" banners
                            if (t && t.length > 1 && t.length < 120 && !/^\d+\s+position/i.test(t)) return t;
                        }
                        node = walker.nextNode();
                    }
                    return '';
                }

                // Handle one top-level li: detect nested group vs flat single role
                function processTopLi(li) {
                    const nestedContainer = li.querySelector(
                        '.pvs-list__container, ul.pvs-list, ol.pvs-list'
                    );
                    if (nestedContainer) {
                        // Company group — get company name from spans before the nested list
                        const company = companyBefore(li, nestedContainer);
                        const subItems = nestedContainer.querySelectorAll(
                            'li.pvs-list__paged-list-item, li[class*="pvs-"], li'
                        );
                        let added = 0;
                        for (const sub of subItems) {
                            // sub-item spans only (no further nesting)
                            const texts = collectSpans(sub, null);
                            if (texts.length > 0) {
                                addResult(texts, sub.querySelector('a')?.getAttribute('href'), company || undefined);
                                added++;
                            }
                        }
                        if (added === 0) {
                            // Fallback: treat outer item text (before nested list) as a single entry
                            const texts = collectSpans(li, nestedContainer);
                            addResult(texts, li.querySelector('a')?.getAttribute('href'), undefined);
                        }
                    } else {
                        addResult(getTexts(li), li.querySelector('a')?.getAttribute('href'));
                    }
                }

                // Strategy 1: data-view-name="profile-component-entity"
                const entities = document.querySelectorAll('[data-view-name="profile-component-entity"]');
                if (entities.length > 0) {
                    for (const entity of entities) {
                        const nestedList = entity.querySelector('.pvs-list__container');
                        if (nestedList) {
                            const company = companyBefore(entity, nestedList);
                            const rootHref = entity.querySelector('a')?.getAttribute('href');
                            for (const sub of nestedList.querySelectorAll('.pvs-list__paged-list-item, li')) {
                                addResult(collectSpans(sub, null), sub.querySelector('a')?.getAttribute('href') || rootHref, company || undefined);
                            }
                        } else {
                            addResult(getTexts(entity), entity.querySelector('a')?.getAttribute('href'));
                        }
                    }
                    if (results.length > 0) return results;
                }

                // Strategy 2: top-level pvs/artdeco list items
                const topSelectors = 'li.pvs-list__paged-list-item, li.artdeco-list__item, li[class*="pvs-"]';
                for (const li of document.querySelectorAll(topSelectors)) {
                    // Only process top-level (not nested inside another matched li)
                    if (li.parentElement?.closest(topSelectors)) continue;
                    processTopLi(li);
                }
                if (results.length > 0) return results;

                // Strategy 3: any top-level li in main/section
                for (const li of document.querySelectorAll('main li, section li')) {
                    if (li.parentElement?.closest('li')) continue;
                    const texts = getTexts(li);
                    if (texts.length >= 1 && texts[0].length > 2) {
                        addResult(texts, li.querySelector('a')?.getAttribute('href'));
                    }
                }
                if (results.length > 0) return results;

                // Strategy 4: divs with multiple sibling aria-hidden spans
                for (const div of document.querySelectorAll('main div, section div')) {
                    const spans = div.querySelectorAll(':scope > span[aria-hidden="true"]');
                    if (spans.length >= 2) {
                        const texts = Array.from(spans).map(s => (s.textContent || '').trim()).filter(t => t.length > 1 && t.length < 300);
                        if (texts.length >= 2) {
                            addResult(texts, div.closest('a')?.getAttribute('href') || div.querySelector('a')?.getAttribute('href'));
                        }
                    }
                }

                return results;
            }''')
        except Exception as e:
            logger.warning(f"JS item extraction failed: {e}")
            return []

    async def _get_experiences(self, base_url: str) -> list[Experience]:
        """Extract experiences from the detail page for full data including descriptions."""
        experiences = []

        try:
            exp_url = urljoin(base_url, "details/experience/")
            await self.navigate_and_wait(exp_url)
            await self.page.wait_for_selector("main", timeout=10000)
            await self.wait_and_focus(2)
            await self.scroll_page_to_half()
            await self.scroll_page_to_bottom(pause_time=0.7, max_scrolls=8)
            await self.wait_and_focus(1.5)
            try:
                await self.page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass

            # Scroll until no new items appear (LinkedIn lazy-loads experience items)
            prev_count = 0
            for _ in range(12):
                cur = await self.page.evaluate(
                    "() => document.querySelectorAll('li.pvs-list__paged-list-item, li[class*=\"pvs-\"], main li').length"
                )
                if cur == prev_count and cur > 0:
                    break
                prev_count = cur
                await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await self.wait_and_focus(1.0)

            items_data = await self._extract_detail_page_items_js()
            print(f"[person-scraper] experience detail page bodyLen={await self.page.evaluate('() => document.body.innerHTML.length')}", file=sys.stderr)
            print(f"[person-scraper] experience page: found {len(items_data)} raw items via JS", file=sys.stderr)

            for item in items_data:
                try:
                    texts = item.get("texts", [])
                    if not texts:
                        continue

                    # If the JS hinted at a company name (nested structure)
                    company_hint = item.get("companyName")
                    
                    if company_hint:
                        # Nested item: texts[0] = title, then optionally employment type, then dates
                        position_title = texts[0]
                        company_name = company_hint
                        work_times = self._find_date_text(texts, start=1)
                        date_idx = self._find_date_text_index(texts, start=1)
                        location = texts[date_idx + 1] if date_idx >= 0 and date_idx + 1 < len(texts) else ""
                    else:
                        # Single item: texts[0] = title, texts[1] = "Company · Type", texts[2] = dates
                        if len(texts) < 2:
                            continue
                        position_title = texts[0]
                        company_raw = texts[1]
                        company_name = company_raw.split("·")[0].strip() if "·" in company_raw else company_raw
                        work_times = self._find_date_text(texts, start=2)
                        date_idx = self._find_date_text_index(texts, start=2)
                        location = texts[date_idx + 1] if date_idx >= 0 and date_idx + 1 < len(texts) else ""

                    # Skip navigation/non-content items
                    if len(position_title) < 2 or position_title.lower() in ("experience", "education", "skills", "add"):
                        continue

                    from_date, to_date, duration = self._parse_work_times(work_times)

                    href = item.get("href") or item.get("detailHref")

                    experiences.append(Experience(
                        position_title=position_title.strip(),
                        institution_name=company_name.strip(),
                        linkedin_url=href,
                        from_date=from_date,
                        to_date=to_date,
                        duration=duration,
                        location=location.strip() if location else None,
                        description=None, # Detail pages don't usually show full desc without more clicks, but this gets all titles!
                    ))
                except Exception as e:
                    logger.debug(f"Error parsing experience item: {e}")
                    continue

            print(f"[person-scraper] parsed {len(experiences)} experiences", file=sys.stderr)

        except Exception as e:
            logger.warning(f"Error getting experiences: {e}")
            print(f"[person-scraper] experience error: {e}", file=sys.stderr)

        return experiences
    
    async def _parse_main_page_experience(self, item) -> Optional[Experience]:
        """Parse experience from main profile page list item with [logo_link, details_link] structure."""
        try:
            links = await item.locator('a').all()
            if len(links) < 2:
                return None
            
            company_url = await links[0].get_attribute('href')
            detail_link = links[1]
            
            unique_texts = await self._extract_unique_texts_from_element(detail_link)
            
            if len(unique_texts) < 2:
                return None
            
            position_title = unique_texts[0]
            company_name = unique_texts[1]
            work_times = unique_texts[2] if len(unique_texts) > 2 else ""
            
            from_date, to_date, duration = self._parse_work_times(work_times)
            
            return Experience(
                position_title=position_title,
                institution_name=company_name,
                linkedin_url=company_url,
                from_date=from_date,
                to_date=to_date,
                duration=duration,
                location=None,
                description=None,
            )
            
        except Exception as e:
            logger.debug(f"Error parsing main page experience: {e}")
            return None
    
    async def _extract_unique_texts_from_element(self, element) -> list[str]:
        """Extract unique text content from nested elements, avoiding duplicates from parent/child overlap."""
        text_elements = await element.locator('span[aria-hidden="true"], div > span').all()
        
        if not text_elements:
            text_elements = await element.locator('span, div').all()
        
        seen_texts = set()
        unique_texts = []
        
        for el in text_elements:
            text = await el.text_content()
            if text and text.strip():
                text = text.strip()
                if text not in seen_texts and len(text) < 200 and not any(text in t or t in text for t in seen_texts if len(t) > 3):
                    seen_texts.add(text)
                    unique_texts.append(text)
        
        return unique_texts

    async def _parse_experience_item(self, item):
        """Parse experience item. Returns Experience or list for nested positions."""
        try:
            links = await item.locator('a, link').all()
            if len(links) >= 2:
                company_url = await links[0].get_attribute('href')
                detail_link = links[1]
                
                generics = await detail_link.locator('generic, span, div').all()
                texts = []
                for g in generics:
                    text = await g.text_content()
                    if text and text.strip() and len(text.strip()) < 200:
                        texts.append(text.strip())
                
                unique_texts = list(dict.fromkeys(texts))
                
                if len(unique_texts) >= 2:
                    position_title = unique_texts[0]
                    company_name = unique_texts[1]
                    work_times = unique_texts[2] if len(unique_texts) > 2 else ""
                    location = unique_texts[3] if len(unique_texts) > 3 else ""
                    
                    from_date, to_date, duration = self._parse_work_times(work_times)
                    
                    return Experience(
                        position_title=position_title,
                        institution_name=company_name,
                        linkedin_url=company_url,
                        from_date=from_date,
                        to_date=to_date,
                        duration=duration,
                        location=location,
                        description=None,
                    )
            
            entity = item.locator('div[data-view-name="profile-component-entity"]').first
            if await entity.count() == 0:
                return None

            children = await entity.locator("> *").all()
            if len(children) < 2:
                return None

            company_link = children[0].locator("a").first
            company_url = await company_link.get_attribute("href")

            detail_container = children[1]
            detail_children = await detail_container.locator("> *").all()

            if len(detail_children) == 0:
                return None

            has_nested_positions = False
            if len(detail_children) > 1:
                nested_list = await detail_children[1].locator(".pvs-list__container").count()
                has_nested_positions = nested_list > 0

            if has_nested_positions:
                return await self._parse_nested_experience(item, company_url, detail_children)
            else:
                first_detail = detail_children[0]
                nested_elements = await first_detail.locator("> *").all()

                if len(nested_elements) == 0:
                    return None

                span_container = nested_elements[0]
                outer_spans = await span_container.locator("> *").all()

                position_title = ""
                company_name = ""
                work_times = ""
                location = ""

                if len(outer_spans) >= 1:
                    aria_span = outer_spans[0].locator('span[aria-hidden="true"]').first
                    position_title = await aria_span.text_content()
                if len(outer_spans) >= 2:
                    aria_span = outer_spans[1].locator('span[aria-hidden="true"]').first
                    company_name = await aria_span.text_content()
                if len(outer_spans) >= 3:
                    aria_span = outer_spans[2].locator('span[aria-hidden="true"]').first
                    work_times = await aria_span.text_content()
                if len(outer_spans) >= 4:
                    aria_span = outer_spans[3].locator('span[aria-hidden="true"]').first
                    location = await aria_span.text_content()

                from_date, to_date, duration = self._parse_work_times(work_times)

                description = ""
                if len(detail_children) > 1:
                    description = await detail_children[1].inner_text()

                return Experience(
                    position_title=position_title.strip(),
                    institution_name=company_name.strip(),
                    linkedin_url=company_url,
                    from_date=from_date,
                    to_date=to_date,
                    duration=duration,
                    location=location.strip(),
                    description=description.strip() if description else None,
                )

        except Exception as e:
            logger.debug(f"Error parsing experience: {e}")
            return None

    async def _parse_nested_experience(
        self, _item, company_url: str, detail_children
    ) -> list[Experience]:
        """
        Parse nested experience positions (multiple roles at the same company).
        Returns a list of Experience objects.
        """
        experiences = []

        try:
            # Get company name from first detail
            first_detail = detail_children[0]
            nested_elements = await first_detail.locator("> *").all()
            if len(nested_elements) == 0:
                return []

            span_container = nested_elements[0]
            outer_spans = await span_container.locator("> *").all()

            # First span is company name for nested positions
            company_name = ""
            if len(outer_spans) >= 1:
                aria_span = outer_spans[0].locator('span[aria-hidden="true"]').first
                company_name = await aria_span.text_content()

            # Get the nested list from detail_children[1]
            nested_container = detail_children[1].locator(".pvs-list__container").first
            nested_items = await nested_container.locator(
                ".pvs-list__paged-list-item"
            ).all()

            for nested_item in nested_items:
                try:
                    # Each nested item has a link with position details
                    link = nested_item.locator("a").first
                    link_children = await link.locator("> *").all()

                    if len(link_children) == 0:
                        continue

                    # Navigate to get the spans
                    first_child = link_children[0]
                    nested_els = await first_child.locator("> *").all()
                    if len(nested_els) == 0:
                        continue

                    spans_container = nested_els[0]
                    position_spans = await spans_container.locator("> *").all()

                    # Extract position details
                    position_title = ""
                    work_times = ""
                    location = ""

                    if len(position_spans) >= 1:
                        aria_span = (
                            position_spans[0].locator('span[aria-hidden="true"]').first
                        )
                        position_title = await aria_span.text_content()
                    if len(position_spans) >= 2:
                        aria_span = (
                            position_spans[1].locator('span[aria-hidden="true"]').first
                        )
                        work_times = await aria_span.text_content()
                    if len(position_spans) >= 3:
                        aria_span = (
                            position_spans[2].locator('span[aria-hidden="true"]').first
                        )
                        location = await aria_span.text_content()

                    # Parse dates
                    from_date, to_date, duration = self._parse_work_times(work_times)

                    # Get description if available
                    description = ""
                    if len(link_children) > 1:
                        description = await link_children[1].inner_text()

                    experiences.append(
                        Experience(
                            position_title=position_title.strip(),
                            institution_name=company_name.strip(),
                            linkedin_url=company_url,
                            from_date=from_date,
                            to_date=to_date,
                            duration=duration,
                            location=location.strip(),
                            description=description.strip() if description else None,
                        )
                    )

                except Exception as e:
                    logger.debug(f"Error parsing nested position: {e}")
                    continue

        except Exception as e:
            logger.debug(f"Error parsing nested experience: {e}")

        return experiences

    _DATE_PATTERN = None  # initialized lazily

    def _looks_like_date(self, text: str) -> bool:
        """Return True if text looks like a date range or duration."""
        import re
        t = text.strip()
        # Matches things like "Jan 2020", "2020 - Present", "Jan 2020 - Dec 2022 · 2 yrs"
        return bool(re.search(r'\b(19|20)\d{2}\b|Present|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec', t))

    def _find_date_text(self, texts: list[str], start: int) -> str:
        """Return the first text from texts[start:] that looks like a date/duration."""
        for t in texts[start:]:
            if self._looks_like_date(t):
                return t
        return texts[start] if len(texts) > start else ""

    def _find_date_text_index(self, texts: list[str], start: int) -> int:
        """Return index of first date-like text starting at `start`, or -1."""
        for i, t in enumerate(texts[start:], start=start):
            if self._looks_like_date(t):
                return i
        return -1

    def _parse_work_times(
        self, work_times: str
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Parse work times string into from_date, to_date, duration.

        Examples:
        - "2000 - Present · 26 yrs 1 mo" -> ("2000", "Present", "26 yrs 1 mo")
        - "Jan 2020 - Dec 2022 · 2 yrs" -> ("Jan 2020", "Dec 2022", "2 yrs")
        - "2015 - Present" -> ("2015", "Present", None)
        """
        if not work_times:
            return None, None, None

        try:
            # Split by · to separate date range from duration
            parts = work_times.split("·")
            times = parts[0].strip() if len(parts) > 0 else ""
            duration = parts[1].strip() if len(parts) > 1 else None

            # Parse dates - split by " - " to get from and to
            if " - " in times:
                date_parts = times.split(" - ")
                from_date = date_parts[0].strip()
                to_date = date_parts[1].strip() if len(date_parts) > 1 else ""
            else:
                from_date = times
                to_date = ""

            return from_date, to_date, duration
        except Exception as e:
            logger.debug(f"Error parsing work times '{work_times}': {e}")
            return None, None, None

    async def _get_educations(self, base_url: str) -> list[Education]:
        """Extract educations from the detail page for full data including descriptions."""
        educations = []

        try:
            edu_url = urljoin(base_url, "details/education/")
            await self.navigate_and_wait(edu_url)
            await self.page.wait_for_selector("main", timeout=10000)
            await self.wait_and_focus(2)
            await self.scroll_page_to_half()
            await self.scroll_page_to_bottom(pause_time=0.7, max_scrolls=8)
            await self.wait_and_focus(1.5)
            try:
                await self.page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass

            prev_count = 0
            for _ in range(8):
                cur = await self.page.evaluate(
                    "() => document.querySelectorAll('li.pvs-list__paged-list-item, li[class*=\"pvs-\"], main li').length"
                )
                if cur == prev_count and cur > 0:
                    break
                prev_count = cur
                await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await self.wait_and_focus(1.0)

            items_data = await self._extract_detail_page_items_js()
            print(f"[person-scraper] education detail page bodyLen={await self.page.evaluate('() => document.body.innerHTML.length')}", file=sys.stderr)
            print(f"[person-scraper] education page: found {len(items_data)} raw items via JS", file=sys.stderr)

            for item in items_data:
                try:
                    texts = item.get("texts", [])
                    if not texts:
                        continue

                    institution_name = texts[0]
                    # Skip navigation/non-content items
                    if len(institution_name) < 2 or institution_name.lower() in ("education", "add", "show all"):
                        continue

                    degree = None
                    times = ""

                    if len(texts) == 1:
                        pass  # just institution name
                    elif len(texts) == 2:
                        second = texts[1]
                        # If it looks like a date range, it's the time period
                        if any(c.isdigit() for c in second) and (" - " in second or len(second) <= 12):
                            times = second
                        else:
                            degree = second
                    else:
                        degree = texts[1]
                        times = texts[2]

                    from_date, to_date = self._parse_education_times(times)
                    href = item.get("href") or item.get("detailHref")

                    educations.append(Education(
                        institution_name=institution_name.strip(),
                        degree=degree.strip() if degree else None,
                        linkedin_url=href,
                        from_date=from_date,
                        to_date=to_date,
                        description=None,
                    ))
                except Exception as e:
                    logger.debug(f"Error parsing education item: {e}")
                    continue

            print(f"[person-scraper] parsed {len(educations)} educations", file=sys.stderr)

        except Exception as e:
            logger.warning(f"Error getting educations: {e}")
            print(f"[person-scraper] education error: {e}", file=sys.stderr)

        return educations
    
    async def _parse_main_page_education(self, item) -> Optional[Education]:
        """Parse education from main profile page list item with [logo_link, details_link] structure."""
        try:
            links = await item.locator('a').all()
            if not links:
                return None
            
            institution_url = await links[0].get_attribute('href')
            detail_link = links[1] if len(links) > 1 else links[0]
            
            unique_texts = await self._extract_unique_texts_from_element(detail_link)
            
            if not unique_texts:
                return None
            
            institution_name = unique_texts[0]
            degree = None
            times = ""
            
            if len(unique_texts) == 3:
                degree = unique_texts[1]
                times = unique_texts[2]
            elif len(unique_texts) == 2:
                second = unique_texts[1]
                if " - " in second or any(c.isdigit() for c in second):
                    times = second
                else:
                    degree = second
            
            from_date, to_date = self._parse_education_times(times)
            
            return Education(
                institution_name=institution_name,
                degree=degree.strip() if degree else None,
                linkedin_url=institution_url,
                from_date=from_date,
                to_date=to_date,
                description=None,
            )
            
        except Exception as e:
            logger.debug(f"Error parsing main page education: {e}")
            return None

    async def _parse_education_item(self, item) -> Optional[Education]:
        """Parse a single education item."""
        try:
            links = await item.locator('a, link').all()
            if len(links) >= 1:
                institution_url = await links[0].get_attribute('href')
                
                detail_link = links[1] if len(links) >= 2 else links[0]
                generics = await detail_link.locator('generic, span, div').all()
                texts = []
                for g in generics:
                    text = await g.text_content()
                    if text and text.strip() and len(text.strip()) < 200:
                        texts.append(text.strip())
                
                unique_texts = list(dict.fromkeys(texts))
                
                if unique_texts:
                    institution_name = unique_texts[0]
                    degree = None
                    times = ""
                    
                    if len(unique_texts) == 3:
                        degree = unique_texts[1]
                        times = unique_texts[2]
                    elif len(unique_texts) == 2:
                        second = unique_texts[1]
                        if " - " in second or second.isdigit() or any(c.isdigit() for c in second):
                            times = second
                        else:
                            degree = second
                    
                    from_date, to_date = self._parse_education_times(times)
                    
                    return Education(
                        institution_name=institution_name,
                        degree=degree.strip() if degree else None,
                        linkedin_url=institution_url,
                        from_date=from_date,
                        to_date=to_date,
                        description=None,
                    )
            
            entity = item.locator('div[data-view-name="profile-component-entity"]').first
            if await entity.count() == 0:
                return None

            children = await entity.locator("> *").all()
            if len(children) < 2:
                return None

            institution_link = children[0].locator("a").first
            institution_url = await institution_link.get_attribute("href")

            detail_container = children[1]
            detail_children = await detail_container.locator("> *").all()

            if len(detail_children) == 0:
                return None

            first_detail = detail_children[0]
            nested_elements = await first_detail.locator("> *").all()

            if len(nested_elements) == 0:
                return None

            span_container = nested_elements[0]
            outer_spans = await span_container.locator("> *").all()

            institution_name = ""
            degree = None
            times = ""

            if len(outer_spans) >= 1:
                aria_span = outer_spans[0].locator('span[aria-hidden="true"]').first
                institution_name = await aria_span.text_content()

            if len(outer_spans) == 3:
                aria_span = outer_spans[1].locator('span[aria-hidden="true"]').first
                degree = await aria_span.text_content()
                aria_span = outer_spans[2].locator('span[aria-hidden="true"]').first
                times = await aria_span.text_content()
            elif len(outer_spans) == 2:
                aria_span = outer_spans[1].locator('span[aria-hidden="true"]').first
                times = await aria_span.text_content()

            from_date, to_date = self._parse_education_times(times)

            description = ""
            if len(detail_children) > 1:
                description = await detail_children[1].inner_text()

            return Education(
                institution_name=institution_name.strip(),
                degree=degree.strip() if degree else None,
                linkedin_url=institution_url,
                from_date=from_date,
                to_date=to_date,
                description=description.strip() if description else None,
            )

        except Exception as e:
            logger.debug(f"Error parsing education: {e}")
            return None

    def _parse_education_times(self, times: str) -> tuple[Optional[str], Optional[str]]:
        """
        Parse education times string into from_date, to_date.

        Examples:
        - "1973 - 1977" -> ("1973", "1977")
        - "2015" -> ("2015", "2015")
        - "" -> (None, None)
        """
        if not times:
            return None, None

        try:
            # Split by " - " to get from and to dates
            if " - " in times:
                parts = times.split(" - ")
                from_date = parts[0].strip()
                to_date = parts[1].strip() if len(parts) > 1 else ""
            else:
                # Single year
                from_date = times.strip()
                to_date = times.strip()

            return from_date, to_date
        except Exception as e:
            logger.debug(f"Error parsing education times '{times}': {e}")
            return None, None

    async def _get_skills(self, base_url: str) -> list[str]:
        """Extract skills from the skills detail page."""
        skills = []
        seen: set[str] = set()

        try:
            skills_url = urljoin(base_url, "details/skills/")
            await self.navigate_and_wait(skills_url)
            await self.page.wait_for_selector("main", timeout=10000)
            await self.wait_and_focus(1)
            await self.scroll_page_to_half()
            await self.scroll_page_to_bottom(pause_time=0.5, max_scrolls=5)

            nothing = await self.page.locator('text="Nothing to see for now"').count()
            if nothing > 0:
                return skills

            items_data = await self._extract_detail_page_items_js()
            print(f"[person-scraper] skills page: found {len(items_data)} raw items via JS", file=sys.stderr)

            for item in items_data:
                texts = item.get("texts", [])
                if texts:
                    skill_name = texts[0].strip()
                    # Skip navigation items and very short/long strings
                    if skill_name and 1 < len(skill_name) < 100 and skill_name not in seen:
                        # Filter out obvious non-skill entries
                        if skill_name.lower() not in ("skills", "add", "show all", "endorse"):
                            seen.add(skill_name)
                            skills.append(skill_name)

            print(f"[person-scraper] parsed {len(skills)} skills", file=sys.stderr)

        except Exception as e:
            logger.warning(f"Error getting skills: {e}")
            print(f"[person-scraper] skills error: {e}", file=sys.stderr)

        return skills

    async def _get_interests(self, base_url: str) -> list[Interest]:
        """Extract interests from the main profile page Interests section with tablist."""
        interests = []

        try:
            interests_heading = self.page.locator('h2:has-text("Interests")').first
            
            if await interests_heading.count() > 0:
                interests_section = interests_heading.locator('xpath=ancestor::*[.//tablist or .//*[@role="tablist"]][1]')
                if await interests_section.count() == 0:
                    interests_section = interests_heading.locator('xpath=ancestor::*[4]')
                
                tabs = await interests_section.locator('[role="tab"], tab').all() if await interests_section.count() > 0 else []
                
                if tabs:
                    for tab in tabs:
                        try:
                            tab_name = await tab.text_content()
                            if not tab_name:
                                continue
                            tab_name = tab_name.strip()
                            category = self._map_interest_tab_to_category(tab_name)

                            await tab.click()
                            await self.wait_and_focus(0.5)

                            tabpanel = interests_section.locator('[role="tabpanel"]').first
                            if await tabpanel.count() > 0:
                                list_items = await tabpanel.locator('li, listitem').all()
                                
                                for item in list_items:
                                    try:
                                        interest = await self._parse_interest_item(item, category)
                                        if interest:
                                            interests.append(interest)
                                    except Exception as e:
                                        logger.debug(f"Error parsing interest item: {e}")
                                        continue
                        except Exception as e:
                            logger.debug(f"Error processing interest tab: {e}")
                            continue
            
            if not interests:
                interests_url = urljoin(base_url, "details/interests/")
                await self.navigate_and_wait(interests_url)
                await self.page.wait_for_selector("main", timeout=10000)
                await self.wait_and_focus(1.5)

                tabs = await self.page.locator('[role="tab"], tab').all()

                if not tabs:
                    logger.debug("No interests tabs found on profile")
                    return interests

                for tab in tabs:
                    try:
                        tab_name = await tab.text_content()
                        if not tab_name:
                            continue
                        tab_name = tab_name.strip()
                        category = self._map_interest_tab_to_category(tab_name)

                        await tab.click()
                        await self.wait_and_focus(0.8)

                        tabpanel = self.page.locator('[role="tabpanel"], tabpanel').first
                        list_items = await tabpanel.locator("listitem, li, .pvs-list__paged-list-item").all()

                        for item in list_items:
                            try:
                                interest = await self._parse_interest_item(item, category)
                                if interest:
                                    interests.append(interest)
                            except Exception as e:
                                logger.debug(f"Error parsing interest item: {e}")
                                continue

                    except Exception as e:
                        logger.debug(f"Error processing interest tab: {e}")
                        continue

        except Exception as e:
            logger.warning(f"Error getting interests: {e}")

        return interests
    
    async def _parse_interest_item(self, item, category: str) -> Optional[Interest]:
        """Parse a single interest item from profile or details page."""
        try:
            link = item.locator("a, link").first
            if await link.count() == 0:
                return None
            href = await link.get_attribute("href")

            unique_texts = await self._extract_unique_texts_from_element(item)
            name = unique_texts[0] if unique_texts else None

            if name and href:
                return Interest(
                    name=name,
                    category=category,
                    linkedin_url=href,
                )
            return None
        except Exception as e:
            logger.debug(f"Error parsing interest: {e}")
            return None

    def _map_interest_tab_to_category(self, tab_name: str) -> str:
        tab_lower = tab_name.lower()
        if "compan" in tab_lower:
            return "company"
        elif "group" in tab_lower:
            return "group"
        elif "school" in tab_lower:
            return "school"
        elif "newsletter" in tab_lower:
            return "newsletter"
        elif "voice" in tab_lower or "influencer" in tab_lower:
            return "influencer"
        else:
            return tab_lower

    async def _get_accomplishments(self, base_url: str) -> list[Accomplishment]:
        accomplishments = []

        accomplishment_sections = [
            ("certifications", "certification"),
            ("honors", "honor"),
            ("publications", "publication"),
            ("patents", "patent"),
            ("courses", "course"),
            ("projects", "project"),
            ("languages", "language"),
            ("organizations", "organization"),
        ]

        total_sections = len(accomplishment_sections)
        for idx, (url_path, category) in enumerate(accomplishment_sections):
            try:
                pct = 68 + int((idx / total_sections) * 17)
                await self.callback.on_progress(f"Checking {category}s...", pct)
                section_url = urljoin(base_url, f"details/{url_path}/")
                await self.navigate_and_wait(section_url)
                await self.page.wait_for_selector("main", timeout=10000)
                await self.wait_and_focus(1)

                nothing_to_see = await self.page.locator(
                    'text="Nothing to see for now"'
                ).count()
                if nothing_to_see > 0:
                    continue

                main_list = self.page.locator(
                    ".pvs-list__container, main ul, main ol"
                ).first
                if await main_list.count() == 0:
                    continue

                items = await main_list.locator(".pvs-list__paged-list-item").all()
                if not items:
                    items = await main_list.locator("> li").all()

                seen_titles = set()
                for item in items:
                    try:
                        accomplishment = await self._parse_accomplishment_item(
                            item, category
                        )
                        if accomplishment and accomplishment.title not in seen_titles:
                            seen_titles.add(accomplishment.title)
                            accomplishments.append(accomplishment)
                    except Exception as e:
                        logger.debug(f"Error parsing {category} item: {e}")
                        continue

            except Exception as e:
                logger.debug(f"Error getting {category}s: {e}")
                continue

        return accomplishments

    async def _parse_accomplishment_item(
        self, item, category: str
    ) -> Optional[Accomplishment]:
        try:
            entity = item.locator(
                'div[data-view-name="profile-component-entity"]'
            ).first
            if await entity.count() > 0:
                spans = await entity.locator('span[aria-hidden="true"]').all()
            else:
                spans = await item.locator('span[aria-hidden="true"]').all()

            title = ""
            issuer = ""
            issued_date = ""
            credential_id = ""

            for i, span in enumerate(spans[:5]):
                text = await span.text_content()
                if not text:
                    continue
                text = text.strip()

                if len(text) > 500:
                    continue

                if i == 0:
                    title = text
                elif "Issued by" in text:
                    parts = text.split("·")
                    issuer = parts[0].replace("Issued by", "").strip()
                    if len(parts) > 1:
                        issued_date = parts[1].strip()
                elif "Issued " in text and not issued_date:
                    issued_date = text.replace("Issued ", "")
                elif "Credential ID" in text:
                    credential_id = text.replace("Credential ID ", "")
                elif i == 1 and not issuer:
                    issuer = text
                elif (
                    any(
                        month in text
                        for month in [
                            "Jan",
                            "Feb",
                            "Mar",
                            "Apr",
                            "May",
                            "Jun",
                            "Jul",
                            "Aug",
                            "Sep",
                            "Oct",
                            "Nov",
                            "Dec",
                        ]
                    )
                    and not issued_date
                ):
                    if "·" in text:
                        parts = text.split("·")
                        issued_date = parts[0].strip()
                    else:
                        issued_date = text

            link = item.locator('a[href*="credential"], a[href*="verify"]').first
            credential_url = (
                await link.get_attribute("href") if await link.count() > 0 else None
            )

            if not title or len(title) > 200:
                return None

            return Accomplishment(
                category=category,
                title=title,
                issuer=issuer if issuer else None,
                issued_date=issued_date if issued_date else None,
                credential_id=credential_id if credential_id else None,
                credential_url=credential_url,
            )

        except Exception as e:
            logger.debug(f"Error parsing accomplishment: {e}")
            return None

    async def _get_contacts(self, base_url: str) -> list[Contact]:
        """Extract contact info from the contact-info overlay dialog."""
        contacts = []

        try:
            contact_url = urljoin(base_url, "overlay/contact-info/")
            await self.navigate_and_wait(contact_url)
            await self.wait_and_focus(1)

            dialog = self.page.locator('dialog, [role="dialog"]').first
            if await dialog.count() == 0:
                logger.warning("Contact info dialog not found")
                return contacts

            contact_sections = await dialog.locator('h3').all()
            
            for section_heading in contact_sections:
                try:
                    heading_text = await section_heading.text_content()
                    if not heading_text:
                        continue
                    heading_text = heading_text.strip().lower()
                    
                    section_container = section_heading.locator('xpath=ancestor::*[1]')
                    if await section_container.count() == 0:
                        continue
                    
                    contact_type = self._map_contact_heading_to_type(heading_text)
                    if not contact_type:
                        continue
                    
                    links = await section_container.locator('a').all()
                    for link in links:
                        href = await link.get_attribute('href')
                        text = await link.text_content()
                        if href and text:
                            text = text.strip()
                            label = None
                            sibling_text = await section_container.locator('span, generic').all()
                            for sib in sibling_text:
                                sib_text = await sib.text_content()
                                if sib_text and sib_text.strip().startswith('(') and sib_text.strip().endswith(')'):
                                    label = sib_text.strip()[1:-1]
                                    break
                            
                            if contact_type == "linkedin":
                                contacts.append(Contact(type=contact_type, value=href, label=label))
                            elif contact_type == "email" and "mailto:" in href:
                                contacts.append(Contact(type=contact_type, value=href.replace("mailto:", ""), label=label))
                            else:
                                contacts.append(Contact(type=contact_type, value=text, label=label))
                    
                    if contact_type == "birthday" and not links:
                        birthday_text = await section_container.text_content()
                        if birthday_text:
                            birthday_value = birthday_text.replace(heading_text, "").replace("Birthday", "").strip()
                            if birthday_value:
                                contacts.append(Contact(type="birthday", value=birthday_value))
                    
                    if contact_type == "phone" and not links:
                        phone_text = await section_container.text_content()
                        if phone_text:
                            phone_value = phone_text.replace(heading_text, "").replace("Phone", "").strip()
                            if phone_value:
                                contacts.append(Contact(type="phone", value=phone_value))
                    
                    if contact_type == "address" and not links:
                        address_text = await section_container.text_content()
                        if address_text:
                            address_value = address_text.replace(heading_text, "").replace("Address", "").strip()
                            if address_value:
                                contacts.append(Contact(type="address", value=address_value))
                                
                except Exception as e:
                    logger.debug(f"Error parsing contact section: {e}")
                    continue

        except Exception as e:
            logger.warning(f"Error getting contacts: {e}")

        return contacts
    
    def _map_contact_heading_to_type(self, heading: str) -> Optional[str]:
        """Map contact section heading to contact type."""
        heading = heading.lower()
        if "profile" in heading:
            return "linkedin"
        elif "website" in heading:
            return "website"
        elif "email" in heading:
            return "email"
        elif "phone" in heading:
            return "phone"
        elif "twitter" in heading or "x.com" in heading:
            return "twitter"
        elif "birthday" in heading:
            return "birthday"
        elif "address" in heading:
            return "address"
        return None
