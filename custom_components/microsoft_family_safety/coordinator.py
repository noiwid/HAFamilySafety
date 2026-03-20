"""DataUpdateCoordinator for Microsoft Family Safety."""
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
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api_client import FamilySafetyWebAPI, FamilySafetyWebAPIError
from .const import (
    CONF_REFRESH_TOKEN,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    ERROR_AUTH_FAILED,
    ERROR_TOKEN_EXPIRED,
)

_LOGGER = logging.getLogger(__name__)


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
            # Transient server error (e.g. "upstream aggregator error") — retry later
            _LOGGER.warning("Transient API error during setup, will retry: %s", err)
            raise UpdateFailed(f"Transient API error: {err}") from err
        except Exception as err:
            err_str = str(err).lower()
            if "auth" in err_str or "token" in err_str or "401" in err_str or "403" in err_str:
                _LOGGER.error("Authentication failed during API setup: %s", err)
                raise ConfigEntryAuthFailed(ERROR_AUTH_FAILED) from err
            _LOGGER.warning("Unexpected error during API setup, will retry: %s", err)
            raise UpdateFailed(f"API setup error: {err}") from err

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
        """Approve a pending screen time request. extension_time in seconds."""
        if self.api is None:
            return False
        return await self.api.approve_pending_request(request_id, extension_time)

    async def async_deny_request(self, request_id: str) -> bool:
        """Deny a pending screen time request."""
        if self.api is None:
            return False
        return await self.api.deny_pending_request(request_id)

    # ──────────────────────────────────────────────────────────────────────
    # New controls (via web API)
    # ──────────────────────────────────────────────────────────────────────

    async def async_set_screentime_limit(
        self, child_id: str, day_of_week: int, hours: int, minutes: int
    ) -> None:
        """Set screen time daily allowance."""
        if self.web_api is None:
            raise RuntimeError("Web API not initialized")
        await self.web_api.set_screentime_daily_allowance(
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
        """Set screen time allowed intervals."""
        if self.web_api is None:
            raise RuntimeError("Web API not initialized")
        await self.web_api.set_screentime_intervals_from_range(
            child_id, day_of_week, start_hour, start_minute, end_hour, end_minute
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
        self,
        child_id: str,
        app_id: str,
        display_name: str,
        platform: str,
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
    # Data fetching (web API enrichment)
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
            screentime = await self.web_api.get_screentime_policy(account_id)
            _LOGGER.debug("Raw screentime policy response for %s: %s", account_id, screentime)
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

        # Determine blocked platforms
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
                    raise ConfigEntryAuthFailed(ERROR_TOKEN_EXPIRED) from err
                raise UpdateFailed(f"Authentication failed: {err}") from err
            raise UpdateFailed(f"Error communicating with API: {err}") from err
        except Exception as err:
            _LOGGER.exception("Unexpected error fetching data: %s", err)
            raise UpdateFailed(f"Unexpected error: {err}") from err

    async def async_cleanup(self) -> None:
        """Clean up resources."""
        self._accounts.clear()
        self._devices.clear()
        if self.web_api:
            await self.web_api.close()
            self.web_api = None
        self.api = None
