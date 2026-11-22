"""Browser lifecycle management for Playwright."""

import asyncio
import json
import logging
import random
import traceback
from pathlib import Path
from typing import Optional, Dict, Any
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

from .exceptions import NetworkError

logger = logging.getLogger(__name__)

# ── Real Chrome user-agents (updated periodically) ───────────────────────────
_USER_AGENTS = [
    # Chrome 120 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    # Chrome 121 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    # Chrome 122 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Chrome 123 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Chrome 120 – macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    # Chrome 122 – macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Edge 121 – Windows (common in enterprise)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
]

# Common desktop viewport sizes — avoids robotic 1280x720 every time
_VIEWPORTS = [
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1600, "height": 900},
    {"width": 1920, "height": 1080},
    {"width": 1280, "height": 800},
    {"width": 1280, "height": 720},
]


class BrowserManager:
    """Async context manager for Playwright browser lifecycle."""
    
    def __init__(
        self,
        headless: bool = True,
        slow_mo: int = 0,
        viewport: Optional[Dict[str, int]] = None,
        user_agent: Optional[str] = None,
        **launch_options: Any
    ):
        """
        Initialize browser manager.

        Args:
            headless: Run browser in headless mode
            slow_mo: Slow down operations by specified milliseconds
            viewport: Browser viewport size (randomised from common desktop sizes if not set)
            user_agent: Custom UA — leave None to use Playwright's real Chromium UA.
                        WARNING: Setting a hardcoded UA that differs from the actual
                        Chromium binary version creates a detectable mismatch.
            **launch_options: Additional Playwright launch options
        """
        self.headless = headless
        self.slow_mo = slow_mo
        # Randomise viewport per session so window dimensions vary naturally.
        self.viewport = viewport or random.choice(_VIEWPORTS)
        # Do NOT default to a hardcoded UA string — Playwright's built-in Chromium
        # UA is self-consistent with all JS APIs; any override risks a version mismatch.
        self.user_agent = user_agent  # None = use real Chromium UA
        self.launch_options = launch_options
        
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._is_authenticated = False
    
    async def __aenter__(self) -> "BrowserManager":
        """Start browser and create context."""
        await self.start()
        return self
    
    async def __aexit__(self, _exc_type, _exc_val, _exc_tb) -> None:
        """Close browser and cleanup."""
        await self.close()
    
    async def start(self) -> None:
        """Start Playwright and launch browser with stealth anti-detection flags."""
        try:
            self._playwright = await async_playwright().start()

            # Chromium launch flags. Key rule: never add
            # --disable-features=IsolateOrigins,site-per-process — it breaks cross-origin
            # cookie scoping and causes ERR_TOO_MANY_REDIRECTS on authenticated sites.
            stealth_args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-first-run",
                "--no-service-autorun",
                "--password-store=basic",
                "--use-mock-keychain",
                # Suppress "Chrome is being controlled by automated software" bar
                "--disable-component-extensions-with-background-pages",
                # GPU: ignore the blocklist so Chromium at least tries GPU/WebGL
                # instead of immediately falling back to SwiftShader.
                "--ignore-gpu-blocklist",
                # Suppress crash reporter and metrics that can signal automation
                "--disable-crash-reporter",
                "--metrics-recording-only",
                "--no-report-upload",
                # Suppress first-run / default-browser prompts
                "--no-default-browser-check",
                "--disable-sync",
                "--hide-crash-restore-bubble",
                f"--window-size={self.viewport['width']},{self.viewport['height']}",
            ]

            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                slow_mo=self.slow_mo,
                args=stealth_args,
                **self.launch_options
            )

            logger.info(f"Browser launched (headless={self.headless})")

            # Randomise locale/timezone so each session looks like a different real user's region
            _locale = random.choice(["en-US", "en-GB", "en-CA", "en-AU"])
            _timezone = random.choice([
                "America/New_York", "America/Chicago", "America/Los_Angeles",
                "Europe/London", "Europe/Berlin", "America/Toronto",
            ])
            # ── User-Agent: always present as Windows Chrome ──────────────────
            # LinkedIn session cookies are created on the user's Windows/Mac desktop
            # Chrome. On a Linux production server Playwright's real UA contains
            # "X11; Linux x86_64", which LinkedIn's session validator flags as an
            # OS switch → immediately invalidates the token on profile pages.
            # Strategy: launch a throwaway page, read the real Chromium version,
            # then rebuild the UA as "Windows NT 10.0" so the OS looks consistent
            # with where the cookies were originally created. The Chrome version
            # stays identical so navigator.appVersion matches the real binary.
            _tmp_ctx = await self._browser.new_context()
            _tmp_page = await _tmp_ctx.new_page()
            _raw_ua: str = await _tmp_page.evaluate("() => navigator.userAgent")
            await _tmp_page.close()
            await _tmp_ctx.close()

            # Replace Linux/Mac OS token with Windows 10 — keep Chrome version intact
            import re as _re
            if "Linux" in _raw_ua or "Macintosh" in _raw_ua:
                _win_ua = _re.sub(
                    r'\(.*?\)',
                    '(Windows NT 10.0; Win64; x64)',
                    _raw_ua,
                    count=1,
                )
            else:
                _win_ua = _raw_ua  # already Windows, no change needed

            # Caller-supplied UA takes highest priority
            _effective_ua = self.user_agent or _win_ua
            logger.info(f"Raw browser UA : {_raw_ua}")
            logger.info(f"Effective UA   : {_effective_ua}")

            self._context = await self._browser.new_context(
                viewport=self.viewport,
                locale=_locale,
                timezone_id=_timezone,
                color_scheme="light",
                user_agent=_effective_ua,
                extra_http_headers={
                    "Accept-Language": f"{_locale},{_locale.split('-')[0]};q=0.9,en;q=0.8",
                },
            )
            self._page = await self._context.new_page()

            # ── JS stealth patches — injected before every page script runs ─────
            # add_init_script on the context applies to ALL pages/frames in this
            # context, including iframes. This is important because LinkedIn loads
            # detection scripts inside sub-frames too.
            await self._context.add_init_script("""
                (() => {
                    // ── 1. navigator.webdriver ───────────────────────────────────────
                    // The #1 signal. Must be undefined (not false) — some detectors
                    // check typeof instead of truthiness.
                    try {
                        Object.defineProperty(navigator, 'webdriver', {
                            get: () => undefined,
                            configurable: true,
                        });
                    } catch(e) {}

                    // ── 2. Plugin array ──────────────────────────────────────────────
                    // Headless has 0 plugins. Real Chrome has 3 PDF-related entries.
                    try {
                        const fakePlugins = [
                            { name: 'Chrome PDF Plugin',  filename: 'internal-pdf-viewer',           description: 'Portable Document Format', length: 1 },
                            { name: 'Chrome PDF Viewer',  filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '',                        length: 1 },
                            { name: 'Native Client',      filename: 'internal-nacl-plugin',           description: '',                        length: 2 },
                        ];
                        Object.defineProperty(navigator, 'plugins', {
                            get: () => Object.assign(Object.create(PluginArray.prototype), fakePlugins, { length: fakePlugins.length }),
                            configurable: true,
                        });
                        Object.defineProperty(navigator, 'mimeTypes', {
                            get: () => Object.create(MimeTypeArray.prototype),
                            configurable: true,
                        });
                    } catch(e) {}

                    // ── 3. Languages ─────────────────────────────────────────────────
                    // Headless returns [] — trivially detectable.
                    try {
                        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'], configurable: true });
                        Object.defineProperty(navigator, 'language',  { get: () => 'en-US',          configurable: true });
                    } catch(e) {}

                    // ── 4. window.chrome ─────────────────────────────────────────────
                    // Absent in headless — LinkedIn checks window.chrome.runtime.
                    try {
                        if (!window.chrome) {
                            window.chrome = {
                                app: {
                                    isInstalled: false,
                                    InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
                                    RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
                                },
                                runtime: {
                                    id: undefined,
                                    connect: () => {},
                                    sendMessage: () => {},
                                },
                                loadTimes: () => ({}),
                                csi: () => ({}),
                            };
                        }
                    } catch(e) {}

                    // ── 5. Permissions API ───────────────────────────────────────────
                    // Headless returns 'denied' for notifications; real Chrome → 'default'.
                    try {
                        if (window.Permissions) {
                            const orig = Permissions.prototype.query;
                            Permissions.prototype.query = function(params) {
                                if (params && params.name === 'notifications') {
                                    return Promise.resolve(Object.create(PermissionStatus.prototype, {
                                        state: { value: 'default', writable: false },
                                        onchange: { value: null, writable: true },
                                    }));
                                }
                                return orig.call(this, params);
                            };
                        }
                    } catch(e) {}

                    // ── 6. Hardware + OS signals ──────────────────────────────────────
                    // navigator.platform must match the User-Agent OS token.
                    // On a Linux server the real value is "Linux x86_64"; patching it to
                    // "Win32" keeps it consistent with the Windows UA we send in headers.
                    try {
                        Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8,       configurable: true });
                        Object.defineProperty(navigator, 'deviceMemory',         { get: () => 8,       configurable: true });
                        Object.defineProperty(navigator, 'maxTouchPoints',       { get: () => 0,       configurable: true });
                        Object.defineProperty(navigator, 'platform',             { get: () => 'Win32', configurable: true });
                        Object.defineProperty(navigator, 'oscpu',                { get: () => undefined, configurable: true });
                        Object.defineProperty(screen,    'colorDepth',           { get: () => 24,      configurable: true });
                        Object.defineProperty(screen,    'pixelDepth',           { get: () => 24,      configurable: true });
                    } catch(e) {}

                    // ── 7. WebGL vendor/renderer ─────────────────────────────────────
                    // THIS IS THE MAIN REMAINING DETECTION VECTOR.
                    // Headless exposes "SwiftShader" (Google's software renderer) which is
                    // an immediate 100% bot signal. Patch getParameter to return realistic
                    // Intel GPU strings for the two extension-only parameters LinkedIn probes.
                    try {
                        const _getParam = WebGLRenderingContext.prototype.getParameter;
                        WebGLRenderingContext.prototype.getParameter = function(param) {
                            // UNMASKED_VENDOR_WEBGL (0x9245) and UNMASKED_RENDERER_WEBGL (0x9246)
                            if (param === 0x9245) return 'Intel Inc.';
                            if (param === 0x9246) return 'Intel(R) Iris(TM) Plus Graphics 640';
                            return _getParam.call(this, param);
                        };
                    } catch(e) {}
                    try {
                        const _getParam2 = WebGL2RenderingContext.prototype.getParameter;
                        WebGL2RenderingContext.prototype.getParameter = function(param) {
                            if (param === 0x9245) return 'Intel Inc.';
                            if (param === 0x9246) return 'Intel(R) Iris(TM) Plus Graphics 640';
                            return _getParam2.call(this, param);
                        };
                    } catch(e) {}

                    // ── 8. Canvas fingerprint noise ──────────────────────────────────
                    // Every headless session produces identical canvas output — bots can be
                    // identified when the same exact pixel hash is seen on a new "user".
                    // Add 1-pixel-in-20 of sub-lsb noise so hashes differ between sessions
                    // while remaining visually identical.
                    try {
                        const _toDataURL = HTMLCanvasElement.prototype.toDataURL;
                        HTMLCanvasElement.prototype.toDataURL = function(type, quality) {
                            const ctx = this.getContext('2d');
                            if (ctx) {
                                const img = ctx.getImageData(0, 0, this.width || 1, this.height || 1);
                                for (let i = 0; i < img.data.length; i += 4) {
                                    if (Math.random() < 0.04) img.data[i] ^= 1;
                                }
                                ctx.putImageData(img, 0, 0);
                            }
                            return _toDataURL.call(this, type, quality);
                        };
                        const _getImageData = CanvasRenderingContext2D.prototype.getImageData;
                        CanvasRenderingContext2D.prototype.getImageData = function(x, y, w, h) {
                            const data = _getImageData.call(this, x, y, w, h);
                            for (let i = 0; i < data.data.length; i += 4) {
                                if (Math.random() < 0.03) data.data[i] ^= 1;
                            }
                            return data;
                        };
                    } catch(e) {}

                    // ── 9. iframe contentWindow.navigator.webdriver ──────────────────
                    // Some detectors create a hidden iframe and read its navigator — patch
                    // HTMLIFrameElement so the contentWindow also has webdriver=undefined.
                    try {
                        const _attach = HTMLIFrameElement.prototype.__lookupGetter__('contentWindow');
                        if (_attach) {
                            Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
                                get: function() {
                                    const win = _attach.call(this);
                                    if (win && win.navigator) {
                                        try { Object.defineProperty(win.navigator, 'webdriver', { get: () => undefined, configurable: true }); } catch(_) {}
                                    }
                                    return win;
                                },
                                configurable: true,
                            });
                        }
                    } catch(e) {}
                })();
            """)

            # playwright-stealth v2: masks additional automation signals on top of our patches
            try:
                from playwright_stealth import Stealth
                await Stealth().apply_stealth_async(self._page)
                logger.info("playwright-stealth (v2) applied")
            except ImportError:
                logger.warning("playwright-stealth not installed — JS patches applied manually")
            except Exception as e:
                logger.warning(f"playwright-stealth error (non-fatal): {e}")

            logger.info("Browser context and page created")

        except Exception as e:
            logger.error(f"Error starting browser in browser.py:\n{traceback.format_exc()}")
            await self.close()
            raise NetworkError(f"Failed to start browser: {e}")

    async def close(self) -> None:
        """Close browser and cleanup resources."""
        try:
            if self._page:
                await self._page.close()
                self._page = None
            
            if self._context:
                await self._context.close()
                self._context = None
            
            if self._browser:
                await self._browser.close()
                self._browser = None
            
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
            
            logger.info("Browser closed")
            
        except Exception as e:
            logger.error(f"Error closing browser: {e}")
    
    async def new_page(self) -> Page:
        """
        Create a new page in the current context.
        
        Returns:
            New Playwright page
        """
        if not self._context:
            raise RuntimeError("Browser context not initialized. Call start() first.")
        
        page = await self._context.new_page()
        return page
    
    @property
    def page(self) -> Page:
        """
        Get the main page.
        
        Returns:
            Main Playwright page
        """
        if not self._page:
            raise RuntimeError("Browser not started. Use async context manager or call start().")
        return self._page
    
    @property
    def context(self) -> BrowserContext:
        """
        Get the browser context.
        
        Returns:
            Playwright browser context
        """
        if not self._context:
            raise RuntimeError("Browser context not initialized.")
        return self._context
    
    @property
    def browser(self) -> Browser:
        """
        Get the browser instance.
        
        Returns:
            Playwright browser
        """
        if not self._browser:
            raise RuntimeError("Browser not started.")
        return self._browser
    
    async def save_session(self, filepath: str) -> None:
        """
        Save browser session (cookies and storage) to file.
        
        Args:
            filepath: Path to save session file
        """
        if not self._context:
            raise RuntimeError("No browser context to save")
        
        storage_state = await self._context.storage_state()
        
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(filepath, 'w') as f:
            json.dump(storage_state, f, indent=2)
        
        logger.info(f"Session saved to {filepath}")
    
    async def load_session(self, filepath: str) -> None:
        """
        Load browser session from file.
        
        Args:
            filepath: Path to session file
        """
        if not Path(filepath).exists():
            raise FileNotFoundError(f"Session file not found: {filepath}")
        
        # Close existing context and create new one with stored state
        if self._context:
            await self._context.close()
        
        if not self._browser:
            raise RuntimeError("Browser not started")
        
        self._context = await self._browser.new_context(
            storage_state=filepath,
            viewport=self.viewport,
            user_agent=self.user_agent
        )
        
        # Create new page
        if self._page:
            await self._page.close()
        self._page = await self._context.new_page()
        
        self._is_authenticated = True
        
        logger.info(f"Session loaded from {filepath}")
    
    async def set_cookie(self, name: str, value: str, domain: str = ".linkedin.com") -> None:
        """
        Set a single cookie.
        
        Args:
            name: Cookie name
            value: Cookie value
            domain: Cookie domain
        """
        if not self._context:
            raise RuntimeError("No browser context")
        
        await self._context.add_cookies([{
            "name": name,
            "value": value,
            "domain": domain,
            "path": "/"
        }])
        
        logger.debug(f"Cookie set: {name}")
    
    @property
    def is_authenticated(self) -> bool:
        """Check if user is authenticated."""
        return self._is_authenticated
    
    @is_authenticated.setter
    def is_authenticated(self, value: bool) -> None:
        """Set authentication status."""
        self._is_authenticated = value

    # ── Human-behaviour helpers ───────────────────────────────────────────────

    async def human_mouse_drift(self, steps: int = 3) -> None:
        """Move mouse in small random arcs — mimics hand jitter between actions."""
        if not self._page:
            return
        vw = self.viewport["width"]
        vh = self.viewport["height"]
        for _ in range(steps):
            x = random.randint(int(vw * 0.2), int(vw * 0.8))
            y = random.randint(int(vh * 0.2), int(vh * 0.8))
            await self._page.mouse.move(x, y, steps=random.randint(8, 20))
            await asyncio.sleep(random.uniform(0.05, 0.2))

    async def human_scroll(self, distance: int = 600, direction: str = "down") -> None:
        """Scroll the page in small incremental steps with acceleration/deceleration."""
        if not self._page:
            return
        sign = 1 if direction == "down" else -1
        # Split distance into chunks with slight randomness
        remaining = abs(distance)
        while remaining > 0:
            chunk = min(remaining, random.randint(80, 180))
            await self._page.mouse.wheel(0, sign * chunk)
            remaining -= chunk
            await asyncio.sleep(random.uniform(0.08, 0.25))

    async def human_read_pause(self, min_sec: float = 1.5, max_sec: float = 4.0) -> None:
        """Simulate time spent reading — variable pause with occasional micro-jitter."""
        pause = random.uniform(min_sec, max_sec)
        # Split into micro-sleeps so event loop stays responsive
        slept = 0.0
        while slept < pause:
            chunk = min(pause - slept, random.uniform(0.3, 0.8))
            await asyncio.sleep(chunk)
            slept += chunk
            # Occasional tiny mouse drift during "reading"
            if random.random() < 0.3:
                await self.human_mouse_drift(steps=1)
