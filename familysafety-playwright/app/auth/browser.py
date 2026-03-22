"""Browser-based authentication manager using Playwright for Microsoft Family Safety."""
import asyncio
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Dict

from playwright.async_api import (
    async_playwright,
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
        # Lock guards the Chrome profile directory — both auth and fetch must acquire it
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

    async def _wait_for_family_dashboard(self, page, timeout_ms: int = 20000) -> str:
        """Navigate to the family page and wait until we actually land on the dashboard.

        After auth or with a fresh profile, Microsoft often redirects through
        ``complete-client-signin-oauth-silent`` before landing on the real
        family dashboard.  This helper keeps trying until the URL contains
        ``account.microsoft.com/family`` **without** being an OAuth intermediate
        page, or until the timeout expires.

        Returns the final page URL.
        """
        import time

        _OAUTH_FRAGMENTS = (
            "complete-signin-oauth",
            "complete-client-signin",
            "oauth20_authorize",
        )
        _NOT_AUTH_PATTERNS = (
            "www.microsoft.com",  # marketing page redirect
        )

        deadline = time.monotonic() + timeout_ms / 1000
        attempts = 0

        while time.monotonic() < deadline:
            attempts += 1
            try:
                _LOGGER.info(
                    "_wait_for_family_dashboard: navigating (attempt %d)...",
                    attempts,
                )
                await page.goto(
                    "https://account.microsoft.com/family",
                    wait_until="load",
                    timeout=15000,
                )
            except Exception as e:
                _LOGGER.warning(
                    "_wait_for_family_dashboard: navigation error: %s", e
                )

            # Give JS redirects time to settle
            await asyncio.sleep(3)
            current_url = page.url
            _LOGGER.info(
                "_wait_for_family_dashboard: current URL: %s", current_url
            )

            # Check if we're on an OAuth intermediate page
            if any(frag in current_url for frag in _OAUTH_FRAGMENTS):
                _LOGGER.info(
                    "_wait_for_family_dashboard: on OAuth intermediate page, "
                    "waiting for JS redirect..."
                )
                # Wait for the JS redirect to complete
                try:
                    await page.wait_for_url(
                        "**/family*",
                        timeout=10000,
                        wait_until="load",
                    )
                except Exception:
                    pass
                current_url = page.url
                _LOGGER.info(
                    "_wait_for_family_dashboard: after wait, URL: %s",
                    current_url,
                )

            # Success: we're on account.microsoft.com/family (the real dashboard)
            if (
                "account.microsoft.com/family" in current_url
                and not any(frag in current_url for frag in _OAUTH_FRAGMENTS)
                and not any(pat in current_url for pat in _NOT_AUTH_PATTERNS)
            ):
                _LOGGER.info(
                    "_wait_for_family_dashboard: landed on family dashboard!"
                )
                return current_url

            # Not there yet — wait before retrying
            remaining = deadline - time.monotonic()
            if remaining > 3:
                _LOGGER.info(
                    "_wait_for_family_dashboard: not on dashboard yet, "
                    "retrying (%.0fs remaining)...",
                    remaining,
                )
                await asyncio.sleep(2)
            else:
                break

        final_url = page.url
        _LOGGER.warning(
            "_wait_for_family_dashboard: timed out, final URL: %s", final_url
        )
        return final_url

    def _remove_stale_singleton_lock(self):
        """Remove SingletonLock if no Chromium process is using the profile.

        Checks the PID stored in the lock file symlink target. If the process
        is not running, removes the lock. This handles crashes and restarts
        safely without killing a live browser.
        """
        lock_file = Path(_PROFILE_DIR) / "SingletonLock"
        if not lock_file.exists() and not lock_file.is_symlink():
            return

        try:
            # Chromium stores the lock as a symlink whose target contains the hostname and PID
            # Format: "hostname-PID"
            if lock_file.is_symlink():
                target = os.readlink(str(lock_file))
                parts = target.rsplit("-", 1)
                if len(parts) == 2:
                    try:
                        pid = int(parts[1])
                        # Check if process is still running
                        os.kill(pid, 0)
                        _LOGGER.warning(
                            "SingletonLock held by live process PID %d — not removing", pid
                        )
                        return
                    except (ValueError, ProcessLookupError, PermissionError):
                        pass  # PID not valid or process dead — safe to remove

            lock_file.unlink(missing_ok=True)
            _LOGGER.info("Removed stale SingletonLock from %s", _PROFILE_DIR)
        except Exception as e:
            _LOGGER.warning("Failed to check/remove SingletonLock: %s", e)

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

        # Acquire the browser lock to prevent conflict with browser_fetch
        await self._browser_lock.acquire()

        context = None
        page = None
        try:
            self._remove_stale_singleton_lock()

            # Launch persistent browser context (non-headless for VNC interaction)
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

            self._sessions[session_id] = {
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

            # Start monitoring in background (will release _browser_lock when done)
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
            # Release lock on failure
            if self._browser_lock.locked():
                self._browser_lock.release()
            raise

    async def _monitor_authentication(self, session_id: str):
        """Monitor authentication progress for Microsoft login."""
        session = self._sessions.get(session_id)
        if not session:
            if self._browser_lock.locked():
                self._browser_lock.release()
            return

        context: BrowserContext = session["context"]

        try:
            _LOGGER.info(f"Monitoring authentication for session {session_id}")

            await asyncio.sleep(5)  # Give initial page time to load

            start_time = asyncio.get_running_loop().time()
            authenticated = False
            last_url = None

            MS_AUTH_COOKIE_NAMES = {
                "MSPAuth",
                "MSPProf",
                "WLSSC",
                "RPSAuth",
                "RPSSecAuth",
            }

            while (asyncio.get_running_loop().time() - start_time) < self._auth_timeout:
                page: Page = session["page"]
                current_url = page.url

                if current_url != last_url:
                    _LOGGER.info(f"URL changed to: {current_url}")
                    last_url = current_url
                else:
                    _LOGGER.debug("Polling - URL unchanged")

                # Detect marketing page redirect (expired profile cookies)
                # Microsoft redirects to www.microsoft.com/.../family-safety when session is stale
                if "www.microsoft.com" in current_url and "family-safety" in current_url:
                    _LOGGER.info(
                        "Redirected to marketing page — profile session expired, "
                        "navigating to login..."
                    )
                    try:
                        await page.goto(
                            "https://account.microsoft.com/family",
                            wait_until="load",
                            timeout=15000,
                        )
                    except Exception:
                        pass
                    continue

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

                        _LOGGER.info(
                            "Navigating to account.microsoft.com/family to finalize session..."
                        )
                        final = await self._wait_for_family_dashboard(page)
                        _LOGGER.info(
                            "Session finalization done, URL: %s", final
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

                        final = await self._wait_for_family_dashboard(page)
                        _LOGGER.info(
                            "Session finalization done, URL: %s", final
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

        finally:
            # Always release the browser lock when auth is done
            if self._browser_lock.locked():
                self._browser_lock.release()

    def _on_monitor_done(self, session_id: str, task: asyncio.Task):
        """Handle monitor task completion."""
        self._monitor_tasks.pop(session_id, None)
        try:
            exc = task.exception()
        except (asyncio.CancelledError, asyncio.InvalidStateError):
            return
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
                if session.get("context"):
                    await session["context"].close()
                _LOGGER.info(f"Cleaned up session {session_id}")
            except Exception as e:
                _LOGGER.warning(f"Cleanup error for session {session_id}: {e}")
            finally:
                self._sessions[session_id] = {
                    "status": session.get("status", "cleaned_up"),
                    "cookies": session.get("cookies"),
                    "created_at": session.get("created_at"),
                }

    # _FETCH_JS accepts [url, canary] — canary is the cookie value (extracted
    # on the Python side because httpOnly cookies are not accessible via JS).
    # The __RequestVerificationToken header needs the value from the hidden
    # input in the DOM — that is the CSRF token the server validates against
    # the canary cookie.
    _FETCH_JS = """async ([url, canary]) => {
        try {
            // Extract CSRF token from hidden input in the page DOM
            const csrfInput = document.querySelector(
                'input[name="__RequestVerificationToken"]'
            );
            const csrfToken = csrfInput ? csrfInput.value : canary;

            const hdrs = {
                'Accept': 'application/json, text/plain, */*',
                'Content-Type': 'application/json',
                'X-Requested-With': 'XMLHttpRequest',
                'X-AMC-JsonMode': 'CamelCase'
            };
            if (csrfToken) {
                hdrs['__RequestVerificationToken'] = csrfToken;
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
                    text: text.substring(0, 500),
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
        """
        # If auth is in progress (lock held), return immediately
        # instead of blocking until the HA HTTP request times out
        if self._browser_lock.locked():
            _LOGGER.info("browser_fetch: browser lock held (auth in progress?), returning immediately")
            return {
                "__error": True, "status": 503, "code": "BROWSER_BUSY",
                "text": "Browser is busy (authentication may be in progress). Try again later.",
            }

        # Build the full URL with params
        from urllib.parse import urlencode
        query = "?" + urlencode(params) if params else ""
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
        # _browser_lock ensures mutual exclusion with auth AND other fetches
        async with self._browser_lock:
            context = None
            try:
                self._remove_stale_singleton_lock()

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

                # Navigate to family dashboard, handling OAuth intermediate
                # redirects and waiting until we're on the real page
                final_url = await self._wait_for_family_dashboard(page)

                # Detect login redirect — session expired, need re-auth
                _LOGIN_DOMAINS = (
                    "login.microsoftonline.com",
                    "login.live.com",
                    "login.microsoft.com",
                )
                if (
                    any(domain in final_url for domain in _LOGIN_DOMAINS)
                    or "www.microsoft.com" in final_url
                    or "account.microsoft.com/family" not in final_url
                ):
                    _LOGGER.error(
                        "browser_fetch: not on family dashboard (%s) "
                        "— session expired, need re-auth",
                        final_url,
                    )
                    return {
                        "__error": True,
                        "status": 401,
                        "text": f"Not on family dashboard: {final_url}",
                        "code": "LOGIN_REDIRECT",
                    }

                # Extra wait for JS-based session tokens (MSAL) to initialize
                await page.wait_for_timeout(2000)

                # Extract canary token from cookies (httpOnly — not accessible via JS)
                canary = ""
                for c in await context.cookies():
                    if c.get("name") == "canary":
                        canary = c["value"]
                        break

                # Check if page has the CSRF hidden input (for logging)
                has_csrf_input = await page.evaluate(
                    "!!document.querySelector('input[name=\"__RequestVerificationToken\"]')"
                )
                _LOGGER.info(
                    "browser_fetch: calling %s (canary=%s, csrf_input=%s)",
                    full_url,
                    "yes" if canary else "no",
                    "yes" if has_csrf_input else "no",
                )
                result = await page.evaluate(self._FETCH_JS, [full_url, canary])

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
