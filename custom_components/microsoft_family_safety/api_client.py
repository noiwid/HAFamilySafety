"""API client for Microsoft Family Safety mobile endpoints.

This module provides HTTP access to the Microsoft Family Safety mobile API
at mobileaggregator.family.microsoft.com/api/, for features not yet exposed
by the pyfamilysafety library (web browsing settings, screen time policy,
content restrictions, etc.).

It reuses the same authentication as pyfamilysafety: an MSA token scoped for
familymobile.microsoft.com, sent as MSAuth1.0 in the Authorization header.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

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
    """API client for Microsoft Family Safety mobile endpoints.

    Named WebAPI for backward compatibility with coordinator imports.
    """

    def __init__(self, authenticator) -> None:
        self._authenticator = authenticator
        self._session: aiohttp.ClientSession | None = None
        self._access_token: str | None = None
        self._token_expires: datetime | None = None

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
    # GET Endpoints (mobile API)
    # ──────────────────────────────────────────────────────────────────────

    async def get_web_browsing_settings(self, child_id: str) -> dict | None:
        """Get web browsing/filter restrictions."""
        result = await self._request(
            "GET", f"/v1/WebRestrictions/{child_id}"
        )
        _LOGGER.debug("WebRestrictions response for %s: %s", child_id, result)
        return result

    async def get_screentime_policy(
        self, child_id: str, platform: str = "Windows"
    ) -> dict | None:
        """Get device limits schedules (daily allowances + time windows).

        Tries GET first (with appliesTo query param), then falls back
        to a no-op PATCH which should return the current schedule.
        """
        plat_headers = {"Plat-Info": platform}

        # Attempt 1: GET with appliesTo query parameter
        try:
            result = await self._request(
                "GET",
                f"/v4/devicelimits/schedules/{child_id}",
                params={"appliesTo": platform},
                extra_headers=plat_headers,
            )
            _LOGGER.debug("Schedules GET response for %s: %s", child_id, result)
            return result
        except FamilySafetyWebAPIError as err:
            _LOGGER.debug("Schedules GET failed (%s), trying PATCH fallback", err)

        # Attempt 2: PATCH with empty body — response should contain current schedule
        try:
            result = await self._request(
                "PATCH",
                f"/v4/devicelimits/schedules/{child_id}",
                json_data={},
                extra_headers=plat_headers,
            )
            _LOGGER.debug("Schedules PATCH fallback response for %s: %s", child_id, result)
            return result
        except FamilySafetyWebAPIError as err:
            _LOGGER.debug("Schedules PATCH fallback also failed: %s", err)
            raise

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
    # Screen Time Controls
    # ──────────────────────────────────────────────────────────────────────

    async def set_screentime_daily_allowance(
        self,
        child_id: str,
        day_of_week: int,
        hours: int,
        minutes: int,
        platform: str = "Windows",
    ) -> dict | None:
        """Set daily screen time allowance via device limits schedule.

        Uses PATCH /v4/devicelimits/schedules/{USER_ID}.
        """
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
        """Set allowed time intervals for a specific day.

        Uses PATCH /v4/devicelimits/schedules/{USER_ID}.
        """
        if len(allowed_intervals) != 48:
            raise ValueError("allowed_intervals must contain exactly 48 booleans")
        day_names = [
            "sunday", "monday", "tuesday", "wednesday",
            "thursday", "friday", "saturday",
        ]
        day_name = day_names[day_of_week]

        # Convert 48 booleans to interval ranges
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
        """Set a per-app time limit.

        Uses PATCH /v3/appLimits/policies/{USER_ID}/{APP_ID}.
        """
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
    # Web Filtering Controls
    # ──────────────────────────────────────────────────────────────────────

    async def block_website(self, child_id: str, website: str) -> dict | None:
        """Block a website for a child.

        Uses PATCH /v1/WebRestrictions/{USER_ID}.
        """
        return await self._request(
            "PATCH",
            f"/v1/WebRestrictions/{child_id}",
            json_data={"blockedSites": [website]},
        )

    async def remove_website(self, child_id: str, website: str) -> dict | None:
        """Remove a website from blocked list.

        Uses PATCH /v1/WebRestrictions/{USER_ID}.
        """
        # First get current restrictions to remove from list
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
        """Toggle web filtering on/off.

        Uses PATCH /v1/WebRestrictions/{USER_ID}.
        """
        return await self._request(
            "PATCH",
            f"/v1/WebRestrictions/{child_id}",
            json_data={"isEnabled": enabled},
        )

    # ──────────────────────────────────────────────────────────────────────
    # Content / Age Restrictions
    # ──────────────────────────────────────────────────────────────────────

    async def set_age_rating(self, child_id: str, age: int) -> dict | None:
        """Set content age rating restriction.

        Uses PATCH /v1/ContentRestrictions/{USER_ID}.
        """
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
        """Set the ask-to-buy policy.

        Uses PATCH /v1/ContentRestrictions/{USER_ID}.
        """
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
