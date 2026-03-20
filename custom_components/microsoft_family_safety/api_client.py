"""Direct API client for Microsoft Family Safety web endpoints.

This module provides direct HTTP access to the Microsoft Family Safety
web API at account.microsoft.com/family/api/, bypassing the pyfamilysafety
library for features not yet supported by the library.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://account.microsoft.com"

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
    """Direct API client for Microsoft Family Safety web endpoints."""

    def __init__(self, authenticator) -> None:
        """Initialize the web API client.

        Args:
            authenticator: The pyfamilysafety Authenticator instance.
        """
        self._authenticator = authenticator
        self._session: aiohttp.ClientSession | None = None
        self._csrf_token: str | None = None

    async def _ensure_session(self) -> None:
        """Ensure an active aiohttp session exists."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def _ensure_auth(self) -> None:
        """Ensure the authenticator token is fresh."""
        if self._authenticator.expires <= datetime.now():
            await self._authenticator.perform_refresh()

    async def _fetch_csrf_token(self) -> str | None:
        """Fetch the CSRF token from the Family Safety web page."""
        await self._ensure_session()
        await self._ensure_auth()

        headers = {
            "Authorization": f"Bearer {self._authenticator.access_token}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }

        try:
            async with self._session.get(
                f"{BASE_URL}/family/settings", headers=headers
            ) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    match = re.search(
                        r'name="__RequestVerificationToken"[^>]*value="([^"]+)"',
                        html,
                    )
                    if match:
                        self._csrf_token = match.group(1)
                        _LOGGER.debug("CSRF token acquired successfully")
                        return self._csrf_token
                _LOGGER.warning(
                    "Could not fetch CSRF token (status %s)", resp.status
                )
        except Exception as err:
            _LOGGER.warning("Error fetching CSRF token: %s", err)

        return None

    def _build_headers(self) -> dict[str, str]:
        """Build request headers for the web API."""
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._authenticator.access_token}",
            "X-AMC-JsonMode": "CamelCase",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        if self._csrf_token:
            headers["__RequestVerificationToken"] = self._csrf_token
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        json_data: dict | None = None,
        params: dict | None = None,
    ) -> dict | list | None:
        """Make an authenticated request to the web API."""
        await self._ensure_session()
        await self._ensure_auth()

        url = f"{BASE_URL}{path}"
        headers = self._build_headers()

        _LOGGER.debug("Web API %s %s", method, path)

        try:
            async with self._session.request(
                method, url, headers=headers, json=json_data, params=params
            ) as resp:
                if resp.status == 403 and not self._csrf_token:
                    _LOGGER.info("Got 403, attempting to fetch CSRF token")
                    await self._fetch_csrf_token()
                    headers = self._build_headers()
                    async with self._session.request(
                        method, url, headers=headers, json=json_data, params=params
                    ) as retry_resp:
                        return await self._handle_response(retry_resp)
                return await self._handle_response(resp)
        except aiohttp.ClientError as err:
            _LOGGER.error("Web API request failed: %s", err)
            raise

    async def _handle_response(self, resp: aiohttp.ClientResponse) -> dict | list | None:
        """Handle the API response."""
        if resp.status in (200, 201, 204):
            if resp.content_type == "application/json":
                return await resp.json()
            return None
        text = await resp.text()
        _LOGGER.error("Web API error %s: %s", resp.status, text[:200])
        raise FamilySafetyWebAPIError(
            f"API request failed with status {resp.status}: {text[:200]}"
        )

    # ──────────────────────────────────────────────────────────────────────
    # GET Endpoints
    # ──────────────────────────────────────────────────────────────────────

    async def get_screentime_policy(self, child_id: str) -> dict | None:
        """Get per-device screen time policy."""
        return await self._request("GET", "/family/api/st", params={"childId": child_id})

    async def get_screentime_global(self, child_id: str) -> dict | None:
        """Get global screen time toggle state."""
        return await self._request(
            "GET", "/family/api/screen-time-global", params={"childId": child_id}
        )

    async def get_web_browsing_settings(self, child_id: str) -> dict | None:
        """Get web browsing/filter settings."""
        return await self._request(
            "GET", "/family/api/settings/web-browsing", params={"childId": child_id}
        )

    async def get_content_settings(self, child_id: str) -> dict | None:
        """Get content/age restriction settings."""
        return await self._request(
            "GET", "/family/api/settings/update-content-settings",
            params={"childId": child_id},
        )

    async def get_devices(self, child_id: str) -> dict | None:
        """Get list of connected devices."""
        return await self._request(
            "GET", "/family/api/device-limits/get-devices",
            params={"childId": child_id},
        )

    async def get_app_policies(self, child_id: str) -> dict | None:
        """Get all app policies."""
        return await self._request(
            "GET", "/family/api/app-limits/get-all-app-policies-v3",
            params={"childId": child_id},
        )

    async def get_app_limits(self, child_id: str, timezone: str = "Europe/Paris") -> dict | None:
        """Get all app limits."""
        return await self._request(
            "GET", "/family/api/app-limits/get-all-app-limits-v3",
            params={"childId": child_id, "timeZone": timezone},
        )

    # ──────────────────────────────────────────────────────────────────────
    # Screen Time Controls
    # ──────────────────────────────────────────────────────────────────────

    async def set_screentime_daily_allowance(
        self,
        child_id: str,
        day_of_week: int,
        hours: int,
        minutes: int,
    ) -> dict | None:
        """Set daily screen time allowance for a specific day.

        Args:
            child_id: The child's user ID.
            day_of_week: 0=Sunday, 1=Monday, ..., 6=Saturday.
            hours: Allowed hours (0-24).
            minutes: Allowed minutes (0-59).
        """
        return await self._request(
            "POST",
            "/family/api//st/day-allow",
            json_data={
                "childId": child_id,
                "dayOfWeek": day_of_week,
                "timeSpanDays": 0,
                "timeSpanHours": hours,
                "timeSpanMinutes": minutes,
            },
        )

    async def set_screentime_intervals(
        self,
        child_id: str,
        day_of_week: int,
        allowed_intervals: list[bool],
    ) -> dict | None:
        """Set allowed time intervals for a specific day.

        Args:
            child_id: The child's user ID.
            day_of_week: 0=Sunday, 1=Monday, ..., 6=Saturday.
            allowed_intervals: 48 booleans for 30-min slots (00:00 to 23:30).
        """
        if len(allowed_intervals) != 48:
            raise ValueError("allowed_intervals must contain exactly 48 booleans")
        return await self._request(
            "POST",
            "/family/api//st/day-allow-int",
            json_data={
                "childId": child_id,
                "dayOfWeek": day_of_week,
                "allowedIntervals": allowed_intervals,
            },
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
        """Set allowed time interval using start/end time.

        Converts a time range to the 48-boolean-slot format.

        Args:
            child_id: The child's user ID.
            day_of_week: 0=Sunday, 1=Monday, ..., 6=Saturday.
            start_hour: Start hour (0-23).
            start_minute: Start minute (0 or 30).
            end_hour: End hour (0-23).
            end_minute: End minute (0 or 30).
        """
        intervals = [False] * 48
        start_slot = start_hour * 2 + (1 if start_minute >= 30 else 0)
        end_slot = end_hour * 2 + (1 if end_minute >= 30 else 0)
        for i in range(start_slot, min(end_slot, 48)):
            intervals[i] = True
        return await self.set_screentime_intervals(child_id, day_of_week, intervals)

    # ──────────────────────────────────────────────────────────────────────
    # App Limits Controls
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
        """Set a per-app time limit for all days.

        Args:
            child_id: The child's user ID.
            app_id: The application ID (e.g., "x:windowsplt:61f08e27aff06317").
            display_name: The application display name.
            platform: "windows", "xbox", or "mobile".
            allowance: Duration as "HH:MM:SS" (e.g., "02:00:00" for 2 hours).
            start_time: Allowed start time as "HH:MM:SS" (default "07:00:00").
            end_time: Allowed end time as "HH:MM:SS" (default "22:00:00").
        """
        day_schedule = {
            "allowance": allowance,
            "allottedIntervalsEnabled": True,
            "allottedIntervals": [{"start": start_time, "end": end_time}],
        }
        app_policy = {
            "id": app_id,
            "displayName": display_name,
            "enabled": True,
            "blockState": "notBlocked",
            "appTimeEnforcementPolicy": "custom",
            "lastModified": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "monday": day_schedule,
            "tuesday": day_schedule,
            "wednesday": day_schedule,
            "thursday": day_schedule,
            "friday": day_schedule,
            "saturday": day_schedule,
            "sunday": day_schedule,
        }
        return await self._request(
            "POST",
            "/family/api/app-limits/set-custom-app-policy-v3",
            json_data={
                "childId": child_id,
                "appPolicy": app_policy,
                "platform": platform,
            },
        )

    async def remove_app_time_limit(
        self,
        child_id: str,
        app_id: str,
        display_name: str,
        platform: str,
    ) -> dict | None:
        """Remove a per-app time limit (turn off limits for an app).

        Args:
            child_id: The child's user ID.
            app_id: The application ID.
            display_name: The application display name.
            platform: "windows", "xbox", or "mobile".
        """
        return await self._request(
            "POST",
            "/family/api/app-limits/set-custom-app-policy-v3",
            json_data={
                "childId": child_id,
                "appPolicy": {
                    "id": app_id,
                    "displayName": display_name,
                    "enabled": False,
                    "appTimeEnforcementPolicy": "custom",
                    "lastModified": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                },
                "platform": platform,
            },
        )

    # ──────────────────────────────────────────────────────────────────────
    # Web Filtering Controls
    # ──────────────────────────────────────────────────────────────────────

    async def block_website(self, child_id: str, website: str) -> dict | None:
        """Block a website for a child.

        Args:
            child_id: The child's user ID.
            website: Domain to block (e.g., "example.com").
        """
        return await self._request(
            "POST",
            "/family/api/settings/block-website",
            json_data={"childId": child_id, "website": website},
        )

    async def remove_website(self, child_id: str, website: str) -> dict | None:
        """Remove a website from blocked/allowed list.

        Args:
            child_id: The child's user ID.
            website: Domain to remove.
        """
        return await self._request(
            "DELETE",
            "/family/api/settings/remove-website",
            params={"childId": child_id, "website": website},
        )

    async def toggle_web_filter(self, child_id: str, enabled: bool) -> dict | None:
        """Toggle web filtering on/off.

        Args:
            child_id: The child's user ID.
            enabled: True to enable web filtering, False to disable.
        """
        return await self._request(
            "POST",
            "/family/api/settings/web-browsing-toggle",
            json_data={"childId": child_id, "isEnabled": enabled},
        )

    # ──────────────────────────────────────────────────────────────────────
    # Content / Age Restrictions
    # ──────────────────────────────────────────────────────────────────────

    async def set_age_rating(self, child_id: str, age: int) -> dict | None:
        """Set content age rating restriction.

        Args:
            child_id: The child's user ID.
            age: Age rating (3-20, or 21 for "any age" / no restriction).
        """
        if not 3 <= age <= 21:
            raise ValueError("age must be between 3 and 21 (21 = no restriction)")
        return await self._request(
            "PUT",
            "/family/api/settings/update-content-settings",
            json_data={"childId": child_id, "contentRatingAge": age},
        )

    # ──────────────────────────────────────────────────────────────────────
    # Acquisition Policy (Ask to Buy)
    # ──────────────────────────────────────────────────────────────────────

    async def set_acquisition_policy(
        self, child_id: str, require_approval: bool
    ) -> dict | None:
        """Set the ask-to-buy policy.

        Args:
            child_id: The child's user ID.
            require_approval: True = require approval (freeOnly), False = unrestricted.
        """
        policy = "freeOnly" if require_approval else "unrestricted"
        return await self._request(
            "POST",
            "/family/api/ps/set-acquisition-policy",
            json_data={"childId": child_id, "policy": policy},
        )

    # ──────────────────────────────────────────────────────────────────────
    # Cleanup
    # ──────────────────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        self._csrf_token = None


class FamilySafetyWebAPIError(Exception):
    """Exception raised for web API errors."""
