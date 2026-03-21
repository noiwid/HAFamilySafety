"""API client for Microsoft Family Safety.

This module provides two authentication strategies:

1. **Mobile API** (MSAuth1.0 token) — for WRITE operations (PATCH endpoints)
   Uses mobileaggregator.family.microsoft.com with MSA tokens from pyfamilysafety.

2. **Web API** (browser cookies) — for READ operations (screen time schedule)
   Uses account.microsoft.com/family/api/* with cookies from the Playwright addon.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from yarl import URL

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Mobile API base
_BASE_URL = "https://mobileaggregator.family.microsoft.com/api"

# Token constants (same as pyfamilysafety)
_TOKEN_ENDPOINT = "https://login.live.com/oauth20_token.srf"
_CLIENT_ID = "000000000004893A"
_SCOPE = "service::familymobile.microsoft.com::MBI_SSL"

# Emulate Android Family Safety app
_APP_VERSION = "v 1.26.0.1001"
_USER_AGENT = f"Family Safety-prod/({_APP_VERSION}) Android/33 google/Pixel 4 XL"

# Days of week mapping
DAYS_OF_WEEK = {
    "sunday": 0,
    "monday": 1,
    "tuesday": 2,
    "wednesday": 3,
    "thursday": 4,
    "friday": 5,
    "saturday": 6,
}


class FamilySafetyWebAPI:
    """API client for Microsoft Family Safety.

    Combines mobile API (token auth) for writes and web API (cookie auth) for reads.
    """

    # Web API base URL
    WEB_API_BASE = "https://account.microsoft.com"

    def __init__(self, authenticator) -> None:
        self._authenticator = authenticator
        self._session: aiohttp.ClientSession | None = None
        self._access_token: str | None = None
        self._token_expires: datetime | None = None
        # Browser cookies for web API reads
        self._web_cookies: list[dict[str, Any]] | None = None
        self._web_canary: str | None = None

    def set_web_cookies(self, cookies: list[dict[str, Any]]) -> None:
        """Set browser cookies for web API access (from Playwright addon)."""
        self._web_cookies = cookies
        _LOGGER.info(
            "Web API cookies configured (%d cookies)",
            len(cookies) if cookies else 0,
        )

    @property
    def has_web_cookies(self) -> bool:
        """Return True if web cookies are available."""
        return bool(self._web_cookies)

    async def _ensure_session(self) -> None:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            self._session = aiohttp.ClientSession(timeout=timeout)

    async def _ensure_auth(self) -> None:
        """Acquire a valid access token for the mobile API."""
        if (
            self._access_token
            and self._token_expires
            and self._token_expires > datetime.now()
        ):
            return
        await self._ensure_session()

        refresh_token = self._authenticator.refresh_token
        if not refresh_token:
            raise FamilySafetyWebAPIError("No refresh token available")

        form_data = {
            "client_id": _CLIENT_ID,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "scope": _SCOPE,
        }
        async with self._session.post(
            _TOKEN_ENDPOINT, data=aiohttp.FormData(form_data)
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                _LOGGER.error(
                    "Token request failed (status %s): %s",
                    resp.status, text[:200],
                )
                raise FamilySafetyWebAPIError(
                    f"Token request failed with status {resp.status}"
                )
            data = await resp.json(content_type=None)
            self._access_token = data["access_token"]
            self._token_expires = datetime.now() + timedelta(
                seconds=data.get("expires_in", 3600)
            )
            _LOGGER.debug(
                "Mobile API token acquired (expires in %ss)",
                data.get("expires_in"),
            )

    def _build_headers(self) -> dict[str, str]:
        return {
            "Authorization": f'MSAuth1.0 usertoken="{self._access_token}", type="MSACT"',
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

        url = f"{_BASE_URL}{path}"
        headers = self._build_headers()
        if extra_headers:
            headers.update(extra_headers)

        _LOGGER.debug("Mobile API %s %s", method, path)

        try:
            async with self._session.request(
                method, url, headers=headers, json=json_data, params=params
            ) as resp:
                if resp.status in (401, 403):
                    _LOGGER.info(
                        "Got %s, refreshing token and retrying", resp.status
                    )
                    self._access_token = None
                    self._token_expires = None
                    await self._ensure_auth()
                    headers = self._build_headers()
                    if extra_headers:
                        headers.update(extra_headers)
                    async with self._session.request(
                        method, url, headers=headers,
                        json=json_data, params=params,
                    ) as retry_resp:
                        return await self._handle_response(retry_resp)
                return await self._handle_response(resp)
        except aiohttp.ClientError as err:
            _LOGGER.error("Mobile API request failed: %s", err)
            raise

    async def _handle_response(
        self, resp: aiohttp.ClientResponse
    ) -> dict | list | None:
        if resp.status in (200, 201, 204):
            if resp.content_type and "json" in resp.content_type:
                return await resp.json()
            return None
        text = await resp.text()
        _LOGGER.error("Mobile API error %s: %s", resp.status, text[:200])
        raise FamilySafetyWebAPIError(
            f"API request failed with status {resp.status}: {text[:200]}"
        )

    # ──────────────────────────────────────────────────────────────────────
    # Web API (cookie-based) — READ operations
    # ──────────────────────────────────────────────────────────────────────

    def _build_cookie_jar(self) -> aiohttp.CookieJar:
        """Build a cookie jar from Playwright browser cookies."""
        jar = aiohttp.CookieJar(unsafe=True)
        if not self._web_cookies:
            return jar

        for cookie in self._web_cookies:
            name = cookie.get("name", "")
            value = cookie.get("value", "")
            domain = cookie.get("domain", "")

            # Build a morsel-compatible SimpleCookie
            jar.update_cookies(
                {name: value},
                URL(f"https://{domain.lstrip('.')}/"),
            )
        return jar

    async def _web_request(
        self,
        method: str,
        url: str,
        params: dict | None = None,
        json_data: dict | None = None,
    ) -> dict | list | None:
        """Make an authenticated request to the web API using browser cookies."""
        if not self._web_cookies:
            _LOGGER.warning("No web cookies available for web API request")
            return None

        cookie_jar = self._build_cookie_jar()
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://account.microsoft.com",
            "Referer": "https://account.microsoft.com/family",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }

        # Microsoft web APIs may require a canary/CSRF token
        canary = getattr(self, "_web_canary", None)
        if not canary:
            for cookie in self._web_cookies:
                name = cookie.get("name", "")
                value = cookie.get("value", "")
                if name.lower() in ("canary", "fpt", "xsrf-token", "__requestverificationtoken"):
                    canary = value
                    _LOGGER.debug("Found CSRF/canary token in cookie: %s", name)
                    break
        if canary:
            headers["canary"] = canary

        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        async with aiohttp.ClientSession(
            cookie_jar=cookie_jar, timeout=timeout
        ) as session:
            _LOGGER.debug("Web API %s %s", method, url)
            async with session.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_data,
                allow_redirects=False,
            ) as resp:
                if resp.status == 200:
                    if resp.content_type and "json" in resp.content_type:
                        data = await resp.json()
                        _LOGGER.debug("Web API success: %s", str(data)[:300])
                        return data
                    return None

                if resp.status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location", "unknown")
                    _LOGGER.warning(
                        "Web API redirected (status %s) to %s — cookies may be expired",
                        resp.status,
                        location,
                    )
                    return None

                text = await resp.text()
                _LOGGER.warning(
                    "Web API error %s: %s", resp.status, text[:200]
                )

                # On 401, log cookie domains for diagnostics
                if resp.status == 401:
                    domains = {c.get("domain", "?") for c in (self._web_cookies or [])}
                    _LOGGER.debug(
                        "Web API 401 — cookie domains present: %s",
                        ", ".join(sorted(domains)),
                    )
                return None

    async def _warm_web_session(self) -> str | None:
        """Visit the family page to initialize the web session and extract canary token.

        Microsoft requires an active session before API calls succeed.
        Returns the canary token if found, or None.
        """
        if not self._web_cookies:
            return None

        cookie_jar = self._build_cookie_jar()
        headers = {
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }

        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        try:
            async with aiohttp.ClientSession(
                cookie_jar=cookie_jar, timeout=timeout
            ) as session:
                async with session.get(
                    f"{self.WEB_API_BASE}/family",
                    headers=headers,
                    allow_redirects=True,
                ) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        # Extract canary token from page if present
                        import re
                        match = re.search(
                            r'"canary"\s*:\s*"([^"]+)"', text
                        ) or re.search(
                            r'name="canary"\s+(?:content|value)="([^"]+)"', text
                        ) or re.search(
                            r'"apiCanary"\s*:\s*"([^"]+)"', text
                        )
                        if match:
                            _LOGGER.debug("Extracted canary token from family page")
                            return match.group(1)
                        _LOGGER.debug("Family page loaded (no canary token found)")
                    else:
                        _LOGGER.debug("Family page warm-up returned %s", resp.status)
        except Exception as exc:
            _LOGGER.debug("Family page warm-up failed: %s", exc)
        return None

    async def get_screentime_policy(
        self, child_id: str, platform: str = "Windows"
    ) -> dict | None:
        """Get screen time policy via the web API (cookie-based).

        Requires browser cookies from the Playwright auth addon.
        Falls back gracefully if cookies are not available.
        """
        if not self._web_cookies:
            _LOGGER.debug(
                "No web cookies — screen time policy not available. "
                "Install the Family Safety Auth add-on for full read support."
            )
            return None

        url = f"{self.WEB_API_BASE}/family/api/st"
        result = await self._web_request("GET", url, params={"childId": child_id})

        # If first attempt fails, warm up the session and retry
        if result is None and self._web_cookies:
            _LOGGER.debug("Web API returned None, warming up session and retrying...")
            canary = await self._warm_web_session()
            if canary:
                # Store canary for subsequent requests
                self._web_canary = canary
            result = await self._web_request("GET", url, params={"childId": child_id})

        if result:
            _LOGGER.info(
                "Successfully read screen time policy for child %s via web API",
                child_id,
            )
        return result

    # ──────────────────────────────────────────────────────────────────────
    # GET Endpoints (mobile API)
    # ──────────────────────────────────────────────────────────────────────

    async def get_web_browsing_settings(self, child_id: str) -> dict | None:
        """Get web browsing/filter restrictions."""
        result = await self._request(
            "GET", f"/v1/WebRestrictions/{child_id}"
        )
        _LOGGER.debug("WebRestrictions response for %s: %s", child_id, result)
        return result

    async def get_device_overrides(self, child_id: str) -> dict | None:
        """Get device lock/unlock overrides."""
        return await self._request(
            "GET", f"/v1/devicelimits/{child_id}/overrides"
        )

    async def get_content_settings(self, child_id: str) -> dict | None:
        """Get content/age restriction settings."""
        return await self._request(
            "GET", f"/v1/ContentRestrictions/{child_id}"
        )

    async def get_devices(self, child_id: str) -> dict | None:
        """Get list of connected devices."""
        return await self._request(
            "GET", f"/v1/devices/{child_id}"
        )

    # ──────────────────────────────────────────────────────────────────────
    # Screen Time Controls (mobile API — WRITE)
    # ──────────────────────────────────────────────────────────────────────

    async def set_screentime_daily_allowance(
        self,
        child_id: str,
        day_of_week: int,
        hours: int,
        minutes: int,
        platform: str = "Windows",
    ) -> dict | None:
        """Set daily screen time allowance via device limits schedule."""
        day_names = [
            "sunday", "monday", "tuesday", "wednesday",
            "thursday", "friday", "saturday",
        ]
        day_name = day_names[day_of_week]
        allowance = f"{hours:02d}:{minutes:02d}:00"
        schedule = {
            day_name: {"allowance": allowance}
        }
        return await self._request(
            "PATCH",
            f"/v4/devicelimits/schedules/{child_id}",
            json_data=schedule,
            extra_headers={"Plat-Info": platform},
        )

    async def set_screentime_intervals(
        self,
        child_id: str,
        day_of_week: int,
        allowed_intervals: list[bool],
        platform: str = "Windows",
    ) -> dict | None:
        """Set allowed time intervals for a specific day."""
        if len(allowed_intervals) != 48:
            raise ValueError("allowed_intervals must contain exactly 48 booleans")
        day_names = [
            "sunday", "monday", "tuesday", "wednesday",
            "thursday", "friday", "saturday",
        ]
        day_name = day_names[day_of_week]

        intervals = []
        i = 0
        while i < 48:
            if allowed_intervals[i]:
                start_h, start_m = divmod(i * 30, 60)
                j = i
                while j < 48 and allowed_intervals[j]:
                    j += 1
                end_h, end_m = divmod(j * 30, 60)
                intervals.append({
                    "start": f"{start_h:02d}:{start_m:02d}:00",
                    "end": f"{end_h:02d}:{end_m:02d}:00",
                })
                i = j
            else:
                i += 1

        schedule = {
            day_name: {
                "allottedIntervalsEnabled": True,
                "allottedIntervals": intervals,
            }
        }
        return await self._request(
            "PATCH",
            f"/v4/devicelimits/schedules/{child_id}",
            json_data=schedule,
            extra_headers={"Plat-Info": platform},
        )

    async def set_screentime_intervals_from_range(
        self,
        child_id: str,
        day_of_week: int,
        start_hour: int,
        start_minute: int,
        end_hour: int,
        end_minute: int,
    ) -> dict | None:
        """Set allowed time interval using start/end time."""
        intervals = [False] * 48
        start_slot = start_hour * 2 + (1 if start_minute >= 30 else 0)
        end_slot = end_hour * 2 + (1 if end_minute >= 30 else 0)
        for i in range(start_slot, min(end_slot, 48)):
            intervals[i] = True
        return await self.set_screentime_intervals(child_id, day_of_week, intervals)

    # ──────────────────────────────────────────────────────────────────────
    # App Limits Controls (mobile API — WRITE)
    # ──────────────────────────────────────────────────────────────────────

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
        """Set a per-app time limit."""
        day_schedule = {
            "allowance": allowance,
            "allottedIntervalsEnabled": True,
            "allottedIntervals": [{"start": start_time, "end": end_time}],
        }
        policy = {
            "enabled": True,
            "blockState": "notBlocked",
            "appTimeEnforcementPolicy": "custom",
            "monday": day_schedule,
            "tuesday": day_schedule,
            "wednesday": day_schedule,
            "thursday": day_schedule,
            "friday": day_schedule,
            "saturday": day_schedule,
            "sunday": day_schedule,
        }
        return await self._request(
            "PATCH",
            f"/v3/appLimits/policies/{child_id}/{app_id}",
            json_data=policy,
        )

    async def remove_app_time_limit(
        self,
        child_id: str,
        app_id: str,
        display_name: str,
        platform: str,
    ) -> dict | None:
        """Remove a per-app time limit."""
        return await self._request(
            "PATCH",
            f"/v3/appLimits/policies/{child_id}/{app_id}",
            json_data={
                "enabled": False,
                "appTimeEnforcementPolicy": "custom",
            },
        )

    # ──────────────────────────────────────────────────────────────────────
    # Web Filtering Controls (mobile API — WRITE)
    # ──────────────────────────────────────────────────────────────────────

    async def block_website(self, child_id: str, website: str) -> dict | None:
        """Block a website for a child."""
        return await self._request(
            "PATCH",
            f"/v1/WebRestrictions/{child_id}",
            json_data={"blockedSites": [website]},
        )

    async def remove_website(self, child_id: str, website: str) -> dict | None:
        """Remove a website from blocked list."""
        current = await self.get_web_browsing_settings(child_id)
        if not current:
            return None
        blocked = current.get("blockedSites", [])
        blocked = [s for s in blocked if s != website]
        allowed = current.get("allowedSites", [])
        allowed = [s for s in allowed if s != website]
        return await self._request(
            "PATCH",
            f"/v1/WebRestrictions/{child_id}",
            json_data={"blockedSites": blocked, "allowedSites": allowed},
        )

    async def toggle_web_filter(self, child_id: str, enabled: bool) -> dict | None:
        """Toggle web filtering on/off."""
        return await self._request(
            "PATCH",
            f"/v1/WebRestrictions/{child_id}",
            json_data={"isEnabled": enabled},
        )

    # ──────────────────────────────────────────────────────────────────────
    # Content / Age Restrictions (mobile API — WRITE)
    # ──────────────────────────────────────────────────────────────────────

    async def set_age_rating(self, child_id: str, age: int) -> dict | None:
        """Set content age rating restriction."""
        if not 3 <= age <= 21:
            raise ValueError("age must be between 3 and 21 (21 = no restriction)")
        return await self._request(
            "PATCH",
            f"/v1/ContentRestrictions/{child_id}",
            json_data={"maxAgeRating": age},
        )

    # ──────────────────────────────────────────────────────────────────────
    # Acquisition Policy (Ask to Buy)
    # ──────────────────────────────────────────────────────────────────────

    async def set_acquisition_policy(
        self, child_id: str, require_approval: bool
    ) -> dict | None:
        """Set the ask-to-buy policy."""
        policy = "freeOnly" if require_approval else "unrestricted"
        return await self._request(
            "PATCH",
            f"/v1/ContentRestrictions/{child_id}",
            json_data={"acquisitionPolicy": policy},
        )

    # ──────────────────────────────────────────────────────────────────────
    # Cleanup
    # ──────────────────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        self._access_token = None
        self._token_expires = None


class FamilySafetyWebAPIError(Exception):
    """Exception raised for API errors."""
