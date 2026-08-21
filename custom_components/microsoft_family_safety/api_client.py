"""Microsoft Family Safety API client.

The mobile API uses the refresh token supplied by pyfamilysafety.  Screen-time
schedule operations use Microsoft's private account web API.  Web cookies may
come either from the legacy Playwright add-on or from the native browser-assisted
Home Assistant authentication flow.
"""
from __future__ import annotations

import asyncio
import base64
from html import unescape
from http.cookies import SimpleCookie
import json
import logging
import re
import time
from typing import Any

import aiohttp
from yarl import URL

from ._pyfamilysafety_compat import authenticator_access_token_expired
from .const import DAY_KEYS

_LOGGER = logging.getLogger(__name__)

_BASE_URL = "https://mobileaggregator.family.microsoft.com/api"
_APP_VERSION = "v 1.26.0.1001"
_USER_AGENT = f"Family Safety-prod/({_APP_VERSION}) Android/33 google/Pixel 4 XL"
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0"
)


def _extract_request_verification_token(page: str) -> str | None:
    """Extract the CSRF token used by account.microsoft.com/family.

    The Playwright add-on obtains this exact value from the hidden DOM input.
    Keep several fallbacks for older Microsoft page variants.
    """
    patterns = (
        r'<input[^>]+name=["\']__RequestVerificationToken["\'][^>]+value=["\']([^"\']+)',
        r'<input[^>]+value=["\']([^"\']+)["\'][^>]+name=["\']__RequestVerificationToken["\']',
        r'"__RequestVerificationToken"\s*:\s*"([^"]+)"',
    )
    for pattern in patterns:
        match = re.search(pattern, page, re.IGNORECASE)
        if match:
            return unescape(match.group(1))
    return None


class FamilySafetyWebAPI:
    """Combined Microsoft Family mobile and private web API client."""

    WEB_API_BASE = "https://account.microsoft.com"

    def __init__(self, authenticator) -> None:
        self._authenticator = authenticator
        self._session: aiohttp.ClientSession | None = None
        self._web_cookies: list[dict[str, Any]] | None = None
        self._web_canary: str | None = None
        self._web_csrf: str | None = None
        self._web_session: aiohttp.ClientSession | None = None
        self._relationship_tokens: dict[str, tuple[str, int]] = {}
        self.last_web_error_code: str | None = None
        # Keep authentication health separate from endpoint-specific failures.
        # A private endpoint may return 401/403 while the account.microsoft.com
        # Family browser session itself is still authenticated.
        self.web_session_state: str = "unknown"
        self.web_session_last_checked: int | None = None
        self.web_session_last_http_status: int | None = None
        self.screentime_policy_status: str = "unknown"
        self.screentime_policy_source: str | None = None
        # Endpoint health is intentionally separate from browser-login health.
        # A Family dashboard can still be authenticated while one private API
        # endpoint is timing out or rejecting a request.
        self.web_api_state: str = "unknown"
        self.web_api_last_checked: int | None = None
        self.web_api_last_http_status: int | None = None
        self.web_api_last_endpoint: str | None = None
        # A valid Microsoft account session is not enough for the private
        # Family API.  The Family SPA issues its own request-verification token
        # in /family/home; keep that context separate from /account health.
        self.family_context_state: str = "unknown"
        self.family_context_last_checked: int | None = None
        self.family_context_last_http_status: int | None = None
        self.family_context_last_path: str | None = None
        self.family_token_source: str | None = None
        self._family_referer: str = f"{self.WEB_API_BASE}/family/home"

    def set_web_cookies(
        self, cookies: list[dict[str, Any]], *,
        family_token: str | None = None,
        family_referer: str | None = None,
    ) -> None:
        """Set browser cookies and optional browser-captured Family API context."""
        same_cookies = cookies == self._web_cookies
        self._web_cookies = cookies
        self._web_canary = None
        self._web_csrf = family_token or (self._web_csrf if same_cookies else None)
        self._relationship_tokens.clear()
        self.last_web_error_code = None
        self.web_session_state = "unknown" if cookies else "missing"
        self.web_session_last_checked = None
        self.web_session_last_http_status = None
        self.screentime_policy_status = "unknown"
        self.screentime_policy_source = None
        self.web_api_state = "unknown"
        self.web_api_last_checked = None
        self.web_api_last_http_status = None
        self.web_api_last_endpoint = None
        if family_token:
            self._family_referer = family_referer or f"{self.WEB_API_BASE}/family/home"
            self.family_context_state = "ready"
            self.family_context_last_checked = int(time.time())
            self.family_context_last_http_status = 200
            self.family_context_last_path = URL(self._family_referer).path
            self.family_token_source = "auth_proxy"
        else:
            self.family_context_state = "unknown" if cookies else "missing"
            self.family_context_last_checked = None
            self.family_context_last_http_status = None
            self.family_context_last_path = None
            self.family_token_source = None
            self._family_referer = f"{self.WEB_API_BASE}/family/home"
        if self._web_session and not self._web_session.closed:
            session = self._web_session
            self._web_session = None
            try:
                asyncio.get_running_loop().create_task(session.close())
            except RuntimeError:
                pass
        _LOGGER.info(
            "Web API cookies configured (%d cookies; family_context=%s token_source=%s)",
            len(cookies or []), self.family_context_state, self.family_token_source,
        )

    @property
    def has_web_cookies(self) -> bool:
        """Return True if web cookies are available."""
        return bool(self._web_cookies)

    def export_web_cookies(self) -> list[dict[str, Any]]:
        """Export the currently effective browser cookie jar.

        Microsoft can rotate authentication cookies through Set-Cookie responses.
        The exported representation is safe to persist in Home Assistant storage
        and can be restored after a restart.
        """
        if self._web_session is None or self._web_session.closed:
            return list(self._web_cookies or [])
        exported: list[dict[str, Any]] = []
        for morsel in self._web_session.cookie_jar:
            domain = str(morsel["domain"] or "")
            if not domain:
                continue
            item: dict[str, Any] = {
                "name": morsel.key,
                "value": morsel.value,
                "domain": domain,
                "path": str(morsel["path"] or "/"),
                "secure": bool(morsel["secure"]),
                "httpOnly": bool(morsel["httponly"]),
            }
            if morsel["expires"]:
                item["expires"] = str(morsel["expires"])
            exported.append(item)
        exported.sort(key=lambda item: (item["domain"], item["path"], item["name"]))
        return exported

    def export_family_context(self) -> tuple[str | None, str | None]:
        """Return captured Family antiforgery context for persistence."""
        return self._web_csrf, self._family_referer

    def sync_web_cookies_from_session(self) -> bool:
        """Capture any Microsoft Set-Cookie rotations from the live session."""
        exported = self.export_web_cookies()
        if not exported or exported == (self._web_cookies or []):
            return False
        self._web_cookies = exported
        return True

    async def _ensure_session(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30, connect=10)
            )

    async def _ensure_auth(self) -> None:
        """Use the single pyfamilysafety authenticator for mobile API auth.

        Keeping exactly one refresh-token chain prevents two independent token
        grants from rotating the same Microsoft refresh token out from under each
        other.
        """
        refresh_token = getattr(self._authenticator, "refresh_token", None)
        if not refresh_token:
            raise FamilySafetyWebAPIError("No refresh token available")
        try:
            if authenticator_access_token_expired(self._authenticator):
                await self._authenticator.perform_refresh()
        except Exception as err:
            raise FamilySafetyWebAPIError(f"Mobile token refresh failed: {err}") from err

    def _build_headers(self) -> dict[str, str]:
        return {
            "Authorization": self._authenticator.access_token,
            "User-Agent": _USER_AGENT,
            "X-Requested-With": "com.microsoft.familysafety",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        json_data: dict | None = None,
        params: dict | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict | list | None:
        """Make an authenticated request to the mobile API."""
        await self._ensure_session()
        await self._ensure_auth()
        assert self._session is not None
        url = f"{_BASE_URL}{path}"
        headers = self._build_headers()
        if extra_headers:
            headers.update(extra_headers)

        async def do_request() -> dict | list | None:
            async with self._session.request(
                method, url, headers=headers, json=json_data, params=params
            ) as resp:
                return await self._handle_response(resp)

        try:
            try:
                return await do_request()
            except FamilySafetyWebAPIError as err:
                if "status 401" not in str(err) and "status 403" not in str(err):
                    raise
                await self._authenticator.perform_refresh()
                headers.update(self._build_headers())
                return await do_request()
        except aiohttp.ClientError as err:
            raise FamilySafetyWebAPIError(f"Mobile API request failed: {err}") from err

    async def _handle_response(self, resp: aiohttp.ClientResponse) -> dict | list | None:
        if resp.status in (200, 201, 204):
            text = await resp.text()
            if not text:
                return None
            try:
                return await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                return None
        text = await resp.text()
        raise FamilySafetyWebAPIError(
            f"API request failed with status {resp.status}: {text[:200]}"
        )

    # ------------------------------------------------------------------
    # Private web API
    # ------------------------------------------------------------------

    def _build_cookie_jar(self) -> aiohttp.CookieJar:
        """Build a cookie jar without losing domain/path/secure semantics."""
        jar = aiohttp.CookieJar(unsafe=True)
        for cookie in self._web_cookies or []:
            name = str(cookie.get("name") or "")
            value = str(cookie.get("value") or "")
            domain = str(cookie.get("domain") or "")
            path = str(cookie.get("path") or "/")
            if not name or not domain:
                continue

            simple = SimpleCookie()
            simple[name] = value
            morsel = simple[name]
            morsel["domain"] = domain
            morsel["path"] = path
            if cookie.get("secure"):
                morsel["secure"] = True
            if cookie.get("httpOnly") or cookie.get("httponly"):
                morsel["httponly"] = True

            # Supply a host inside the cookie's domain. CookieJar then retains
            # the Domain attribute rather than turning it into a host-only cookie.
            host = domain.lstrip(".")
            jar.update_cookies(simple, response_url=URL(f"https://{host}{path}"))
        return jar

    def _get_web_session(self) -> aiohttp.ClientSession:
        if self._web_session is None or self._web_session.closed:
            self._web_session = aiohttp.ClientSession(
                cookie_jar=self._build_cookie_jar(),
                timeout=aiohttp.ClientTimeout(
                    total=20, connect=8, sock_connect=8, sock_read=15
                ),
            )
        return self._web_session

    def _cookie_value(self, name: str) -> str | None:
        for cookie in self._web_cookies or []:
            if str(cookie.get("name", "")).lower() == name.lower():
                return str(cookie.get("value") or "") or None
        return None

    def _mark_web_session(self, state: str, http_status: int | None = None) -> None:
        """Record browser-session health without exposing credentials."""
        self.web_session_state = state
        self.web_session_last_checked = int(time.time())
        self.web_session_last_http_status = http_status

    def _mark_web_api(
        self, state: str, endpoint: str, http_status: int | None = None
    ) -> None:
        """Record private Family web API health separately from login health."""
        self.web_api_state = state
        self.web_api_last_checked = int(time.time())
        self.web_api_last_http_status = http_status
        self.web_api_last_endpoint = endpoint

    @staticmethod
    def _is_authentication_url(value: str | URL | None) -> bool:
        """Return True only for destinations that actually represent login.

        account.microsoft.com/family may legitimately redirect to a public
        Microsoft Family Safety landing page.  That is a routing/product-page
        change, not proof that the captured Microsoft account session expired.
        """
        if not value:
            return False
        try:
            url = value if isinstance(value, URL) else URL(str(value))
        except ValueError:
            return False
        host = (url.host or "").lower()
        path = (url.path or "").lower()
        if host in {
            "login.live.com",
            "login.microsoft.com",
            "login.microsoftonline.com",
            "device.login.microsoftonline.com",
            "login-us.microsoftonline.com",
            "logincert.microsoftonline.com",
        }:
            return True
        return host == "account.microsoft.com" and path.startswith("/auth/")

    async def async_check_web_session(self) -> bool:
        """Actively verify the stored Microsoft account browser session.

        Cookie presence alone is not proof of authentication.  Verify the same
        authenticated account destination that the native browser flow itself
        accepts (/account).  Family private-API reachability is tracked
        independently in web_api_state and must not be conflated with login
        health.
        """
        if not self._web_cookies:
            self._mark_web_session("missing")
            return False

        # Preserve a Family antiforgery token captured by the real browser.
        # The supplied 2026-08-12 HAR shows that the hidden token from the
        # Family landing page is reused verbatim by successful /family/api
        # requests. Previous builds cleared it here before the first API call
        # and then tried to rebuild the Family SPA context with aiohttp.
        had_family_token = bool(self._web_csrf or self._web_canary)
        family_source = self.family_token_source
        family_referer_path = (
            URL(self._family_referer).path if self._family_referer else None
        )
        await self._warm_web_session()
        authenticated = self.web_session_state == "authenticated"

        if authenticated and had_family_token:
            _LOGGER.debug(
                "Microsoft account session verified while preserving browser-captured "
                "Family context: token_source=%s token_present=%s token_length=%d "
                "referer_path=%s",
                family_source,
                bool(self._web_csrf or self._web_canary),
                len((self._web_csrf or self._web_canary) or ""),
                family_referer_path,
            )
        elif self.web_session_state in ("missing", "expired"):
            # Confirmed login loss invalidates browser-derived Family context.
            self._web_csrf = None
            self._web_canary = None
            self.family_context_state = self.web_session_state
            self.family_token_source = None

        return authenticated

    async def _warm_web_session(self) -> str | None:
        """Verify the Microsoft account login without sourcing a Family API token.

        The /account page can contain an antiforgery value, but the supplied
        browser capture shows that private /family/api requests use the hidden
        __RequestVerificationToken emitted by the Family SPA itself.  Treat the
        account page only as an authentication-health probe.
        """
        if not self._web_cookies:
            self._mark_web_session("missing")
            return None
        headers = {
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "de,de-DE;q=0.9,en;q=0.8,en-US;q=0.6",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": _BROWSER_USER_AGENT,
        }
        try:
            session = self._get_web_session()
            async with session.get(
                f"{self.WEB_API_BASE}/account",
                headers=headers,
                allow_redirects=True,
            ) as resp:
                final_url = URL(str(resp.url))
                page = await resp.text()
                rotated = self.sync_web_cookies_from_session()

                if resp.status in (401, 403) or self._is_authentication_url(final_url):
                    self.last_web_error_code = "LOGIN_REDIRECT"
                    self._mark_web_session("expired", resp.status)
                    _LOGGER.warning(
                        "Microsoft account session probe requires authentication: "
                        "final_host=%s final_path=%s status=%s cookies=%d rotated=%s",
                        final_url.host,
                        final_url.path,
                        resp.status,
                        len(self._web_cookies or []),
                        rotated,
                    )
                    return None

                if (
                    resp.status != 200
                    or final_url.host != "account.microsoft.com"
                    or not final_url.path.startswith("/account")
                ):
                    self.last_web_error_code = "UNEXPECTED_SESSION_DESTINATION"
                    self._mark_web_session("error", resp.status)
                    _LOGGER.warning(
                        "Microsoft account session probe ended at an unexpected "
                        "destination: final_host=%s final_path=%s status=%s "
                        "cookies=%d rotated=%s",
                        final_url.host,
                        final_url.path,
                        resp.status,
                        len(self._web_cookies or []),
                        rotated,
                    )
                    return None

                # Diagnostic only.  Do NOT use a token from /account for Family
                # API calls; it is a different antiforgery context.
                account_token_present = bool(_extract_request_verification_token(page))
                self.last_web_error_code = None
                self._mark_web_session("authenticated", resp.status)
                _LOGGER.debug(
                    "Microsoft account session verified: final_host=%s "
                    "final_path=%s status=%s cookies=%d rotated=%s "
                    "account_antiforgery_present=%s",
                    final_url.host,
                    final_url.path,
                    resp.status,
                    len(self._web_cookies or []),
                    rotated,
                    account_token_present,
                )
                return None
        except asyncio.TimeoutError:
            self.last_web_error_code = "TIMEOUT"
            self._mark_web_session("error")
            _LOGGER.warning("Microsoft account session check timed out")
            return None
        except (aiohttp.ClientError, ValueError) as err:
            self.last_web_error_code = "NETWORK_ERROR"
            self._mark_web_session("error")
            _LOGGER.debug("Microsoft account session check failed: %r", err)
            return None

    async def _warm_family_context(self) -> str | None:
        """Load the Family SPA and obtain its request-verification token.

        The browser capture uses the hidden token from /family/home (or the
        Windows direct landing page) on successful /family/api requests.  This
        token is distinct from antiforgery data exposed by /account.
        """
        if not self._web_cookies:
            self.family_context_state = "missing"
            return None

        # Clear stale Family antiforgery state before rebuilding it.
        self._web_csrf = None
        self._web_canary = None
        headers = {
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "de,de-DE;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": _BROWSER_USER_AGENT,
        }
        candidates = (
            (
                f"{self.WEB_API_BASE}/family/windows/home/direct"
                "?fref=coldstartv2&refd=account.microsoft.com"
            ),
            f"{self.WEB_API_BASE}/family/home",
        )
        session = self._get_web_session()
        last_destination: URL | None = None
        last_status: int | None = None
        try:
            for attempt, candidate in enumerate(candidates, 1):
                async with session.get(candidate, headers=headers, allow_redirects=True) as resp:
                    final_url = URL(str(resp.url))
                    last_destination = final_url
                    last_status = resp.status
                    page = await resp.text()
                    rotated = self.sync_web_cookies_from_session()
                    self.family_context_last_checked = int(time.time())
                    self.family_context_last_http_status = resp.status
                    self.family_context_last_path = final_url.path

                    if resp.status in (401, 403) or self._is_authentication_url(final_url):
                        self.family_context_state = "auth_required"
                        self.family_token_source = None
                        self.last_web_error_code = "FAMILY_CONTEXT_AUTH_REQUIRED"
                        # A Family-SPA bootstrap redirect is not proof that the
                        # independently verified /account session has expired.
                        # Keep web_session_state untouched and surface this as a
                        # Family-context problem instead of starting a reauth loop.
                        _LOGGER.warning(
                            "Microsoft Family context requires browser authentication: "
                            "attempt=%d final_host=%s final_path=%s status=%s rotated=%s; "
                            "account-session auth state left unchanged",
                            attempt, final_url.host, final_url.path, resp.status, rotated,
                        )
                        return None

                    if (
                        resp.status == 200
                        and final_url.host == "account.microsoft.com"
                        and final_url.path.startswith("/family")
                    ):
                        csrf = _extract_request_verification_token(page)
                        if csrf:
                            self._web_csrf = csrf
                            self._family_referer = str(final_url)
                            self.family_context_state = "ready"
                            self.family_token_source = "family_page"
                            self.last_web_error_code = None
                            _LOGGER.debug(
                                "Microsoft Family API context ready: attempt=%d "
                                "final_path=%s status=%s cookies=%d rotated=%s "
                                "token_source=family_page token_length=%d",
                                attempt,
                                final_url.path,
                                resp.status,
                                len(self._web_cookies or []),
                                rotated,
                                len(csrf),
                            )
                            return csrf

                        _LOGGER.debug(
                            "Microsoft Family page loaded but no request-verification token "
                            "was found: attempt=%d final_path=%s status=%s",
                            attempt,
                            final_url.path,
                            resp.status,
                        )
                        continue

                    _LOGGER.debug(
                        "Microsoft Family context candidate ended elsewhere: "
                        "attempt=%d final_host=%s final_path=%s status=%s rotated=%s",
                        attempt,
                        final_url.host,
                        final_url.path,
                        resp.status,
                        rotated,
                    )

            self.family_context_state = "unavailable"
            self.family_token_source = None
            self.last_web_error_code = "FAMILY_CONTEXT_UNAVAILABLE"
            _LOGGER.warning(
                "Could not establish Microsoft Family API context: "
                "last_host=%s last_path=%s status=%s cookies=%d; "
                "account-session auth state left unchanged",
                last_destination.host if last_destination else None,
                last_destination.path if last_destination else None,
                last_status,
                len(self._web_cookies or []),
            )
            return None
        except asyncio.TimeoutError:
            self.family_context_state = "timeout"
            self.family_token_source = None
            self.last_web_error_code = "FAMILY_CONTEXT_TIMEOUT"
            _LOGGER.warning("Microsoft Family API context warm-up timed out")
            return None
        except (aiohttp.ClientError, ValueError) as err:
            self.family_context_state = "error"
            self.family_token_source = None
            self.last_web_error_code = "FAMILY_CONTEXT_NETWORK_ERROR"
            _LOGGER.warning("Microsoft Family API context warm-up failed: %r", err)
            return None

    @staticmethod
    def _decode_relationship_token(value: str) -> dict[str, Any] | None:
        """Decode only enough JWT metadata to associate a Family token.

        The token is still validated cryptographically by Microsoft.  Local
        decoding is used solely to select the token whose target PUID matches
        the child.  Token values are never logged.
        """
        if not isinstance(value, str) or value.count(".") != 2:
            return None
        try:
            payload = value.split(".", 2)[1]
            payload += "=" * ((4 - len(payload) % 4) % 4)
            decoded = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(decoded, dict) or decoded.get("iss") != "urn:microsoft:family":
            return None
        return decoded

    def _cache_relationship_tokens(self, payload: object) -> None:
        """Harvest per-child Family relationship JWTs from web JSON.

        The supplied browser HAR shows that modern device/app endpoints send
        X-JwtFamilyRelationshipToken.  The token is already present in data
        loaded before those requests; this recursive scanner deliberately does
        not depend on Microsoft's private JSON property name.
        """
        def walk(value: object):
            if isinstance(value, dict):
                for item in value.values():
                    yield from walk(item)
            elif isinstance(value, list):
                for item in value:
                    yield from walk(item)
            elif isinstance(value, str):
                yield value

        for value in walk(payload):
            claims = self._decode_relationship_token(value)
            if claims is None:
                continue
            child_id = claims.get("t-puid")
            if child_id is None:
                continue
            try:
                expires = int(claims.get("exp") or 0)
            except (TypeError, ValueError):
                expires = 0
            self._relationship_tokens[str(child_id)] = (value, expires)

    async def _get_relationship_token(self, child_id: str) -> str | None:
        cached = self._relationship_tokens.get(str(child_id))
        if cached and (not cached[1] or cached[1] > int(time.time()) + 60):
            return cached[0]

        # These are the non-privileged calls observed immediately before the
        # browser starts sending X-JwtFamilyRelationshipToken.  Scan both so
        # the implementation remains tolerant of Microsoft moving the token.
        for url, params in (
            (f"{self.WEB_API_BASE}/family/api/roster", None),
            (
                f"{self.WEB_API_BASE}/family/api/landing-page-feeds",
                {"memberIdList": str(child_id)},
            ),
        ):
            payload = await self._web_request("GET", url, params=params)
            if payload is not None:
                self._cache_relationship_tokens(payload)
            cached = self._relationship_tokens.get(str(child_id))
            if cached and (not cached[1] or cached[1] > int(time.time()) + 60):
                return cached[0]
        return None

    async def _web_request(
        self,
        method: str,
        url: str,
        params: dict | None = None,
        json_data: dict | None = None,
        relationship_child_id: str | None = None,
    ) -> dict | list | None:
        """Call the private Family web API using the captured browser session."""
        if not self._web_cookies:
            return None
        if not self._web_csrf and not self._web_canary:
            token = await self._warm_family_context()
            if token is None:
                endpoint = URL(url).path
                self._mark_web_api("family_context_unavailable", endpoint)
                return None

        token = self._web_csrf or self._web_canary
        # Match the successful browser requests more closely.  In particular,
        # GET calls do not send Origin/Content-Type, while writes do.
        referer = self._family_referer or f"{self.WEB_API_BASE}/family/home"
        if params and params.get("childId"):
            child = str(params["childId"])
            endpoint_path = URL(url).path
            if "web-browsing" in endpoint_path or "app" in endpoint_path:
                referer = f"{self.WEB_API_BASE}/family/settings/windows/{child}/apps"
            elif "screen-time" in endpoint_path or endpoint_path.endswith("/st"):
                referer = f"{self.WEB_API_BASE}/family/settings/windows/{child}/devices"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "de,de-DE;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "X-Requested-With": "XMLHttpRequest",
            "X-AMC-JsonMode": "CamelCase",
            "Referer": referer,
            "User-Agent": _BROWSER_USER_AGENT,
        }
        if method.upper() not in {"GET", "HEAD"}:
            headers["Content-Type"] = "application/json"
            headers["Origin"] = "https://account.microsoft.com"
        if token:
            headers["__RequestVerificationToken"] = token
        if relationship_child_id is not None:
            relationship_token = await self._get_relationship_token(relationship_child_id)
            if not relationship_token:
                _LOGGER.debug(
                    "No Family relationship token available for child-specific web request"
                )
                return None
            headers["X-JwtFamilyRelationshipToken"] = relationship_token

        session = self._get_web_session()
        endpoint = URL(url).path
        started = time.monotonic()
        try:
            cookie_count = len(session.cookie_jar.filter_cookies(URL(url)))
        except Exception:
            cookie_count = -1
        _LOGGER.debug(
            "Family web API request prepared: method=%s endpoint=%s "
            "family_context=%s token_source=%s token_present=%s "
            "token_length=%d referer_path=%s cookie_count=%d",
            method.upper(),
            endpoint,
            self.family_context_state,
            self.family_token_source,
            bool(token),
            len(token or ""),
            URL(referer).path,
            cookie_count,
        )
        try:
            async with session.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_data,
                allow_redirects=False,
            ) as resp:
                if resp.status in (200, 201, 204):
                    self.last_web_error_code = None
                    self._mark_web_session("authenticated", resp.status)
                    self._mark_web_api("ok", endpoint, resp.status)
                    text = await resp.text()
                    self.sync_web_cookies_from_session()
                    if not text:
                        return {"success": True, "status": resp.status}
                    try:
                        payload = await resp.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError):
                        return {"success": True, "status": resp.status}
                    self._cache_relationship_tokens(payload)
                    return payload
                if resp.status in (301, 302, 303, 307, 308):
                    self.sync_web_cookies_from_session()
                    location = resp.headers.get("Location")
                    try:
                        destination = URL(location) if location else None
                        if destination is not None and not destination.is_absolute():
                            destination = URL(url).join(destination)
                    except ValueError:
                        destination = None
                    if self._is_authentication_url(destination):
                        self.last_web_error_code = "LOGIN_REDIRECT"
                        self._mark_web_session("expired", resp.status)
                        self._mark_web_api("auth_redirect", endpoint, resp.status)
                        _LOGGER.warning(
                            "Family web API redirected to authentication: "
                            "endpoint=%s destination_host=%s destination_path=%s",
                            endpoint,
                            destination.host if destination else None,
                            destination.path if destination else None,
                        )
                    else:
                        self.last_web_error_code = "HTTP_REDIRECT"
                        self._mark_web_api("redirect", endpoint, resp.status)
                        _LOGGER.warning(
                            "Family web API returned a non-authentication redirect: "
                            "endpoint=%s destination_host=%s destination_path=%s; "
                            "leaving web-session auth state unchanged",
                            endpoint,
                            destination.host if destination else None,
                            destination.path if destination else None,
                        )
                    return None
                body = await resp.text()
                if resp.status in (401, 403):
                    self.last_web_error_code = "AUTH_ERROR"
                    self._mark_web_api("auth_error", endpoint, resp.status)
                    try:
                        applicable_cookie_meta = sorted(
                            (m.key, str(m["domain"] or ""), str(m["path"] or "/"))
                            for m in session.cookie_jar
                            if m.key in session.cookie_jar.filter_cookies(URL(url))
                        )
                    except Exception:
                        applicable_cookie_meta = []
                    _LOGGER.debug(
                        "Family web API applicable cookie metadata on auth rejection: %s",
                        applicable_cookie_meta,
                    )
                    _LOGGER.warning(
                        "Family web API authentication rejected: status=%s endpoint=%s "
                        "token_source=%s token_present=%s token_length=%d "
                        "referer_path=%s cookie_count=%d body=%s",
                        resp.status,
                        endpoint,
                        self.family_token_source,
                        bool(token),
                        len(token or ""),
                        URL(referer).path,
                        cookie_count,
                        body[:200],
                    )
                else:
                    self.last_web_error_code = f"HTTP_{resp.status}"
                    self._mark_web_api("http_error", endpoint, resp.status)
                    _LOGGER.warning("Web API error %s on %s: %s", resp.status, endpoint, body[:200])
                return None
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - started
            self.last_web_error_code = "TIMEOUT"
            self._mark_web_api("timeout", endpoint)
            # A private endpoint timeout is not proof that the Microsoft browser
            # login expired. Preserve web_session_state from the independent
            # /family probe and expose endpoint health separately.
            _LOGGER.warning(
                "Family web API request timed out after %.1fs: %s %s",
                elapsed,
                method,
                endpoint,
            )
            return None
        except aiohttp.ClientError as err:
            self.last_web_error_code = "NETWORK_ERROR"
            self._mark_web_api("network_error", endpoint)
            _LOGGER.warning("Web API request failed on %s: %s", endpoint, err)
            return None

    @staticmethod
    def _normalize_screentime_policy(payload: object, child_id: str) -> dict | None:
        """Normalize current Family web screen-time payloads.

        Microsoft exposes the schedule in several response shapes.  Prefer a
        policy associated with the requested child, but also accept a single
        unambiguous policy anywhere in the response.  This is intentionally
        tolerant because landing-page-feeds has changed its member wrapper
        fields over time while the actual timeRestrictions/dailyRestrictions
        object has remained stable.
        """
        if not isinstance(payload, (dict, list)):
            return None

        def as_policy(value: object) -> dict | None:
            if not isinstance(value, dict):
                return None
            daily = value.get("dailyRestrictions") or value.get("DailyRestrictions")
            if not isinstance(daily, dict) or not daily:
                return None
            normalized = dict(value)
            normalized["dailyRestrictions"] = daily
            if "isEnabled" not in normalized:
                if "enabled" in normalized:
                    normalized["isEnabled"] = normalized.get("enabled")
                elif "Enabled" in normalized:
                    normalized["isEnabled"] = normalized.get("Enabled")
            return normalized

        child = str(child_id)

        # Prefer the exact landing-page-feeds shape confirmed by the supplied
        # browser HAR before falling back to generic recursive discovery.
        # This was the path that originally populated the weekday entities.
        if isinstance(payload, dict):
            for list_key in ("exceptionRequestsV0", "exceptionRequests"):
                members = payload.get(list_key)
                if not isinstance(members, list):
                    continue
                for member in members:
                    if not isinstance(member, dict):
                        continue
                    member_id = (
                        member.get("id")
                        or member.get("childId")
                        or member.get("memberId")
                        or member.get("userId")
                    )
                    if str(member_id or "") != child:
                        continue
                    for key in ("timeRestrictions", "TimeRestrictions"):
                        if policy := as_policy(member.get(key)):
                            return policy

        id_keys = (
            "id", "Id", "childId", "ChildId", "memberId", "MemberId",
            "userId", "UserId", "puid", "Puid", "user_id", "member_id",
        )
        matched: list[dict] = []
        candidates: list[dict] = []
        seen: set[int] = set()

        def add_candidate(policy: dict, owner: dict | None = None) -> None:
            marker = id(policy)
            if marker in seen:
                return
            seen.add(marker)
            candidates.append(policy)
            if owner:
                for key in id_keys:
                    raw = owner.get(key)
                    if raw is not None and str(raw) == child:
                        matched.append(policy)
                        break

        def walk(value: object, owner: dict | None = None) -> None:
            if isinstance(value, dict):
                if policy := as_policy(value):
                    add_candidate(policy, owner or value)
                # timeRestrictions is usually owned by the surrounding member
                # object, so preserve that owner while descending into it.
                for key, item in value.items():
                    next_owner = value if key in ("timeRestrictions", "TimeRestrictions") else owner
                    walk(item, next_owner)
            elif isinstance(value, list):
                for item in value:
                    walk(item, owner)

        walk(payload)
        if matched:
            return matched[0]
        if len(candidates) == 1:
            return candidates[0]

        # Retain the old direct/wrapper behavior as a final deterministic
        # fallback for response shapes where recursive ownership is ambiguous.
        if isinstance(payload, dict):
            if policy := as_policy(payload):
                return policy
            for key in ("timeRestrictions", "TimeRestrictions", "data", "Data"):
                candidate = payload.get(key)
                if policy := as_policy(candidate):
                    return policy
                if isinstance(candidate, dict):
                    for nested_key in ("timeRestrictions", "TimeRestrictions"):
                        if policy := as_policy(candidate.get(nested_key)):
                            return policy
        return None

    async def get_screentime_policy(
        self, child_id: str, platform: str = "Windows"
    ) -> dict | None:
        """Get and normalize a child's screen-time policy via the Family web API.

        The supplied browser HAR and the earlier working integration build both
        confirm landing-page-feeds as a source of dailyRestrictions. Prefer it
        first so a slow/unstable /st endpoint cannot prevent weekday entities
        from being populated. /st remains a compatibility fallback.
        """
        self.screentime_policy_status = "checking"
        self.screentime_policy_source = None
        if not self._web_cookies:
            self.screentime_policy_status = "missing_session"
            return None

        feed = await self._web_request(
            "GET",
            f"{self.WEB_API_BASE}/family/api/landing-page-feeds",
            params={"memberIdList": str(child_id)},
        )
        policy = self._normalize_screentime_policy(feed, child_id)
        if policy is not None:
            self.screentime_policy_status = "ok"
            self.screentime_policy_source = "landing_page_feeds"
            _LOGGER.debug("Loaded screen-time weekday policy via Family landing-page-feeds")
            return policy

        feed_error = self.last_web_error_code
        if feed_error == "LOGIN_REDIRECT":
            self.screentime_policy_status = "session_expired"
            return None
        if feed_error == "TIMEOUT":
            self.screentime_policy_status = "web_api_timeout"
            return None
        if feed_error == "NETWORK_ERROR":
            self.screentime_policy_status = "web_api_network_error"
            return None

        # Compatibility fallback for accounts where landing-page-feeds does not
        # expose the schedule. Do not retry it after a transport timeout, since
        # that only multiplies coordinator latency without adding information.
        st_url = f"{self.WEB_API_BASE}/family/api/st"
        result = await self._web_request("GET", st_url, params={"childId": child_id})
        policy = self._normalize_screentime_policy(result, child_id)
        if policy is not None:
            self.screentime_policy_status = "ok"
            self.screentime_policy_source = "st"
            _LOGGER.debug("Loaded screen-time weekday policy via Family /st endpoint")
            return policy

        if self.last_web_error_code == "LOGIN_REDIRECT":
            self.screentime_policy_status = "session_expired"
        elif self.last_web_error_code == "TIMEOUT":
            self.screentime_policy_status = "web_api_timeout"
        elif self.last_web_error_code == "NETWORK_ERROR":
            self.screentime_policy_status = "web_api_network_error"
        elif self.web_session_state == "authenticated":
            self.screentime_policy_status = "no_policy_data"
        elif self.web_session_state == "expired":
            self.screentime_policy_status = "session_expired"
        else:
            self.screentime_policy_status = f"session_{self.web_session_state}"

        if isinstance(feed, dict):
            _LOGGER.debug(
                "Family landing-page-feeds contained no usable weekday policy; top-level keys=%s",
                sorted(str(k) for k in feed.keys())[:30],
            )
        else:
            _LOGGER.debug(
                "Family landing-page-feeds contained no usable weekday policy (type=%s, error=%s)",
                type(feed).__name__,
                feed_error,
            )
        return None

    async def set_screentime_allowance(
        self, child_id: str, day_of_week: int, hours: int, minutes: int
    ) -> bool:
        result = await self._web_request(
            "POST",
            f"{self.WEB_API_BASE}/family/api//st/day-allow",
            json_data={
                "childId": str(child_id),
                "dayOfWeek": int(day_of_week),
                "timeSpanDays": 0,
                "timeSpanHours": int(hours),
                "timeSpanMinutes": int(minutes),
            },
        )
        if result is None:
            raise FamilySafetyWebAPIError("Native screen-time allowance update failed")
        return True

    async def set_screentime_intervals(
        self, child_id: str, day_of_week: int, allowed_intervals: list[bool]
    ) -> bool:
        if len(allowed_intervals) != 48:
            raise ValueError("allowed_intervals must contain exactly 48 values")
        result = await self._web_request(
            "POST",
            f"{self.WEB_API_BASE}/family/api//st/day-allow-int",
            json_data={
                "childId": str(child_id),
                "dayOfWeek": int(day_of_week),
                "allowedIntervals": allowed_intervals,
            },
        )
        if result is None:
            raise FamilySafetyWebAPIError("Native screen-time interval update failed")
        return True

    # ------------------------------------------------------------------
    # Mobile API reads / writes retained from upstream
    # ------------------------------------------------------------------

    async def get_web_browsing_settings(self, child_id: str) -> dict | None:
        """Get web-browsing restrictions.

        The current account.microsoft.com Family UI loads this data through the
        authenticated Family web session, not through the mobile aggregator.
        Prefer the browser endpoint whenever web credentials are available. This
        also avoids Authentication.BadCompactTicket responses observed from the
        mobile WebRestrictions endpoint after an otherwise successful OAuth
        refresh.

        Keep the mobile endpoint only as a compatibility fallback for legacy
        installations that do not have a captured web session.
        """
        if self._web_cookies:
            _LOGGER.debug(
                "Fetching web browsing settings via account.microsoft.com Family web API"
            )
            result = await self._web_request(
                "GET",
                f"{self.WEB_API_BASE}/family/api/settings/web-browsing",
                params={"childId": str(child_id)},
            )
            return result if isinstance(result, dict) else None

        result = await self._request(
            "GET", f"/v1/WebRestrictions/{child_id}"
        )
        return result if isinstance(result, dict) else None

    async def get_device_overrides(self, child_id: str) -> dict | None:
        """Get device/platform overrides, preferring the current web route."""
        if self._web_cookies:
            result = await self._web_request(
                "GET",
                f"{self.WEB_API_BASE}/family/api/device-limits/screentime-time-override",
                params={"childId": str(child_id)},
                relationship_child_id=str(child_id),
            )
            if isinstance(result, dict):
                return result
        # Exact pyfamilysafety 1.1.2 compatibility fallback.
        result = await self._request("GET", f"/v1/devicelimits/{child_id}/overrides")
        return result if isinstance(result, dict) else None

    async def set_windows_time_override(
        self, child_id: str, time_override: str, date_time: str
    ) -> bool:
        """Set/cancel a Windows override using the HAR-confirmed web endpoint."""
        if time_override not in {"blockUntil", "cancel"}:
            raise ValueError("time_override must be 'blockUntil' or 'cancel'")
        result = await self._web_request(
            "POST",
            f"{self.WEB_API_BASE}/family/api/device-limits/screentime-time-override",
            json_data={
                "childId": str(child_id),
                "platformType": "windows",
                "timeOverride": time_override,
                "dateTime": date_time,
            },
            relationship_child_id=str(child_id),
        )
        if result is None:
            raise FamilySafetyWebAPIError("Windows screen-time override web request failed")
        return True

    async def get_content_settings(self, child_id: str) -> dict | None:
        result = await self._request("GET", f"/v1/ContentRestrictions/{child_id}")
        return result if isinstance(result, dict) else None

    async def get_devices(self, child_id: str) -> dict | None:
        result = await self._request("GET", f"/v1/devices/{child_id}")
        return result if isinstance(result, dict) else None

    async def set_screentime_daily_allowance(
        self,
        child_id: str,
        day_of_week: int,
        hours: int,
        minutes: int,
        platform: str = "Windows",
    ) -> dict | None:
        day_name = DAY_KEYS[day_of_week]
        result = await self._request(
            "PATCH",
            f"/v4/devicelimits/schedules/{child_id}",
            json_data={day_name: {"allowance": f"{hours:02d}:{minutes:02d}:00"}},
            extra_headers={"Plat-Info": platform},
        )
        return result if isinstance(result, dict) else None

    async def set_app_time_limit(
        self,
        child_id: str,
        app_id: str,
        display_name: str,
        platform: str,
        allowance: str,
        start_time: str = "07:00:00",
        end_time: str = "22:00:00",
    ) -> dict | None:
        day_schedule = {
            "allowance": allowance,
            "allottedIntervalsEnabled": True,
            "allottedIntervals": [{"start": start_time, "end": end_time}],
        }
        policy = {
            # These identity/state fields are present in pyfamilysafety 1.1.2's
            # working set_app_policy payload for the same mobile endpoint.
            "appId": app_id,
            "displayName": display_name or app_id,
            "enabled": True,
            "blocked": False,
            "blockState": "NotBlocked",
            "appTimeEnforcementPolicy": "custom",
            **{day: day_schedule for day in DAY_KEYS},
        }
        platform_header = {
            "windows": "WINDOWS",
            "xbox": "XBOX",
            "mobile": "MOBILE",
        }.get(str(platform).lower(), str(platform).upper())
        result = await self._request(
            "PATCH",
            f"/v3/appLimits/policies/{child_id}/{app_id}",
            json_data=policy,
            extra_headers={"Plat-Info": platform_header},
        )
        return result if isinstance(result, dict) else None

    async def remove_app_time_limit(
        self, child_id: str, app_id: str, display_name: str, platform: str
    ) -> dict | None:
        platform_header = {
            "windows": "WINDOWS",
            "xbox": "XBOX",
            "mobile": "MOBILE",
        }.get(str(platform).lower(), str(platform).upper())
        result = await self._request(
            "PATCH",
            f"/v3/appLimits/policies/{child_id}/{app_id}",
            json_data={
                "appId": app_id,
                "displayName": display_name or app_id,
                "enabled": False,
                "blocked": False,
                "blockState": "NotBlocked",
                "appTimeEnforcementPolicy": "custom",
            },
            extra_headers={"Plat-Info": platform_header},
        )
        return result if isinstance(result, dict) else None

    async def block_website(self, child_id: str, website: str) -> dict | None:
        """Block a website without discarding the existing lists.

        Microsoft treats the list fields as policy values, so preserve the
        current blocked/allowed entries instead of sending only the new host.
        Reading is performed through the current web endpoint when available;
        the actual write remains on the mobile API, as in the upstream client.
        """
        current = await self.get_web_browsing_settings(child_id)
        if not isinstance(current, dict):
            raise FamilySafetyWebAPIError(
                "Cannot safely add a blocked website because the current web policy could not be read"
            )
        blocked = list(current.get("blockedSites") or current.get("BlockedSites") or [])
        allowed = list(current.get("allowedSites") or current.get("AllowedSites") or [])
        if website not in blocked:
            blocked.append(website)
        allowed = [site for site in allowed if site != website]
        result = await self._request(
            "PATCH",
            f"/v1/WebRestrictions/{child_id}",
            json_data={"blockedSites": blocked, "allowedSites": allowed},
        )
        return result if isinstance(result, dict) else None

    async def remove_website(self, child_id: str, website: str) -> dict | None:
        current = await self.get_web_browsing_settings(child_id)
        if not isinstance(current, dict):
            raise FamilySafetyWebAPIError(
                "Cannot safely remove a website because the current web policy could not be read"
            )
        blocked_source = current.get("blockedSites") or current.get("BlockedSites") or []
        allowed_source = current.get("allowedSites") or current.get("AllowedSites") or []
        blocked = [s for s in blocked_source if s != website]
        allowed = [s for s in allowed_source if s != website]
        result = await self._request(
            "PATCH",
            f"/v1/WebRestrictions/{child_id}",
            json_data={"blockedSites": blocked, "allowedSites": allowed},
        )
        return result if isinstance(result, dict) else None

    async def toggle_web_filter(self, child_id: str, enabled: bool) -> dict | None:
        result = await self._request(
            "PATCH", f"/v1/WebRestrictions/{child_id}", json_data={"isEnabled": enabled}
        )
        return result if isinstance(result, dict) else None

    async def set_age_rating(self, child_id: str, age: int) -> dict | None:
        if not 3 <= age <= 21:
            raise ValueError("age must be between 3 and 21")
        result = await self._request(
            "PATCH", f"/v1/ContentRestrictions/{child_id}", json_data={"maxAgeRating": age}
        )
        return result if isinstance(result, dict) else None

    async def set_acquisition_policy(
        self, child_id: str, require_approval: bool
    ) -> dict | None:
        result = await self._request(
            "PATCH",
            f"/v1/ContentRestrictions/{child_id}",
            json_data={"acquisitionPolicy": "freeOnly" if require_approval else "unrestricted"},
        )
        return result if isinstance(result, dict) else None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        if self._web_session and not self._web_session.closed:
            await self._web_session.close()
        self._session = None
        self._web_session = None


class FamilySafetyWebAPIError(Exception):
    """Exception raised for API errors."""
