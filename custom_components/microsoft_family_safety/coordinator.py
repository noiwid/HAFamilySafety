"""DataUpdateCoordinator for Microsoft Family Safety.

Dual authentication strategy:
- Mobile API (MSAuth1.0 token via pyfamilysafety) — for writes and basic reads
- Web API (browser cookies from Playwright addon) — for screen time schedule reads
"""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from pyfamilysafety import FamilySafety
from pyfamilysafety.account import Account
from pyfamilysafety.application import Application
from pyfamilysafety.device import Device
from pyfamilysafety.enum import OverrideTarget, OverrideType
from pyfamilysafety.exceptions import HttpException

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api_client import FamilySafetyWebAPI, FamilySafetyWebAPIError
from .auth.addon_client import AddonCookieClient
from .const import (
    CONF_AUTH_URL,
    CONF_REFRESH_TOKEN,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    ERROR_AUTH_FAILED,
    ERROR_TOKEN_EXPIRED,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}.saved_screentime"
STORAGE_VERSION = 1


def _ms_to_minutes(milliseconds: int | None) -> int:
    """Convert milliseconds to minutes."""
    if not milliseconds:
        return 0
    return int(milliseconds / 60000)


class FamilySafetyDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Class to manage fetching Microsoft Family Safety data."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
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
        # Saved screentime state for lock/unlock per account (persisted via HA Store)
        self._saved_screentime: dict[str, dict[str, Any]] = {}
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        # Addon cookie client for web API reads
        auth_url = entry.data.get(CONF_AUTH_URL) or entry.options.get(CONF_AUTH_URL)
        self._addon_client = AddonCookieClient(hass, auth_url=auth_url)
        self._web_cookies_loaded = False
        self._auth_notification_sent = False

    async def async_load_saved_screentime(self) -> None:
        """Load saved screentime policies from persistent storage."""
        data = await self._store.async_load()
        if data and isinstance(data, dict):
            self._saved_screentime = data
            _LOGGER.debug(
                "Loaded saved screentime policies for %d account(s)",
                len(self._saved_screentime),
            )

    async def _async_save_screentime(self) -> None:
        """Persist saved screentime policies to HA storage."""
        await self._store.async_save(self._saved_screentime)

    async def _async_setup_api(self) -> None:
        """Set up the Family Safety API client."""
        refresh_token = self.entry.data[CONF_REFRESH_TOKEN]

        try:
            self.api = await FamilySafety.create(
                token=refresh_token,
                use_refresh_token=True,
                experimental=True,
            )

            # Initialize web API client using the same authenticator
            self.web_api = FamilySafetyWebAPI(self.api.api.authenticator)

            _LOGGER.debug("Family Safety API client initialized successfully")
        except HttpException as err:
            err_str = str(err).lower()
            if "401" in err_str or "403" in err_str or "authentication" in err_str:
                _LOGGER.error("Authentication failed during API setup: %s", err)
                raise ConfigEntryAuthFailed(ERROR_AUTH_FAILED) from err
            _LOGGER.warning("Transient API error during setup, will retry: %s", err)
            raise UpdateFailed(f"Transient API error: {err}") from err
        except Exception as err:
            err_str = str(err).lower()
            if "auth" in err_str or "token" in err_str or "401" in err_str or "403" in err_str:
                _LOGGER.error("Authentication failed during API setup: %s", err)
                raise ConfigEntryAuthFailed(ERROR_AUTH_FAILED) from err
            _LOGGER.warning("Unexpected error during API setup, will retry: %s", err)
            raise UpdateFailed(f"API setup error: {err}") from err

    async def _async_load_web_cookies(self) -> None:
        """Load browser cookies from the Playwright auth addon."""
        try:
            cookies = await self._addon_client.load_cookies()
            if cookies and self.web_api:
                self.web_api.set_web_cookies(cookies)
                self._web_cookies_loaded = True
                if self._auth_notification_sent:
                    self._auth_notification_sent = False
                _LOGGER.info(
                    "Web cookies loaded from addon (%d cookies) — "
                    "screen time schedule reading enabled",
                    len(cookies),
                )
            else:
                _LOGGER.debug(
                    "No web cookies available from addon — "
                    "screen time schedule reading disabled. "
                    "Install the Family Safety Auth add-on for full support."
                )
        except Exception as err:
            _LOGGER.debug("Could not load web cookies: %s", err)

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

    # ──────────────────────────────────────────────────────────────────────
    # Existing controls (via pyfamilysafety)
    # ──────────────────────────────────────────────────────────────────────

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
        """Lock a platform (Windows/Xbox/Mobile)."""
        account = self._accounts.get(account_id)
        if account is None:
            raise ValueError(f"Account {account_id} not found")
        target = OverrideTarget.from_pretty(platform)
        if valid_until is None:
            valid_until = datetime.now() + timedelta(hours=24)
        await account.override_device(target, OverrideType.UNTIL, valid_until)
        await self.async_request_refresh()

    async def async_unlock_platform(self, account_id: str, platform: str) -> None:
        """Unlock a platform (Windows/Xbox/Mobile)."""
        account = self._accounts.get(account_id)
        if account is None:
            raise ValueError(f"Account {account_id} not found")
        target = OverrideTarget.from_pretty(platform)
        await account.override_device(target, OverrideType.CANCEL)
        await self.async_request_refresh()

    async def async_approve_request(
        self, request_id: str, extension_time: int = 3600
    ) -> bool:
        """Approve a pending screen time request."""
        if self.api is None:
            return False
        return await self.api.approve_pending_request(request_id, extension_time)

    async def async_deny_request(self, request_id: str) -> bool:
        """Deny a pending screen time request."""
        if self.api is None:
            return False
        return await self.api.deny_pending_request(request_id)

    # ──────────────────────────────────────────────────────────────────────
    # Controls via web API (mobile API writes)
    # ──────────────────────────────────────────────────────────────────────

    async def async_set_screentime_limit(
        self, child_id: str, day_of_week: int, hours: int, minutes: int
    ) -> None:
        """Set screen time daily allowance via addon browser."""
        await self._addon_client.set_screentime_allowance(
            child_id, day_of_week, hours, minutes
        )
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
        """Set screen time allowed intervals via addon browser."""
        # Build 48-slot boolean array from start/end range
        intervals = [False] * 48
        start_slot = start_hour * 2 + (1 if start_minute >= 30 else 0)
        end_slot = end_hour * 2 + (1 if end_minute >= 30 else 0)
        for i in range(start_slot, min(end_slot, 48)):
            intervals[i] = True
        await self._addon_client.set_screentime_intervals(
            child_id, day_of_week, intervals
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
        """Set a per-app time limit."""
        if self.web_api is None:
            raise RuntimeError("Web API not initialized")
        await self.web_api.set_app_time_limit(
            child_id, app_id, display_name, platform, allowance, start_time, end_time
        )
        await self.async_request_refresh()

    async def async_remove_app_time_limit(
        self, child_id: str, app_id: str, display_name: str, platform: str
    ) -> None:
        """Remove a per-app time limit."""
        if self.web_api is None:
            raise RuntimeError("Web API not initialized")
        await self.web_api.remove_app_time_limit(
            child_id, app_id, display_name, platform
        )
        await self.async_request_refresh()

    async def async_block_website(self, child_id: str, website: str) -> None:
        """Block a website."""
        if self.web_api is None:
            raise RuntimeError("Web API not initialized")
        await self.web_api.block_website(child_id, website)
        await self.async_request_refresh()

    async def async_remove_website(self, child_id: str, website: str) -> None:
        """Remove a website from block/allow list."""
        if self.web_api is None:
            raise RuntimeError("Web API not initialized")
        await self.web_api.remove_website(child_id, website)
        await self.async_request_refresh()

    async def async_toggle_web_filter(self, child_id: str, enabled: bool) -> None:
        """Toggle web filtering."""
        if self.web_api is None:
            raise RuntimeError("Web API not initialized")
        await self.web_api.toggle_web_filter(child_id, enabled)
        await self.async_request_refresh()

    async def async_set_age_rating(self, child_id: str, age: int) -> None:
        """Set content age rating."""
        if self.web_api is None:
            raise RuntimeError("Web API not initialized")
        await self.web_api.set_age_rating(child_id, age)
        await self.async_request_refresh()

    async def async_set_acquisition_policy(
        self, child_id: str, require_approval: bool
    ) -> None:
        """Set ask-to-buy policy."""
        if self.web_api is None:
            raise RuntimeError("Web API not initialized")
        await self.web_api.set_acquisition_policy(child_id, require_approval)
        await self.async_request_refresh()

    # ──────────────────────────────────────────────────────────────────────
    # Account lock/unlock (screen time based)
    # ──────────────────────────────────────────────────────────────────────

    DAYS_OF_WEEK = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]

    def is_account_locked(self, account_id: str) -> bool | None:
        """Check if an account is currently locked (all 7 days at 0 minutes)."""
        if not self.data:
            return None
        account = self.data.get("accounts", {}).get(account_id)
        if not account:
            return None
        policy = account.get("screentime_policy")
        if not policy or not isinstance(policy, dict):
            return None
        daily = policy.get("dailyRestrictions") or policy.get("DailyRestrictions")
        if not daily or not isinstance(daily, dict):
            return None
        for day_key in self.DAYS_OF_WEEK:
            day_data = daily.get(day_key) or daily.get(day_key.capitalize())
            if not day_data:
                return None
            allowance = day_data.get("allowance") or day_data.get("Allowance") or "00:00:00"
            if allowance != "00:00:00":
                return False
        return True

    async def async_lock_account(self, account_id: str) -> None:
        """Lock an account by setting all 7-day screen time quotas to 0."""
        if self.web_api is None:
            raise RuntimeError("Web API not initialized")

        # Save current screentime policy before zeroing
        current_policy = await self._addon_client.fetch_screentime(account_id)
        if current_policy is None:
            current_policy = await self.web_api.get_screentime_policy(account_id)
        if current_policy:
            daily = current_policy.get("dailyRestrictions") or current_policy.get("DailyRestrictions") or {}
            has_nonzero = False
            for day_key in self.DAYS_OF_WEEK:
                day_data = daily.get(day_key) or daily.get(day_key.capitalize()) or {}
                allowance = day_data.get("allowance") or day_data.get("Allowance") or "00:00:00"
                if allowance != "00:00:00":
                    has_nonzero = True
                    break
            if has_nonzero:
                self._saved_screentime[account_id] = current_policy
                await self._async_save_screentime()
                _LOGGER.info(
                    "Saved screentime policy for account %s before locking",
                    account_id,
                )

        # Set all 7 days to 0 minutes
        days_locked = 0
        try:
            for day_index in range(7):
                await self.web_api.set_screentime_daily_allowance(
                    account_id, day_index, hours=0, minutes=0
                )
                await self.web_api.set_screentime_intervals(
                    account_id, day_index, [False] * 48
                )
                days_locked += 1
        except Exception as err:
            _LOGGER.error(
                "Lock failed on day %d/7 for account %s: %s",
                days_locked, account_id, err,
            )
            for remaining in range(days_locked, 7):
                try:
                    await self.web_api.set_screentime_daily_allowance(
                        account_id, remaining, hours=0, minutes=0
                    )
                    await self.web_api.set_screentime_intervals(
                        account_id, remaining, [False] * 48
                    )
                    days_locked += 1
                except Exception:
                    _LOGGER.warning(
                        "Could not lock day %d for account %s", remaining, account_id
                    )

        _LOGGER.info(
            "Account %s locked (%d/7 days set to 0)", account_id, days_locked
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
        self, account_id: str, day_index: int, hours: int, minutes: int,
        intervals: list[bool] | None,
    ) -> bool:
        """Restore a single day's screentime. Returns True on success."""
        try:
            await self.web_api.set_screentime_daily_allowance(
                account_id, day_index, hours, minutes
            )
            effective_intervals = (
                intervals if intervals and len(intervals) == 48
                else self._default_intervals()
            )
            await self.web_api.set_screentime_intervals(
                account_id, day_index, effective_intervals
            )
            return True
        except Exception as err:
            _LOGGER.warning(
                "Failed to restore day %d for account %s: %s",
                day_index, account_id, err,
            )
            return False

    async def async_unlock_account(self, account_id: str) -> None:
        """Unlock an account by restoring saved screen time quotas."""
        if self.web_api is None:
            raise RuntimeError("Web API not initialized")

        saved = self._saved_screentime.get(account_id)
        days_restored = 0

        if saved:
            daily = saved.get("dailyRestrictions") or saved.get("DailyRestrictions") or {}
            for day_index, day_key in enumerate(self.DAYS_OF_WEEK):
                day_data = daily.get(day_key) or daily.get(day_key.capitalize()) or {}
                allowance = day_data.get("allowance") or day_data.get("Allowance") or "02:00:00"
                try:
                    parts = allowance.split(":")
                    hours = int(parts[0])
                    minutes = int(parts[1]) if len(parts) > 1 else 0
                except (ValueError, IndexError):
                    hours, minutes = 2, 0

                intervals = day_data.get("allowedIntervals") or day_data.get("AllowedIntervals")
                if await self._restore_day(account_id, day_index, hours, minutes, intervals):
                    days_restored += 1

            self._saved_screentime.pop(account_id, None)
            await self._async_save_screentime()
            _LOGGER.info(
                "Account %s unlocked (%d/7 days restored from saved policy)",
                account_id, days_restored,
            )
        else:
            _LOGGER.warning(
                "No saved screentime policy for account %s, restoring defaults (2h/day)",
                account_id,
            )
            for day_index in range(7):
                if await self._restore_day(account_id, day_index, 2, 0, None):
                    days_restored += 1

            _LOGGER.info(
                "Account %s unlocked (%d/7 days restored with defaults)",
                account_id, days_restored,
            )

        await self.async_request_refresh()

    # ──────────────────────────────────────────────────────────────────────
    # Data fetching
    # ──────────────────────────────────────────────────────────────────────

    async def _fetch_web_api_data(self, account_id: str) -> dict[str, Any]:
        """Fetch additional data from the web API for an account."""
        result: dict[str, Any] = {
            "web_browsing": None,
            "screentime_policy": None,
        }
        if self.web_api is None:
            return result

        try:
            web_browsing = await self.web_api.get_web_browsing_settings(account_id)
            result["web_browsing"] = web_browsing
        except Exception as err:
            _LOGGER.debug("Could not fetch web browsing settings: %s", err)

        try:
            # Use addon's browser-based fetch (avoids 401 from direct cookie replay)
            screentime = await self._addon_client.fetch_screentime(account_id)
            if screentime is None:
                # Fallback to direct web API call
                screentime = await self.web_api.get_screentime_policy(account_id)
            _LOGGER.debug("Screen time policy response for %s: %s", account_id, screentime)
            result["screentime_policy"] = screentime
        except Exception as err:
            _LOGGER.debug("Could not fetch screen time policy: %s", err)

        return result

    # ──────────────────────────────────────────────────────────────────────
    # Data transformation
    # ──────────────────────────────────────────────────────────────────────

    def _transform_account_data(self, account: Account) -> tuple[str, dict[str, Any]]:
        """Transform an Account object to dictionary format."""
        account_id = account.user_id

        blocked_platforms_list = []
        if account.blocked_platforms:
            blocked_platforms_list = [str(p) for p in account.blocked_platforms]

        account_data = {
            "user_id": account.user_id,
            "first_name": account.first_name,
            "surname": account.surname,
            "profile_picture": account.profile_picture,
            "today_screentime_usage": _ms_to_minutes(account.today_screentime_usage),
            "average_screentime_usage": _ms_to_minutes(account.average_screentime_usage),
            "account_balance": account.account_balance,
            "account_currency": account.account_currency,
            "blocked_platforms": blocked_platforms_list,
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
        return account_id, account_data

    def _transform_device_data(self, device: Device, account_id: str) -> tuple[str, dict[str, Any]]:
        """Transform a Device object to dictionary format."""
        device_id = device.device_id
        device_data = {
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
        return device_id, device_data

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Family Safety API."""
        if self.api is None:
            await self._async_setup_api()

        # Load/refresh web cookies from addon (every poll cycle)
        await self._async_load_web_cookies()

        try:
            await self.api.update()

            if not hasattr(self.api, 'accounts') or self.api.accounts is None:
                _LOGGER.warning("API accounts is None after update, initializing to empty list")
                self.api.accounts = []

            accounts_data = {}
            devices_data = {}

            _LOGGER.debug("Found %d Family Safety accounts", len(self.api.accounts))

            for account in self.api.accounts:
                account_id, account_data = self._transform_account_data(account)
                accounts_data[account_id] = account_data
                self._accounts[account_id] = account

                for device in account.devices:
                    device_id, device_data = self._transform_device_data(device, account_id)
                    devices_data[device_id] = device_data
                    accounts_data[account_id]["devices"].append(device_id)
                    self._devices[device_id] = device

                # Fetch web API data for this account
                web_data = await self._fetch_web_api_data(account_id)
                accounts_data[account_id]["web_browsing"] = web_data.get("web_browsing")
                accounts_data[account_id]["screentime_policy"] = web_data.get("screentime_policy")

            # Collect pending requests
            pending_requests = []
            if hasattr(self.api, 'pending_requests') and self.api.pending_requests:
                pending_requests = self.api.pending_requests

            return {
                "accounts": accounts_data,
                "devices": devices_data,
                "pending_requests": pending_requests,
            }

        except HttpException as err:
            if "401" in str(err) or "authentication" in str(err).lower():
                if not self._is_retrying_auth:
                    _LOGGER.warning("Authentication failed, token may be expired")
                    self._is_retrying_auth = True
                    self.web_api = None
                    raise ConfigEntryAuthFailed(ERROR_TOKEN_EXPIRED) from err
                raise UpdateFailed(f"Authentication failed: {err}") from err
            raise UpdateFailed(f"Error communicating with API: {err}") from err
        except Exception as err:
            _LOGGER.exception("Unexpected error fetching data: %s", err)
            raise UpdateFailed(f"Unexpected error: {err}") from err

    async def _create_auth_notification(self) -> None:
        """Create a persistent notification when web cookies expire."""
        if self._auth_notification_sent:
            return

        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "Microsoft Family Safety - Authentication Required",
                "message": (
                    "Your Microsoft Family Safety web session has expired.\n\n"
                    "Please re-authenticate using the **Family Safety Auth** add-on:\n"
                    "1. Open the add-on in Supervisor\n"
                    "2. Click 'Open Web UI'\n"
                    "3. Log in with your Microsoft account\n"
                    "4. The integration will automatically resume once authenticated."
                ),
                "notification_id": "familysafety_auth_expired",
            },
        )
        self._auth_notification_sent = True
        _LOGGER.info("Created web authentication notification for user")

    async def async_cleanup(self) -> None:
        """Clean up resources."""
        self._accounts.clear()
        self._devices.clear()
        if self.web_api:
            await self.web_api.close()
            self.web_api = None
        self.api = None
