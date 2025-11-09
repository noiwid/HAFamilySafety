"""DataUpdateCoordinator for Microsoft Family Safety."""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from pyfamilysafety import FamilySafety
from pyfamilysafety.account import Account
from pyfamilysafety.device import Device
from pyfamilysafety.enum import OverrideTarget, OverrideType
from pyfamilysafety.exceptions import HttpException

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_REFRESH_TOKEN,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    ERROR_AUTH_FAILED,
    ERROR_TOKEN_EXPIRED,
)

_LOGGER = logging.getLogger(__name__)


class FamilySafetyDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Class to manage fetching Microsoft Family Safety data."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_UPDATE_INTERVAL),
        )
        self.entry = entry
        self.api: FamilySafety | None = None
        self._accounts: dict[str, Account] = {}
        self._devices: dict[str, Device] = {}
        self._is_retrying_auth = False

    async def _async_setup_api(self) -> None:
        """Set up the Family Safety API client."""
        refresh_token = self.entry.data[CONF_REFRESH_TOKEN]

        try:
            # Initialize Family Safety API using the create() method
            # This method (available in pyfamilysafety 1.1.2) automatically fetches accounts
            self.api = await FamilySafety.create(
                token=refresh_token,
                use_refresh_token=True,
                experimental=False
            )

            _LOGGER.debug("Family Safety API client initialized successfully")
        except Exception as err:
            _LOGGER.error("Failed to initialize Family Safety API: %s", err)
            raise ConfigEntryAuthFailed(ERROR_AUTH_FAILED) from err

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Family Safety API."""
        if self.api is None:
            await self._async_setup_api()

        try:
            # Update all accounts
            try:
                await self.api.update()
            except TypeError as type_err:
                # Handle pyfamilysafety bug where Account.from_dict returns None
                if "'NoneType' object is not iterable" in str(type_err):
                    _LOGGER.warning(
                        "pyfamilysafety returned None for accounts - "
                        "this may indicate no child accounts are configured or an API incompatibility"
                    )
                    if not hasattr(self.api, 'accounts') or self.api.accounts is None:
                        self.api.accounts = []
                else:
                    raise

            # Workaround for pyfamilysafety bug where accounts can be None
            if self.api.accounts is None:
                _LOGGER.warning("API accounts is None after update, initializing to empty list")
                self.api.accounts = []

            # Get all accounts
            accounts_data = {}
            devices_data = {}

            # Log account count
            _LOGGER.debug("Found %d Family Safety accounts", len(self.api.accounts))

            # Store accounts and their devices
            for account in self.api.accounts:
                account_id = account.user_id
                accounts_data[account_id] = {
                    "user_id": account.user_id,
                    "first_name": account.first_name,
                    "surname": account.surname,
                    "profile_picture": account.profile_picture,
                    "today_screentime_usage": account.today_screentime_usage,
                    "average_screentime_usage": account.average_screentime_usage,
                    "account_balance": account.account_balance,
                    "account_currency": account.account_currency,
                    "devices": [],
                    "applications": [],
                }

                # Store account reference
                self._accounts[account_id] = account

                # Process devices for this account
                for device in account.devices:
                    device_id = device.device_id
                    device_data = {
                        "device_id": device.device_id,
                        "device_name": device.device_name,
                        "device_class": device.device_class,
                        "device_make": device.device_make,
                        "device_model": device.device_model,
                        "os_name": device.os_name,
                        "today_time_used": device.today_time_used,
                        "last_seen": device.last_seen,
                        "blocked": device.blocked,
                        "account_id": account_id,
                    }
                    devices_data[device_id] = device_data
                    accounts_data[account_id]["devices"].append(device_id)

                    # Store device reference
                    self._devices[device_id] = device

                # Store applications
                for app in account.applications:
                    accounts_data[account_id]["applications"].append({
                        "app_id": app.app_id,
                        "app_name": app.name,
                        "blocked": app.blocked,
                    })

            return {
                "accounts": accounts_data,
                "devices": devices_data,
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

    async def async_block_platform(
        self,
        account_id: str,
        platform: OverrideTarget,
        duration_minutes: int | None = None
    ) -> None:
        """Block a platform (Windows/Xbox/Mobile) for an account."""
        if account_id not in self._accounts:
            raise ValueError(f"Account {account_id} not found")

        account = self._accounts[account_id]

        try:
            # Calculate valid_until datetime if duration is specified
            valid_until = None
            if duration_minutes:
                valid_until = datetime.now() + timedelta(minutes=duration_minutes)

            # Override platform to block it
            await account.override_device(
                target=platform,
                override=OverrideType.UNTIL,
                valid_until=valid_until
            )

            _LOGGER.info("Blocked platform %s for account %s", platform, account_id)

            # Refresh data after action
            await self.async_request_refresh()

        except Exception as err:
            _LOGGER.error("Failed to block platform %s: %s", platform, err)
            raise

    async def async_unblock_platform(self, account_id: str, platform: OverrideTarget) -> None:
        """Unblock a platform (Windows/Xbox/Mobile) for an account."""
        if account_id not in self._accounts:
            raise ValueError(f"Account {account_id} not found")

        account = self._accounts[account_id]

        try:
            # Cancel override to unblock the platform
            await account.override_device(
                target=platform,
                override=OverrideType.CANCEL,
                valid_until=datetime.now()
            )

            _LOGGER.info("Unblocked platform %s for account %s", platform, account_id)

            # Refresh data after action
            await self.async_request_refresh()

        except Exception as err:
            _LOGGER.error("Failed to unblock platform %s: %s", platform, err)
            raise

    async def async_approve_request(self, request_id: str, extension_time: int) -> None:
        """Approve a pending request with extension time."""
        if self.api is None:
            raise ValueError("API not initialized")

        try:
            await self.api.approve_pending_request(request_id, extension_time)
            await self.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Failed to approve request %s: %s", request_id, err)
            raise

    async def async_deny_request(self, request_id: str) -> None:
        """Deny a pending request."""
        if self.api is None:
            raise ValueError("API not initialized")

        try:
            await self.api.deny_pending_request(request_id)
            await self.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Failed to deny request %s: %s", request_id, err)
            raise

    def _get_account_for_device(self, device_id: str) -> Account | None:
        """Get the account that owns a device."""
        if not self.data or "devices" not in self.data:
            return None

        device_data = self.data["devices"].get(device_id)
        if not device_data:
            return None

        account_id = device_data.get("account_id")
        return self._accounts.get(account_id)

    def get_platforms_for_account(self, account_id: str) -> set[OverrideTarget]:
        """Get all platforms that have devices for an account."""
        if not self.data or "devices" not in self.data:
            return set()

        platforms = set()
        for device_data in self.data["devices"].values():
            if device_data.get("account_id") == account_id:
                platform = self._get_platform_from_os(device_data.get("os_name", ""))
                if platform:
                    platforms.add(platform)

        return platforms

    @staticmethod
    def _get_platform_from_os(os_name: str) -> OverrideTarget | None:
        """Map OS name to OverrideTarget platform."""
        os_lower = os_name.lower()
        if "windows" in os_lower:
            return OverrideTarget.WINDOWS
        elif "xbox" in os_lower:
            return OverrideTarget.XBOX
        elif "android" in os_lower or "ios" in os_lower:
            return OverrideTarget.MOBILE
        return None

    async def async_cleanup(self) -> None:
        """Clean up resources."""
        self._accounts.clear()
        self._devices.clear()
        self.api = None
