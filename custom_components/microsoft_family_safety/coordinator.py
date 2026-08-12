"""Data coordinator for Microsoft Family Safety."""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import logging
from typing import Any

from pyfamilysafety import FamilySafety
from pyfamilysafety.account import Account
from pyfamilysafety.application import Application
from pyfamilysafety.device import Device
from pyfamilysafety.enum import OverrideTarget, OverrideType
from pyfamilysafety.exceptions import HttpException
from yarl import URL

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from ._pyfamilysafety_compat import apply_patches
from .api_client import FamilySafetyWebAPI
from .auth.addon_client import AddonCookieClient
from .const import (
    CONF_API_KEY,
    CONF_AUTH_URL,
    CONF_REFRESH_TOKEN,
    CONF_UPDATE_INTERVAL,
    CONF_WEB_COOKIES,
    CONF_WEB_FAMILY_REFERER,
    CONF_WEB_FAMILY_TOKEN,
    DAY_KEYS,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    ERROR_AUTH_FAILED,
    ERROR_TOKEN_EXPIRED,
)

_LOGGER = logging.getLogger(__name__)
STORAGE_KEY = f"{DOMAIN}.saved_screentime"
STORAGE_VERSION = 1
AUTH_STORAGE_VERSION = 1
AUTH_NOTIFICATION_ID = "familysafety_auth_expired"


def _range_to_slots(start_hour: int, start_minute: int, end_hour: int, end_minute: int) -> list[bool]:
    intervals = [False] * 48
    start_slot = (start_hour * 60 + start_minute) // 30
    end_slot = -(-(end_hour * 60 + end_minute) // 30)  # ceiling division
    for i in range(start_slot, min(end_slot, 48)):
        intervals[i] = True
    return intervals


def _policy_intervals_to_slots(day_data: dict[str, Any]) -> list[bool] | None:
    """Convert saved Microsoft interval formats to 48 half-hour slots.

    The current account.microsoft.com response uses allowedIntervals objects
    with begin/end values. Older integration builds stored a boolean timeline.
    Supporting both is required so account lock/unlock restores the exact
    schedule that was visible before the lock.
    """
    timeline = day_data.get("timeline")
    if (
        isinstance(timeline, list)
        and len(timeline) == 48
        and all(isinstance(item, bool) for item in timeline)
    ):
        return list(timeline)

    raw = day_data.get("allowedIntervals")
    if raw is None:
        raw = day_data.get("AllowedIntervals")
    if not isinstance(raw, list):
        return None

    slots = [False] * 48
    found = False

    def to_minutes(value: object) -> int | None:
        if not isinstance(value, str):
            return None
        try:
            parts = value.split(":")
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            return None
        if hour == 24 and minute == 0:
            return 1440
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            return None
        return hour * 60 + minute

    for interval in raw:
        if not isinstance(interval, dict):
            continue
        begin = to_minutes(
            interval.get("begin")
            or interval.get("Begin")
            or interval.get("start")
            or interval.get("Start")
        )
        end = to_minutes(interval.get("end") or interval.get("End"))
        if begin is None or end is None or end <= begin:
            continue
        start_slot = max(0, min(47, begin // 30))
        end_slot = max(0, min(48, -(-end // 30)))
        for slot in range(start_slot, end_slot):
            slots[slot] = True
        found = True

    # An explicitly empty list means Microsoft allows no time window for that
    # day. Preserve it as 48 disabled slots instead of substituting the legacy
    # 07:00-22:00 default during a later account-unlock restore.
    return slots if found or raw == [] else None


def _ms_to_minutes(milliseconds: int | float | None) -> int:
    if not milliseconds:
        return 0
    return int(milliseconds / 60000)


class FamilySafetyDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch and coordinate Microsoft Family Safety state."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        update_interval = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=update_interval),
        )
        self.entry = entry
        self.api: FamilySafety | None = None
        self.web_api: FamilySafetyWebAPI | None = None
        self._accounts: dict[str, Account] = {}
        self._devices: dict[str, Device] = {}
        self._is_retrying_auth = False
        self._saved_screentime: dict[str, dict[str, Any]] = {}
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._auth_store: Store = Store(
            hass, AUTH_STORAGE_VERSION, f"{DOMAIN}.auth.{entry.entry_id}"
        )
        self._runtime_auth_state: dict[str, Any] = {}
        self._reauth_requested = False
        self._reauth_reason: str | None = None
        auth_url = entry.options.get(CONF_AUTH_URL) or entry.data.get(CONF_AUTH_URL)
        api_key = entry.options.get(CONF_API_KEY) or entry.data.get(CONF_API_KEY)
        self._addon_client = AddonCookieClient(
            hass, auth_url=auth_url, api_key=api_key
        )
        self._native_web_auth = bool(entry.data.get(CONF_WEB_COOKIES))
        self._web_cookies_loaded = False
        self._auth_notification_sent = False
        self._platform_override_until: dict[tuple[str, str], datetime] = {}

    def _entry_auth_anchor(self) -> str:
        """Fingerprint the base browser/mobile login stored in the config entry.

        Runtime credentials may rotate between restarts, but a manual reconfigure
        must supersede the previous runtime cache. Microsoft can return the same
        refresh-token string for a renewed login, so include the entry browser
        credential generation as well. Values are hashed locally and never logged.
        """
        material = {
            "refresh_token": self.entry.data.get(CONF_REFRESH_TOKEN, ""),
            "web_family_token": self.entry.data.get(CONF_WEB_FAMILY_TOKEN, ""),
            "web_family_referer": self.entry.data.get(CONF_WEB_FAMILY_REFERER, ""),
            "web_cookies": self.entry.data.get(CONF_WEB_COOKIES, []),
        }
        encoded = json.dumps(
            material, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    async def async_load_saved_screentime(self) -> None:
        data = await self._store.async_load()
        if isinstance(data, dict):
            self._saved_screentime = data
        auth_state = await self._auth_store.async_load()
        if (
            isinstance(auth_state, dict)
            and auth_state.get("anchor") == self._entry_auth_anchor()
        ):
            self._runtime_auth_state = auth_state
        else:
            self._runtime_auth_state = {}
        runtime_cookies = self._runtime_auth_state.get(CONF_WEB_COOKIES)
        self._native_web_auth = bool(
            runtime_cookies or self.entry.data.get(CONF_WEB_COOKIES)
        )

    async def _async_save_screentime(self) -> None:
        """Persist saved screentime policies to HA storage."""
        await self._store.async_save(self._saved_screentime)

    async def _async_persist_runtime_auth(
        self,
        *,
        refresh_token: str | None = None,
        web_cookies: list[dict[str, Any]] | None = None,
        family_token: str | None = None,
        family_referer: str | None = None,
    ) -> None:
        """Persist rotated credentials without modifying/reloading the config entry."""
        next_state = dict(self._runtime_auth_state)
        next_state["anchor"] = self._entry_auth_anchor()
        if refresh_token:
            next_state[CONF_REFRESH_TOKEN] = refresh_token
        if web_cookies:
            next_state[CONF_WEB_COOKIES] = web_cookies
        if family_token:
            next_state[CONF_WEB_FAMILY_TOKEN] = family_token
        if family_referer:
            next_state[CONF_WEB_FAMILY_REFERER] = family_referer
        next_state["updated_at"] = dt_util.now().isoformat()

        comparable_old = {k: v for k, v in self._runtime_auth_state.items() if k != "updated_at"}
        comparable_new = {k: v for k, v in next_state.items() if k != "updated_at"}
        if comparable_old == comparable_new:
            return
        self._runtime_auth_state = next_state
        await self._auth_store.async_save(next_state)
        self._native_web_auth = bool(
            next_state.get(CONF_WEB_COOKIES) or self.entry.data.get(CONF_WEB_COOKIES)
        )
        _LOGGER.debug("Persisted rotated Microsoft Family Safety credentials")

    async def _async_persist_current_auth(self) -> None:
        if self.api is None:
            return
        authenticator = self.api.api.authenticator
        refresh_token = getattr(authenticator, "refresh_token", None)
        cookies = self.web_api.export_web_cookies() if self.web_api else None
        family_token = family_referer = None
        if self.web_api:
            family_token, family_referer = self.web_api.export_family_context()
        await self._async_persist_runtime_auth(
            refresh_token=refresh_token, web_cookies=cookies,
            family_token=family_token, family_referer=family_referer,
        )

    def _runtime_refresh_token(self) -> str:
        return str(
            self._runtime_auth_state.get(CONF_REFRESH_TOKEN)
            or self.entry.data[CONF_REFRESH_TOKEN]
        )

    async def _async_setup_api(self) -> None:
        refresh_token = self._runtime_refresh_token()
        apply_patches(self.hass)
        try:
            self.api = await FamilySafety.create(
                token=refresh_token,
                use_refresh_token=True,
                experimental=True,
            )
            self.web_api = FamilySafetyWebAPI(self.api.api.authenticator)
            # FamilySafety.create(use_refresh_token=True) itself performs a token
            # refresh. Persist a rotated token immediately so even a restart
            # during the first data poll can recover with the newest credential.
            await self._async_persist_current_auth()
        except HttpException as err:
            text = str(err).lower()
            if "401" in text or "403" in text or "authentication" in text:
                raise ConfigEntryAuthFailed(ERROR_AUTH_FAILED) from err
            raise UpdateFailed(f"Transient API error: {err}") from err
        except Exception as err:
            text = str(err).lower()
            if "auth" in text or "token" in text or "401" in text or "403" in text:
                raise ConfigEntryAuthFailed(ERROR_AUTH_FAILED) from err
            raise UpdateFailed(f"API setup error: {err}") from err

    async def _async_load_web_cookies(self) -> None:
        """Prefer cookies captured natively; retain the Playwright add-on fallback."""
        try:
            cookies: list[dict[str, Any]] | None = None
            if self._native_web_auth:
                raw = (
                    self._runtime_auth_state.get(CONF_WEB_COOKIES)
                    or self.entry.data.get(CONF_WEB_COOKIES)
                )
                if isinstance(raw, list):
                    cookies = raw
            else:
                cookies = await self._addon_client.load_cookies()

            if cookies and self.web_api:
                family_token = (
                    self._runtime_auth_state.get(CONF_WEB_FAMILY_TOKEN)
                    or self.entry.data.get(CONF_WEB_FAMILY_TOKEN)
                )
                family_referer = (
                    self._runtime_auth_state.get(CONF_WEB_FAMILY_REFERER)
                    or self.entry.data.get(CONF_WEB_FAMILY_REFERER)
                )
                self.web_api.set_web_cookies(
                    cookies,
                    family_token=str(family_token) if family_token else None,
                    family_referer=str(family_referer) if family_referer else None,
                )
                _LOGGER.debug(
                    "Loaded Microsoft Family web credentials: cookies=%d "
                    "family_token_present=%s family_referer_path=%s",
                    len(cookies), bool(family_token),
                    URL(str(family_referer)).path if family_referer else None,
                )
                self._web_cookies_loaded = True
            else:
                if self._web_cookies_loaded:
                    await self._create_auth_notification()
                self._web_cookies_loaded = False
                if not cookies and not self.entry.data.get(CONF_AUTH_URL):
                    self._request_reauth("web_credentials_missing")
        except Exception as err:
            self._web_cookies_loaded = False
            _LOGGER.debug("Could not load web cookies: %s", err)
            if not self.entry.data.get(CONF_AUTH_URL):
                self._request_reauth("web_credentials_missing")

    def get_account(self, account_id: str) -> Account | None:
        """Get the raw pyfamilysafety Account object."""
        return self._accounts.get(account_id)

    def get_device(self, device_id: str) -> Device | None:
        """Get the raw pyfamilysafety Device object."""
        return self._devices.get(device_id)

    def get_application(self, account_id: str, app_id: str) -> Application | None:
        """Get a raw pyfamilysafety Application object."""
        account = self._accounts.get(account_id)
        if account is None:
            return None
        try:
            return account.get_application(app_id)
        except (IndexError, ValueError):
            return None

    async def async_block_app(self, account_id: str, app_id: str) -> None:
        """Block an application."""
        app = self.get_application(account_id, app_id)
        if app is None:
            raise ValueError(f"Application {app_id} not found for account {account_id}")
        await app.block_app()
        await self.async_request_refresh()

    async def async_unblock_app(self, account_id: str, app_id: str) -> None:
        """Unblock an application."""
        app = self.get_application(account_id, app_id)
        if app is None:
            raise ValueError(f"Application {app_id} not found for account {account_id}")
        await app.unblock_app()
        await self.async_request_refresh()

    async def async_lock_platform(
        self, account_id: str, platform: str, valid_until: datetime | None = None
    ) -> None:
        account = self._accounts.get(account_id)
        if account is None:
            raise ValueError(f"Account {account_id} not found")
        until = valid_until or (datetime.now() + timedelta(hours=24))

        # The supplied browser HAR captures the current Windows implementation:
        # POST /family/api/device-limits/screentime-time-override with
        # X-JwtFamilyRelationshipToken and timeOverride=blockUntil.  Use it when
        # the relationship token can be recovered from the authenticated Family
        # web data. If Microsoft changes that private token shape, fall back to
        # the established pyfamilysafety mobile operation instead of breaking the
        # Home Assistant switch.
        if (
            platform.lower() == "windows"
            and self._native_web_auth
            and self.web_api is not None
            and self.web_api.has_web_cookies
        ):
            until_utc = dt_util.as_utc(until)
            date_time = until_utc.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            try:
                await self.web_api.set_windows_time_override(
                    account_id, "blockUntil", date_time
                )
            except Exception as err:
                _LOGGER.debug(
                    "Windows web override unavailable; falling back to mobile API: %s",
                    err,
                )
            else:
                self._platform_override_until[(account_id, "windows")] = until_utc
                await self.async_request_refresh()
                return

        target = OverrideTarget.from_pretty(platform)
        await account.override_device(target, OverrideType.UNTIL, until)
        await self.async_request_refresh()

    async def async_unlock_platform(self, account_id: str, platform: str) -> None:
        account = self._accounts.get(account_id)
        if account is None:
            raise ValueError(f"Account {account_id} not found")

        if (
            platform.lower() == "windows"
            and self._native_web_auth
            and self.web_api is not None
            and self.web_api.has_web_cookies
        ):
            until = self._platform_override_until.get(
                (account_id, "windows"), dt_util.utcnow()
            )
            date_time = dt_util.as_utc(until).isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            )
            try:
                await self.web_api.set_windows_time_override(
                    account_id, "cancel", date_time
                )
            except Exception as err:
                _LOGGER.debug(
                    "Windows web cancel unavailable; falling back to mobile API: %s",
                    err,
                )
            else:
                self._platform_override_until.pop((account_id, "windows"), None)
                await self.async_request_refresh()
                return

        await account.override_device(
            OverrideTarget.from_pretty(platform), OverrideType.CANCEL
        )
        self._platform_override_until.pop((account_id, platform.lower()), None)
        await self.async_request_refresh()

    async def async_approve_request(
        self, request_id: str, extension_time: int = 3600
    ) -> bool:
        """Approve a pending screen time request.

        ``extension_time`` is expressed in seconds.

        Workaround for issue #20: pyfamilysafety 1.1.2 converts the value with
        ``extension_time * 100`` while commenting "seconds to ms" — but seconds
        to milliseconds is ``* 1000``. The library therefore grants only a tenth
        of the requested time (e.g. 60 min -> 6 min). We pre-multiply by 10 so
        the library's faulty ``* 100`` yields the correct milliseconds
        (seconds * 10 * 100 == seconds * 1000).
        """
        if self.api is None:
            return False
        return await self.api.approve_pending_request(request_id, extension_time * 10)

    async def async_deny_request(self, request_id: str) -> bool:
        """Deny a pending screen time request."""
        if self.api is None:
            return False
        return await self.api.deny_pending_request(request_id)

    async def _fetch_screentime_policy(self, account_id: str) -> dict | None:
        if self._native_web_auth:
            return await self.web_api.get_screentime_policy(account_id) if self.web_api else None
        screentime = await self._addon_client.fetch_screentime(account_id)
        if screentime is None and self.web_api is not None:
            screentime = await self.web_api.get_screentime_policy(account_id)
        return screentime

    async def _set_screentime_allowance(
        self, child_id: str, day_of_week: int, hours: int, minutes: int
    ) -> bool:
        if self._native_web_auth:
            if self.web_api is None:
                raise RuntimeError("Web API not initialized")
            return await self.web_api.set_screentime_allowance(
                child_id, day_of_week, hours, minutes
            )
        return await self._addon_client.set_screentime_allowance(
            child_id, day_of_week, hours, minutes
        )

    async def _set_screentime_intervals(
        self, child_id: str, day_of_week: int, allowed_intervals: list[bool]
    ) -> bool:
        if self._native_web_auth:
            if self.web_api is None:
                raise RuntimeError("Web API not initialized")
            return await self.web_api.set_screentime_intervals(
                child_id, day_of_week, allowed_intervals
            )
        return await self._addon_client.set_screentime_intervals(
            child_id, day_of_week, allowed_intervals
        )

    async def async_set_screentime_limit(
        self, child_id: str, day_of_week: int, hours: int, minutes: int
    ) -> None:
        if not 0 <= day_of_week <= 6:
            raise ValueError("day_of_week must be between 0 and 6")
        if hours < 0 or minutes < 0 or minutes > 59 or hours * 60 + minutes > 1440:
            raise ValueError("screen-time allowance must be between 0 and 24 hours")
        await self._set_screentime_allowance(child_id, day_of_week, hours, minutes)
        await self.async_request_refresh()

    async def async_set_screentime_intervals(
        self,
        child_id: str,
        day_of_week: int,
        start_hour: int,
        start_minute: int,
        end_hour: int,
        end_minute: int,
    ) -> None:
        if not 0 <= day_of_week <= 6:
            raise ValueError("day_of_week must be between 0 and 6")
        start_total = start_hour * 60 + start_minute
        end_total = end_hour * 60 + end_minute
        if not 0 <= start_total < end_total <= 1440:
            raise ValueError("screen-time interval end must be later than start")
        await self._set_screentime_intervals(
            child_id,
            day_of_week,
            _range_to_slots(start_hour, start_minute, end_hour, end_minute),
        )
        await self.async_request_refresh()

    async def async_set_app_time_limit(
        self,
        child_id: str,
        app_id: str,
        display_name: str,
        platform: str,
        allowance: str,
        start_time: str = "07:00:00",
        end_time: str = "22:00:00",
    ) -> None:
        if not self.web_api:
            raise RuntimeError("Web API not initialized")
        await self.web_api.set_app_time_limit(
            child_id, app_id, display_name, platform, allowance, start_time, end_time
        )
        await self.async_request_refresh()

    async def async_remove_app_time_limit(
        self, child_id: str, app_id: str, display_name: str, platform: str
    ) -> None:
        if not self.web_api:
            raise RuntimeError("Web API not initialized")
        await self.web_api.remove_app_time_limit(
            child_id, app_id, display_name, platform
        )
        await self.async_request_refresh()

    async def async_block_website(self, child_id: str, website: str) -> None:
        if not self.web_api:
            raise RuntimeError("Web API not initialized")
        await self.web_api.block_website(child_id, website)
        await self.async_request_refresh()

    async def async_remove_website(self, child_id: str, website: str) -> None:
        if not self.web_api:
            raise RuntimeError("Web API not initialized")
        await self.web_api.remove_website(child_id, website)
        await self.async_request_refresh()

    async def async_toggle_web_filter(self, child_id: str, enabled: bool) -> None:
        if not self.web_api:
            raise RuntimeError("Web API not initialized")
        await self.web_api.toggle_web_filter(child_id, enabled)
        await self.async_request_refresh()

    async def async_set_age_rating(self, child_id: str, age: int) -> None:
        if not self.web_api:
            raise RuntimeError("Web API not initialized")
        await self.web_api.set_age_rating(child_id, age)
        await self.async_request_refresh()

    async def async_set_acquisition_policy(self, child_id: str, require_approval: bool) -> None:
        if not self.web_api:
            raise RuntimeError("Web API not initialized")
        await self.web_api.set_acquisition_policy(child_id, require_approval)
        await self.async_request_refresh()

    def is_account_locked(self, account_id: str) -> bool | None:
        account = (self.data or {}).get("accounts", {}).get(account_id)
        if not account:
            return None
        policy = account.get("screentime_policy")
        if not isinstance(policy, dict):
            return None
        daily = policy.get("dailyRestrictions") or policy.get("DailyRestrictions")
        if not isinstance(daily, dict):
            return None
        for day_key in DAY_KEYS:
            day_data = daily.get(day_key) or daily.get(day_key.capitalize())
            if not isinstance(day_data, dict):
                return None
            allowance = day_data.get("allowance") or day_data.get("Allowance") or "00:00:00"
            if allowance != "00:00:00":
                return False
        return True

    async def async_lock_account(self, account_id: str) -> None:
        current_policy = await self._fetch_screentime_policy(account_id)
        has_saved = account_id in self._saved_screentime
        if current_policy:
            daily = current_policy.get("dailyRestrictions") or current_policy.get("DailyRestrictions") or {}
            has_nonzero = any(
                (daily.get(key) or daily.get(key.capitalize()) or {}).get("allowance",
                    (daily.get(key) or daily.get(key.capitalize()) or {}).get("Allowance", "00:00:00"))
                != "00:00:00"
                for key in DAY_KEYS
            )
            if has_nonzero:
                self._saved_screentime[account_id] = current_policy
                await self._async_save_screentime()
        elif not has_saved:
            raise UpdateFailed(
                f"Cannot lock account {account_id}: current schedule unreadable and no saved "
                "policy exists. Reauthenticate the Microsoft Family web session."
            )

        failures = 0
        for day_index in range(7):
            try:
                await self._set_screentime_allowance(account_id, day_index, 0, 0)
                await self._set_screentime_intervals(account_id, day_index, [False] * 48)
            except Exception as err:
                failures += 1
                _LOGGER.warning("Could not lock day %d: %s", day_index, err)
        if failures:
            raise UpdateFailed(
                f"Screen-time account lock updated {7 - failures}/7 weekdays; "
                "the saved policy was kept so the operation can be retried safely"
            )
        await self.async_request_refresh()

    @staticmethod
    def _default_intervals() -> list[bool]:
        """Return default allowed intervals: 07:00-22:00 (slots 14-44)."""
        intervals = [False] * 48
        for i in range(14, 44):
            intervals[i] = True
        return intervals

    async def _restore_day(
        self,
        account_id: str,
        day_index: int,
        hours: int,
        minutes: int,
        intervals: list[bool] | None,
    ) -> bool:
        try:
            await self._set_screentime_allowance(account_id, day_index, hours, minutes)
            await self._set_screentime_intervals(
                account_id,
                day_index,
                intervals if intervals and len(intervals) == 48
                else self._default_intervals(),
            )
            return True
        except Exception as err:
            _LOGGER.warning("Failed to restore day %d: %s", day_index, err)
            return False

    async def async_unlock_account(self, account_id: str) -> None:
        saved = self._saved_screentime.get(account_id)
        if saved:
            daily = saved.get("dailyRestrictions") or saved.get("DailyRestrictions") or {}
            failures = 0
            for day_index, day_key in enumerate(DAY_KEYS):
                day_data = daily.get(day_key) or daily.get(day_key.capitalize()) or {}
                allowance = day_data.get("allowance") or day_data.get("Allowance") or "02:00:00"
                try:
                    parts = allowance.split(":")
                    hours, minutes = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
                except (ValueError, IndexError):
                    hours, minutes = 2, 0
                intervals = _policy_intervals_to_slots(day_data)
                if not await self._restore_day(
                    account_id, day_index, hours, minutes, intervals
                ):
                    failures += 1
            if failures:
                # Do not discard the only copy of the pre-lock schedule after a
                # partial restore. A later retry can safely finish the remaining
                # weekdays.
                raise UpdateFailed(
                    f"Screen-time restore updated {7 - failures}/7 weekdays; "
                    "saved policy retained for retry"
                )
            self._saved_screentime.pop(account_id, None)
            await self._async_save_screentime()
        else:
            for day_index in range(7):
                await self._restore_day(account_id, day_index, 2, 0, None)
        await self.async_request_refresh()

    def is_policy_enabled(self, account_id: str) -> bool | None:
        locked = self.is_account_locked(account_id)
        if locked is None:
            return None
        if locked:
            # All days at 0 → fully locked, which is still "limits enabled".
            return True
        account = (self.data or {}).get("accounts", {}).get(account_id)
        policy = (account or {}).get("screentime_policy")
        if not isinstance(policy, dict):
            return None
        daily = policy.get("dailyRestrictions") or policy.get("DailyRestrictions")
        if not isinstance(daily, dict):
            return None
        for day_key in DAY_KEYS:
            day_data = daily.get(day_key) or daily.get(day_key.capitalize()) or {}
            allowance = day_data.get("allowance") or day_data.get("Allowance") or "24:00:00"
            if allowance != "24:00:00":
                return True
        return False

    async def async_set_policy_enabled(self, account_id: str, enabled: bool) -> None:
        if enabled:
            # Re-enable limits: restore the saved schedule (same path as unlock).
            await self.async_unlock_account(account_id)
            return
        current_policy = await self._fetch_screentime_policy(account_id)
        has_saved = account_id in self._saved_screentime
        if current_policy and not has_saved:
            self._saved_screentime[account_id] = current_policy
            await self._async_save_screentime()
        elif current_policy is None and not has_saved:
            raise UpdateFailed(
                "Cannot disable screen-time limits because the current schedule "
                "could not be read and no saved policy exists"
            )

        failures = 0
        for day_index in range(7):
            try:
                await self._set_screentime_allowance(account_id, day_index, 24, 0)
                await self._set_screentime_intervals(account_id, day_index, [True] * 48)
            except Exception as err:
                failures += 1
                _LOGGER.warning("Could not disable screen-time limits for day %d: %s", day_index, err)
        if failures:
            raise UpdateFailed(
                f"Screen-time policy disable updated {7 - failures}/7 weekdays; "
                "saved policy retained for retry"
            )
        await self.async_request_refresh()

    def connection_state(self) -> dict[str, Any]:
        mobile_ok = self.api is not None and self.last_update_success
        has_cookies = bool(self.web_api and self.web_api.has_web_cookies)
        web_error = self.web_api.last_web_error_code if self.web_api else None
        if not has_cookies:
            web_state = "missing"
        elif self.web_api is None:
            web_state = "unknown"
        else:
            web_state = self.web_api.web_session_state

        # Only a successful authenticated Family-dashboard probe proves that the
        # browser session is healthy. Cookie presence alone is insufficient.
        # Do not report False for a timeout/network error: that means "unknown",
        # not "authentication rejected".
        if web_state == "authenticated":
            web_authenticated: bool | None = True
        elif web_state in ("missing", "expired"):
            web_authenticated = False
        else:
            web_authenticated = None
        web_ok = web_authenticated is True
        state = "connected" if mobile_ok and web_ok else "degraded" if mobile_ok else "disconnected"
        reauth_recommended = bool(
            self._native_web_auth
            and web_state == "error"
            and web_error in ("TIMEOUT", "NETWORK_ERROR")
        )
        return {
            "state": state,
            "mobile_api": "ok" if mobile_ok else "error",
            "web_session": web_state,
            "web_session_authenticated": web_authenticated,
            "web_session_last_checked": (
                self.web_api.web_session_last_checked if self.web_api else None
            ),
            "web_session_last_http_status": (
                self.web_api.web_session_last_http_status if self.web_api else None
            ),
            "web_last_error_code": web_error,
            "web_api": self.web_api.web_api_state if self.web_api else "unavailable",
            "web_api_last_checked": (
                self.web_api.web_api_last_checked if self.web_api else None
            ),
            "web_api_last_http_status": (
                self.web_api.web_api_last_http_status if self.web_api else None
            ),
            "web_api_last_endpoint": (
                self.web_api.web_api_last_endpoint if self.web_api else None
            ),
            "family_context": (
                self.web_api.family_context_state if self.web_api else "unavailable"
            ),
            "family_context_last_checked": (
                self.web_api.family_context_last_checked if self.web_api else None
            ),
            "family_context_last_http_status": (
                self.web_api.family_context_last_http_status if self.web_api else None
            ),
            "family_context_last_path": (
                self.web_api.family_context_last_path if self.web_api else None
            ),
            "family_token_source": (
                self.web_api.family_token_source if self.web_api else None
            ),
            "screentime_policy_status": (
                self.web_api.screentime_policy_status if self.web_api else "unavailable"
            ),
            "screentime_policy_source": (
                self.web_api.screentime_policy_source if self.web_api else None
            ),
            "cookies_loaded": self._web_cookies_loaded,
            "native_web_auth": self._native_web_auth,
            "runtime_credentials_persisted": bool(self._runtime_auth_state),
            "reauth_requested": self._reauth_requested,
            "reauth_required": self._reauth_requested,
            "reauth_reason": self._reauth_reason,
            "reauth_recommended": reauth_recommended,
            "last_update_success": self.last_update_success,
        }

    async def _fetch_web_api_data(self, account_id: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "web_browsing": None,
            "screentime_policy": None,
        }
        if not self.web_api:
            return result
        try:
            result["web_browsing"] = await self.web_api.get_web_browsing_settings(account_id)
        except Exception as err:
            _LOGGER.debug("Could not fetch web browsing settings: %r", err)
        try:
            # ContentRestrictions is still an established pyfamilysafety 1.1.2
            # mobile endpoint. Fetch it separately so age-rating and
            # acquisition-policy state are visible instead of exposing write-only
            # services with no readback. Failure stays non-fatal because Microsoft
            # can gate individual private endpoints independently.
            result["content_settings"] = await self.web_api.get_content_settings(account_id)
        except Exception as err:
            _LOGGER.debug("Could not fetch content settings: %r", err)
        try:
            screentime = await self._fetch_screentime_policy(account_id)
            expired = (
                self.web_api.web_session_state == "expired"
                if self._native_web_auth
                else self._addon_client.last_error_code == "LOGIN_REDIRECT"
            )
            if screentime is None and expired:
                await self._create_auth_notification()
                if self._native_web_auth:
                    self._request_reauth("web_session_expired")
            elif screentime is not None:
                await self._dismiss_auth_notification()
            result["screentime_policy"] = screentime
        except Exception as err:
            _LOGGER.debug("Could not fetch screen time policy: %r", err)

        # Only an actual browser-login redirect or an independently expired
        # account session proves that reauthentication is required.  A private
        # Family API 401 can also be caused by a stale/wrong Family SPA
        # request-verification token, so do not create a reauth loop while the
        # /account session probe is still authenticated.
        if self._native_web_auth and (
            self.web_api.last_web_error_code == "LOGIN_REDIRECT"
            or self.web_api.web_session_state == "expired"
        ):
            await self._create_auth_notification()
            self._request_reauth("web_session_expired")
        elif (
            self._native_web_auth
            and self.web_api.last_web_error_code == "AUTH_ERROR"
            and self.web_api.web_session_state == "authenticated"
        ):
            _LOGGER.warning(
                "Microsoft Family private web API rejected a request while the "
                "Microsoft account session is authenticated; keeping reauth disabled "
                "and treating this as Family API context/authentication failure"
            )
        return result

    def _request_reauth(self, reason: str) -> None:
        """Ask Home Assistant to start a linked reauthentication flow.

        ConfigEntry.async_start_reauth() de-duplicates active flows itself. Calling
        it again on later polls means a deliberately aborted/expired repair can be
        offered again without requiring a Home Assistant restart.
        """
        if not self._reauth_requested:
            _LOGGER.warning(
                "Microsoft Family Safety authentication requires renewal (%s)", reason
            )
        self._reauth_requested = True
        self._reauth_reason = reason
        self.entry.async_start_reauth(self.hass, data={"reason": reason})

    def request_reauth(self, reason: str = "manual") -> None:
        """Start the normal Home Assistant reauthentication flow on demand."""
        self._request_reauth(reason)

    def _transform_account_data(self, account: Account) -> tuple[str, dict[str, Any]]:
        account_id = account.user_id
        today = dt_util.now().date().isoformat()
        raw_ms = int(account.today_screentime_usage or 0)
        raw_minutes = _ms_to_minutes(raw_ms)
        polled_at = dt_util.now().isoformat()

        # Expose Microsoft's current aggregate directly.  Do not clamp the
        # value to a locally remembered maximum: Home Assistant's recorder and
        # history UI are responsible for historical presentation, while the
        # entity state should reflect the latest API response verbatim.
        data = {
            "user_id": account.user_id,
            "first_name": account.first_name,
            "surname": account.surname,
            "profile_picture": account.profile_picture,
            "today_screentime_usage": raw_minutes,
            "raw_today_screentime_usage": raw_minutes,
            "raw_today_screentime_ms": raw_ms,
            "screen_time_polled_at": polled_at,
            "screen_time_date": today,
            "average_screentime_usage": _ms_to_minutes(account.average_screentime_usage),
            "account_balance": account.account_balance,
            "account_currency": account.account_currency,
            "blocked_platforms": [str(p) for p in account.blocked_platforms] if account.blocked_platforms else [],
            "devices": [],
            "applications": [
                {
                    "app_id": app.app_id,
                    "app_name": app.name,
                    "blocked": app.blocked,
                    "icon": app.icon,
                    "usage_minutes": round(app.usage, 1) if app.usage else 0,
                }
                for app in account.applications
            ],
        }
        return account_id, data

    def _transform_device_data(self, device: Device, account_id: str) -> tuple[str, dict[str, Any]]:
        return device.device_id, {
            "device_id": device.device_id,
            "device_name": device.device_name,
            "device_class": device.device_class,
            "device_make": device.device_make,
            "device_model": device.device_model,
            "os_name": device.os_name,
            "today_time_used": _ms_to_minutes(device.today_time_used),
            "last_seen": device.last_seen,
            "blocked": device.blocked,
            "account_id": account_id,
        }

    async def _async_update_data(self) -> dict[str, Any]:
        if self.api is None:
            await self._async_setup_api()
        await self._async_load_web_cookies()
        assert self.api is not None
        try:
            # Verify the browser session independently from individual private
            # API endpoints. This makes the Connection sensor authoritative and
            # ensures an expired web login starts HA reauthentication even when
            # the mobile API is still healthy. Network errors are reported as
            # such and do not trigger a needless login flow.
            if self._native_web_auth and self.web_api is not None:
                web_authenticated = await self.web_api.async_check_web_session()
                if web_authenticated:
                    await self._dismiss_auth_notification()
                    self._reauth_requested = False
                    self._reauth_reason = None
                elif self.web_api.web_session_state in ("missing", "expired"):
                    await self._create_auth_notification()
                    self._request_reauth(
                        "web_credentials_missing"
                        if self.web_api.web_session_state == "missing"
                        else "web_session_expired"
                    )
            await self.api.update()
            if not getattr(self.api, "accounts", None):
                self.api.accounts = []
            accounts_data: dict[str, Any] = {}
            devices_data: dict[str, Any] = {}
            new_accounts: dict[str, Account] = {}
            new_devices: dict[str, Device] = {}
            for account in self.api.accounts:
                account_id, account_data = self._transform_account_data(account)
                accounts_data[account_id] = account_data
                new_accounts[account_id] = account
                for device in account.devices:
                    device_id, device_data = self._transform_device_data(device, account_id)
                    devices_data[device_id] = device_data
                    accounts_data[account_id]["devices"].append(device_id)
                    new_devices[device_id] = device
                web_data = await self._fetch_web_api_data(account_id)
                # A transient failure of one private web endpoint must not turn
                # an otherwise healthy entity into `unknown` for the next poll.
                # Keep the previous successful value in memory until Microsoft
                # returns a replacement.  This is not a screen-time usage guard:
                # it applies only to configuration/policy payloads and never
                # modifies measured usage values.
                previous = ((self.data or {}).get("accounts", {}).get(account_id) or {})
                for key in ("web_browsing", "content_settings", "screentime_policy"):
                    if web_data.get(key) is None and previous.get(key) is not None:
                        web_data[key] = previous.get(key)
                accounts_data[account_id].update(web_data)
            self._accounts = new_accounts
            self._devices = new_devices
            pending = getattr(self.api, "pending_requests", None) or []
            self._is_retrying_auth = False
            return {"accounts": accounts_data, "devices": devices_data, "pending_requests": pending}
        except HttpException as err:
            if "401" in str(err) or "authentication" in str(err).lower():
                if not self._is_retrying_auth:
                    self._is_retrying_auth = True
                    self.web_api = None
                    raise ConfigEntryAuthFailed(ERROR_TOKEN_EXPIRED) from err
                raise UpdateFailed(f"Authentication failed: {err}") from err
            raise UpdateFailed(f"Error communicating with API: {err}") from err
        except ConfigEntryAuthFailed:
            raise
        except Exception as err:
            _LOGGER.exception("Unexpected error fetching Family Safety data")
            raise UpdateFailed(f"Unexpected error: {err}") from err
        finally:
            try:
                await self._async_persist_current_auth()
            except Exception as persist_err:
                _LOGGER.debug(
                    "Could not persist rotated Family Safety credentials: %s",
                    persist_err,
                )

    async def _create_auth_notification(self) -> None:
        if self._auth_notification_sent:
            return
        message = (
            "Your Microsoft Family Safety web session is missing or has expired.\n\n"
            "Home Assistant has started a reauthentication flow. Open the integration "
            "or the Repairs page and complete the Microsoft sign-in to renew both the "
            "mobile token and Family web session."
            if self._native_web_auth
            else
            "Your Microsoft Family Safety web session has expired. Re-authenticate using the Family Safety Auth add-on."
        )
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {"title": "Microsoft Family Safety - Authentication Required", "message": message,
             "notification_id": AUTH_NOTIFICATION_ID},
        )
        self._auth_notification_sent = True

    async def _dismiss_auth_notification(self) -> None:
        if not self._auth_notification_sent:
            return
        await self.hass.services.async_call(
            "persistent_notification",
            "dismiss",
            {"notification_id": AUTH_NOTIFICATION_ID},
        )
        self._auth_notification_sent = False

    async def async_cleanup(self) -> None:
        self._accounts.clear()
        self._devices.clear()
        if self.web_api:
            await self.web_api.close()
        self.web_api = None
        self.api = None
