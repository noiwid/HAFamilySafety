"""Browser-based authentication manager using Playwright for Microsoft Family Safety."""
import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Dict, Optional

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

_LOGGER = logging.getLogger(__name__)

# Shared Chrome profile directory — persists across restarts
_PROFILE_DIR = "/share/familysafety/browser_profile"

# Common Chromium launch arguments
_CHROME_ARGS = [
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
]

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


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
        self._browser_lock = asyncio.Lock()

        # Ensure profile directory exists
        Path(_PROFILE_DIR).mkdir(parents=True, exist_ok=True)

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

        context = None
        page = None
        try:
            # Launch persistent browser context (non-headless for VNC interaction)
            # All state (cookies, localStorage, sessionStorage, cache) is saved
            # to _PROFILE_DIR and persists across restarts.
            context = await self._playwright.chromium.launch_persistent_context(
                _PROFILE_DIR,
                headless=False,
                args=_CHROME_ARGS + ["--ozone-platform=x11"],
                user_agent=_USER_AGENT,
                viewport={"width": 1280, "height": 800},
                locale=self._language,
                timezone_id=self._timezone,
            )

            # launch_persistent_context provides a default page
            page = context.pages[0] if context.pages else await context.new_page()

            self._sessions[session_id] = {
                "browser": None,  # No separate browser with persistent context
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
                if context:
                    await context.close()
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

                        # Navigate to family page to finalize session
                        _LOGGER.info(
                            "Navigating to account.microsoft.com/family to finalize session..."
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
                            # Wait for all JS-managed tokens to be set
                            await asyncio.sleep(5)
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

                        # Navigate to family page to finalize session
                        try:
                            await page.goto(
                                "https://account.microsoft.com/family",
                                wait_until="load",
                                timeout=15000,
                            )
                            # Wait for all JS-managed tokens to be set
                            await asyncio.sleep(5)
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

            # Extract cookies (still needed for HA integration direct API calls)
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

            # Save cookies to shared storage (for HA integration)
            if self._storage:
                await self._storage.save_cookies(ms_cookies)
            else:
                from app.storage.file_storage import SharedStorage

                storage = SharedStorage()
                await storage.save_cookies(ms_cookies)

            session["status"] = "completed"
            session["cookies"] = ms_cookies

            _LOGGER.info(
                f"Authentication completed successfully for session {session_id}. "
                f"Browser profile saved to {_PROFILE_DIR}"
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

        cookies = session.get("cookies")
        return {
            "status": session.get("status", "unknown"),
            "has_cookies": cookies is not None,
            "error": session.get("error"),
            "cookie_count": len(cookies) if cookies else 0,
        }

    async def _cleanup_session(self, session_id: str):
        """Clean up session resources."""
        session = self._sessions.get(session_id)
        if session:
            try:
                # With persistent context, closing context saves state and closes browser
                if session.get("context"):
                    await session["context"].close()
                _LOGGER.info(f"Cleaned up session {session_id}")
            except Exception as e:
                _LOGGER.warning(f"Cleanup error for session {session_id}: {e}")
            finally:
                self._sessions[session_id] = {
                    "status": session.get("status", "cleaned_up"),
                    "created_at": session.get("created_at"),
                }

    _FETCH_JS = """async (url) => {
        try {
            // Read canary cookie for the __requestverificationtoken header
            const cookies = document.cookie.split(';').map(c => c.trim());
            const canaryCookie = cookies.find(c => c.startsWith('canary='));
            const canary = canaryCookie ? canaryCookie.split('=').slice(1).join('=') : '';

            const hdrs = {
                'Accept': 'application/json, text/plain, */*',
                'X-Requested-With': '3742,HttpRequest',
                'X-Anc-Jsonmode': 'CamelCase'
            };
            if (canary) {
                hdrs['__requestverificationtoken'] = canary;
            }

            const resp = await fetch(url, {
                credentials: 'include',
                headers: hdrs
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
    }"""

    async def browser_fetch(
        self, url: str, params: dict | None = None
    ) -> dict | None:
        """Make an API call through an authenticated browser session.

        Uses a persistent browser context that shares the same Chrome profile
        as the authentication browser.  All session state (cookies, localStorage,
        MSAL tokens, etc.) is preserved across calls and restarts.

        Returns the JSON response dict on success, or a dict with
        ``__error`` key on failure.
        """
        # Wait for any active auth session to finish first
        active = [
            s for s in self._sessions.values()
            if s.get("status") == "authenticating"
        ]
        if active:
            _LOGGER.info(
                "browser_fetch: auth session in progress, waiting for it to complete..."
            )
            for _ in range(60):  # Wait up to 120 seconds
                await asyncio.sleep(2)
                still_active = [
                    s for s in self._sessions.values()
                    if s.get("status") == "authenticating"
                ]
                if not still_active:
                    _LOGGER.info("browser_fetch: auth session completed, proceeding")
                    break
            else:
                return {
                    "__error": True, "status": 503, "code": "AUTH_IN_PROGRESS",
                    "text": "Authentication session still in progress after waiting",
                }

        # Build the full URL with params
        query = ""
        if params:
            from urllib.parse import urlencode
            query = "?" + urlencode(params)
        full_url = url + query

        # Use persistent context — all session state is on disk
        return await self._persistent_context_fetch(full_url)

    async def _persistent_context_fetch(self, full_url: str) -> dict:
        """Fetch API data using a persistent browser context.

        Opens the same Chrome profile used during authentication so that
        cookies, localStorage (MSAL tokens), and all other browser state
        are available.  Navigates to the family page to let JS establish
        the session, then executes a fetch() from within the page.
        """
        # Prevent concurrent access to the shared profile directory
        async with self._browser_lock:
            context = None
            try:
                # Remove stale SingletonLock from previous crashes/restarts
                lock_file = Path(_PROFILE_DIR) / "SingletonLock"
                if lock_file.exists():
                    _LOGGER.info("browser_fetch: removing stale SingletonLock")
                    lock_file.unlink(missing_ok=True)

                _LOGGER.info("browser_fetch: opening persistent context from %s", _PROFILE_DIR)

                context = await self._playwright.chromium.launch_persistent_context(
                    _PROFILE_DIR,
                    headless=False,
                    args=_CHROME_ARGS + ["--ozone-platform=x11"],
                    user_agent=_USER_AGENT,
                    viewport={"width": 1280, "height": 800},
                    locale=self._language,
                    timezone_id=self._timezone,
                )

                page = context.pages[0] if context.pages else await context.new_page()

                # Navigate to family page — JS will establish the full session
                # using cookies + localStorage (MSAL tokens) from the profile
                _LOGGER.info("browser_fetch: navigating to family page...")
                try:
                    resp = await page.goto(
                        "https://account.microsoft.com/family",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                except Exception as nav_err:
                    _LOGGER.warning(
                        "browser_fetch: navigation error (will still try fetch): %s",
                        nav_err,
                    )
                    resp = None

                final_url = page.url
                resp_status = resp.status if resp else "no response"
                _LOGGER.info(
                    "browser_fetch: family page status=%s, final_url=%s",
                    resp_status,
                    final_url,
                )

                # Detect login redirect — session expired, need re-auth
                if any(
                    domain in final_url
                    for domain in (
                        "login.microsoftonline.com",
                        "login.live.com",
                        "login.microsoft.com",
                    )
                ):
                    _LOGGER.error(
                        "browser_fetch: redirected to login (%s) — session expired",
                        final_url,
                    )
                    return {
                        "__error": True,
                        "status": 401,
                        "text": f"Redirected to login: {final_url}",
                        "code": "LOGIN_REDIRECT",
                    }

                # Wait for JS-based session tokens (MSAL) to initialize
                await page.wait_for_timeout(5000)

                _LOGGER.info("browser_fetch: calling %s via page fetch", full_url)
                result = await page.evaluate(self._FETCH_JS, full_url)

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
                return {
                    "__error": True,
                    "status": 500,
                    "text": f"browser_fetch exception: {type(exc).__name__}: {exc}",
                    "code": "EXCEPTION",
                }
            finally:
                if context:
                    try:
                        await context.close()
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
