"""Browser-based authentication manager using Playwright for Microsoft Family Safety."""
import asyncio
import logging
import time
import uuid
from typing import Dict, Optional

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

_LOGGER = logging.getLogger(__name__)


class BrowserAuthManager:
    """Manages browser-based authentication sessions for Microsoft Family Safety."""

    MAX_CONCURRENT_SESSIONS = 1

    def __init__(
        self,
        auth_timeout: int = 300,
        language: str = "en-US",
        timezone: str = "Europe/Paris",
        storage=None,
    ):
        """Initialize browser auth manager."""
        self._sessions: Dict[str, Dict] = {}
        self._monitor_tasks: Dict[str, asyncio.Task] = {}
        self._playwright = None
        self._auth_timeout = auth_timeout
        self._language = language
        self._timezone = timezone
        self._storage = storage

    async def initialize(self):
        """Initialize Playwright."""
        try:
            self._playwright = await async_playwright().start()
            _LOGGER.info("Playwright initialized successfully")
        except Exception as e:
            _LOGGER.error(f"Failed to initialize Playwright: {e}")
            raise

    async def start_auth_session(self) -> str:
        """Start a new authentication session."""
        self._prune_old_sessions()

        active = [
            s for s in self._sessions.values() if s.get("status") == "authenticating"
        ]
        if len(active) >= self.MAX_CONCURRENT_SESSIONS:
            raise RuntimeError(
                "An authentication session is already in progress. "
                "Please wait or cancel it first."
            )

        session_id = str(uuid.uuid4())
        _LOGGER.info(f"Starting authentication session: {session_id}")

        browser = None
        context = None
        page = None
        try:
            # Launch browser (non-headless so user can interact via noVNC)
            browser = await self._playwright.chromium.launch(
                headless=False,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-gpu-compositing",
                    "--disable-gpu-sandbox",
                    "--disable-software-rasterizer",
                    "--disable-accelerated-2d-canvas",
                    "--disable-accelerated-video-decode",
                    "--disable-accelerated-video-encode",
                    "--disable-skia-runtime-opts",
                    "--disable-partial-raster",
                    "--disable-zero-copy",
                    "--disable-lcd-text",
                    "--disable-font-subpixel-positioning",
                    "--disable-features=VizDisplayCompositor,dbus,IsolateOrigins,"
                    "site-per-process,UseSkiaRenderer,TranslateUI",
                    "--disable-breakpad",
                    "--disable-component-update",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-background-networking",
                    "--disable-default-apps",
                    "--disable-extensions",
                    "--disable-sync",
                    "--no-first-run",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                    "--disable-background-timer-throttling",
                    "--memory-pressure-off",
                    "--disable-low-res-tiling",
                    "--ozone-platform=x11",
                ],
            )

            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale=self._language,
                timezone_id=self._timezone,
            )

            page = await context.new_page()

            self._sessions[session_id] = {
                "browser": browser,
                "context": context,
                "page": page,
                "status": "authenticating",
                "cookies": None,
                "error": None,
                "created_at": time.time(),
            }

            # Listen for new tabs/popups
            def on_page(new_page):
                _LOGGER.info("New tab detected, switching monitoring to new page")
                self._sessions[session_id]["page"] = new_page

            context.on("page", on_page)

            # Navigate to Microsoft Family Safety
            _LOGGER.info("Navigating to Microsoft Family Safety...")
            await page.goto(
                "https://account.microsoft.com/family",
                wait_until="load",
                timeout=30000,
            )

            # Start monitoring in background
            task = asyncio.create_task(self._monitor_authentication(session_id))
            task.add_done_callback(lambda t: self._on_monitor_done(session_id, t))
            self._monitor_tasks[session_id] = task

            return session_id

        except Exception as e:
            _LOGGER.error(f"Failed to start auth session: {e}")
            try:
                if page:
                    await page.close()
                if context:
                    await context.close()
                if browser:
                    await browser.close()
            except Exception as cleanup_err:
                _LOGGER.warning(f"Cleanup after failed session start: {cleanup_err}")
            raise

    async def _monitor_authentication(self, session_id: str):
        """Monitor authentication progress for Microsoft login."""
        session = self._sessions.get(session_id)
        if not session:
            return

        context: BrowserContext = session["context"]

        try:
            _LOGGER.info(f"Monitoring authentication for session {session_id}")

            await asyncio.sleep(5)  # Give initial page time to load

            start_time = asyncio.get_event_loop().time()
            authenticated = False
            last_url = None

            # Microsoft auth cookie names to detect successful login
            MS_AUTH_COOKIE_NAMES = {
                "MSPAuth",
                "MSPProf",
                "WLSSC",
                "RPSAuth",
                "RPSSecAuth",
            }

            while (asyncio.get_event_loop().time() - start_time) < self._auth_timeout:
                page: Page = session["page"]
                current_url = page.url

                if current_url != last_url:
                    _LOGGER.info(f"URL changed to: {current_url}")
                    last_url = current_url
                else:
                    _LOGGER.debug("Polling - URL unchanged")

                # Method 1: URL-based detection
                # Check if we've arrived at the Family Safety dashboard
                if "login.live.com" not in current_url and "login.microsoftonline.com" not in current_url:
                    if any(
                        domain in current_url
                        for domain in [
                            "account.microsoft.com/family",
                            "family.microsoft.com",
                        ]
                    ):
                        _LOGGER.info(
                            f"Authentication detected via URL: {current_url}"
                        )

                        # Navigate to family page to finalize cookies
                        _LOGGER.info(
                            "Navigating to account.microsoft.com/family to finalize cookies..."
                        )
                        try:
                            await page.goto(
                                "https://account.microsoft.com/family",
                                wait_until="load",
                                timeout=15000,
                            )
                            _LOGGER.info(
                                "Successfully navigated to Family Safety dashboard"
                            )
                            await asyncio.sleep(3)
                        except Exception as e:
                            _LOGGER.warning(
                                f"Failed to navigate to Family Safety: {e}"
                            )

                        authenticated = True
                        break

                # Method 2: Cookie-based detection (fallback)
                try:
                    cookies = await context.cookies()
                    ms_auth_cookies = [
                        c
                        for c in cookies
                        if c.get("name") in MS_AUTH_COOKIE_NAMES
                        and any(
                            d in c.get("domain", "")
                            for d in [".microsoft.com", ".live.com"]
                        )
                    ]
                    if len(ms_auth_cookies) >= 2:
                        _LOGGER.info(
                            f"Authentication detected via cookies "
                            f"({len(ms_auth_cookies)} auth cookies found: "
                            f"{[c['name'] for c in ms_auth_cookies]})"
                        )

                        # Navigate to family page to finalize cookies
                        try:
                            await page.goto(
                                "https://account.microsoft.com/family",
                                wait_until="load",
                                timeout=15000,
                            )
                            await asyncio.sleep(3)
                        except Exception as e:
                            _LOGGER.warning(
                                f"Failed to navigate to Family Safety: {e}"
                            )

                        authenticated = True
                        break
                except Exception as e:
                    _LOGGER.debug(f"Cookie check failed: {e}")

                await asyncio.sleep(2)

            if not authenticated:
                raise asyncio.TimeoutError("Authentication timeout")

            # Extract cookies
            _LOGGER.info("Authentication detected, extracting cookies...")
            cookies = await context.cookies()

            # Filter relevant Microsoft cookies
            ms_cookies = [
                c
                for c in cookies
                if any(
                    domain in c.get("domain", "")
                    for domain in [
                        "microsoft.com",
                        "live.com",
                        "account.microsoft.com",
                    ]
                )
            ]

            if not ms_cookies:
                raise Exception("No valid Microsoft cookies found")

            _LOGGER.info(f"Extracted {len(ms_cookies)} Microsoft cookies")

            # Save to shared storage
            if self._storage:
                await self._storage.save_cookies(ms_cookies)
            else:
                from app.storage.file_storage import SharedStorage

                storage = SharedStorage()
                await storage.save_cookies(ms_cookies)

            session["status"] = "completed"
            session["cookies"] = ms_cookies

            _LOGGER.info(
                f"Authentication completed successfully for session {session_id}"
            )

            await asyncio.sleep(2)
            await self._cleanup_session(session_id)

        except (asyncio.TimeoutError, PlaywrightTimeoutError):
            session["status"] = "timeout"
            session["error"] = (
                "Authentication timeout - user did not complete login in time"
            )
            _LOGGER.error(f"Authentication timeout for session {session_id}")
            await self._cleanup_session(session_id)

        except Exception as e:
            session["status"] = "error"
            session["error"] = str(e)
            _LOGGER.error(f"Authentication error for session {session_id}: {e}")
            await self._cleanup_session(session_id)

    def _on_monitor_done(self, session_id: str, task: asyncio.Task):
        """Handle monitor task completion."""
        self._monitor_tasks.pop(session_id, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            _LOGGER.error(f"Monitor task for session {session_id} failed: {exc}")

    def _prune_old_sessions(self, max_age: int = 3600):
        """Remove completed/errored sessions older than max_age seconds."""
        now = time.time()
        to_delete = [
            sid
            for sid, session in self._sessions.items()
            if session.get("status")
            in ("completed", "timeout", "error", "cleaned_up")
            and now - session.get("created_at", 0) > max_age
        ]
        for sid in to_delete:
            del self._sessions[sid]
        if to_delete:
            _LOGGER.debug(f"Pruned {len(to_delete)} old sessions")

    async def get_session_status(self, session_id: str) -> Dict:
        """Get status of authentication session."""
        session = self._sessions.get(session_id)
        if not session:
            return {"status": "not_found"}

        return {
            "status": session["status"],
            "has_cookies": session["cookies"] is not None,
            "error": session.get("error"),
            "cookie_count": len(session["cookies"]) if session["cookies"] else 0,
        }

    async def _cleanup_session(self, session_id: str):
        """Clean up session resources."""
        session = self._sessions.get(session_id)
        if session:
            try:
                if session.get("page"):
                    await session["page"].close()
                if session.get("context"):
                    await session["context"].close()
                if session.get("browser"):
                    await session["browser"].close()
                _LOGGER.info(f"Cleaned up session {session_id}")
            except Exception as e:
                _LOGGER.warning(f"Cleanup error for session {session_id}: {e}")
            finally:
                self._sessions[session_id] = {
                    "status": session.get("status", "cleaned_up"),
                    "created_at": session.get("created_at"),
                }

    async def browser_fetch(
        self, url: str, params: dict | None = None
    ) -> dict | None:
        """Make an API call through an authenticated browser session.

        Launches a temporary Chromium instance with saved cookies,
        navigates to the family page to establish the session,
        then uses fetch() from within the page to call the API.
        This ensures all JS-managed tokens (canary, CSRF, etc.) are present.

        Returns the JSON response dict on success, or a dict with
        ``__error`` key on failure (None only for setup issues).
        """
        if not self._storage:
            _LOGGER.warning("No storage configured for browser_fetch")
            return None

        try:
            cookies = await self._storage.load_cookies()
        except FileNotFoundError:
            _LOGGER.warning("No saved cookies for browser_fetch")
            return None

        if not cookies:
            return None

        _LOGGER.info(
            "browser_fetch: loaded %d cookies from storage", len(cookies)
        )

        browser = None
        try:
            browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )

            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale=self._language,
                timezone_id=self._timezone,
            )

            # Inject saved cookies
            await context.add_cookies(cookies)

            page = await context.new_page()

            # Navigate to family page to establish full session
            _LOGGER.info("browser_fetch: loading family page...")
            resp = await page.goto(
                "https://account.microsoft.com/family",
                wait_until="load",
                timeout=30000,
            )

            final_url = page.url
            resp_status = resp.status if resp else "no response"
            _LOGGER.info(
                "browser_fetch: family page status=%s, final_url=%s",
                resp_status,
                final_url,
            )

            # Detect login redirect — cookies are likely expired
            if "login.microsoftonline.com" in final_url or "login.live.com" in final_url or "login.microsoft.com" in final_url:
                _LOGGER.error(
                    "browser_fetch: redirected to login page (%s) — cookies are expired or invalid. "
                    "Please re-authenticate via the addon.",
                    final_url,
                )
                return {
                    "__error": True,
                    "status": 401,
                    "text": f"Redirected to login: {final_url}",
                    "code": "LOGIN_REDIRECT",
                }

            # Wait a bit for any JS-based session tokens to be set
            await page.wait_for_timeout(2000)

            # Build the full URL with params
            query = ""
            if params:
                from urllib.parse import urlencode
                query = "?" + urlencode(params)
            full_url = url + query

            # Use browser's fetch() — this includes all session tokens automatically
            _LOGGER.info("browser_fetch: calling %s", full_url)
            result = await page.evaluate(
                """async (url) => {
                    try {
                        const resp = await fetch(url, {
                            credentials: 'include',
                            headers: {
                                'Accept': 'application/json',
                                'X-Requested-With': 'XMLHttpRequest'
                            }
                        });
                        const text = await resp.text();
                        if (!resp.ok) {
                            return {
                                __error: true,
                                status: resp.status,
                                text: text,
                                headers: Object.fromEntries(resp.headers.entries())
                            };
                        }
                        try {
                            return JSON.parse(text);
                        } catch (e) {
                            return { __error: true, status: resp.status, text: text.substring(0, 500), message: 'Invalid JSON response' };
                        }
                    } catch (e) {
                        return { __error: true, message: e.message };
                    }
                }""",
                full_url,
            )

            await page.close()
            await context.close()

            if isinstance(result, dict) and result.get("__error"):
                _LOGGER.warning(
                    "browser_fetch error: status=%s text=%s",
                    result.get("status", "?"),
                    str(result.get("text", result.get("message", "")))[:500],
                )
                return result

            _LOGGER.info("browser_fetch: success for %s", full_url)
            return result

        except Exception as exc:
            _LOGGER.error("browser_fetch failed: %s", exc, exc_info=True)
            return None
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass

    async def cleanup(self):
        """Cleanup all resources."""
        _LOGGER.info("Cleaning up all sessions...")
        for session_id in list(self._sessions.keys()):
            await self._cleanup_session(session_id)

        if self._playwright:
            await self._playwright.stop()
            _LOGGER.info("Playwright stopped")
