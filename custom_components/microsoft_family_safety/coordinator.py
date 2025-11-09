"""DataUpdateCoordinator for Microsoft Family Safety."""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from pyfamilysafety import FamilySafety
from pyfamilysafety.account import Account
from pyfamilysafety.device import Device

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

                # Convert screentime from milliseconds to seconds
                today_screentime_ms = account.today_screentime_usage or 0
                today_screentime_seconds = int(today_screentime_ms / 1000) if today_screentime_ms else 0

                average_screentime_ms = account.average_screentime_usage or 0
                average_screentime_seconds = int(average_screentime_ms / 1000) if average_screentime_ms else 0

                accounts_data[account_id] = {
                    "user_id": account.user_id,
                    "first_name": account.first_name,
                    "surname": account.surname,
                    "profile_picture": account.profile_picture,
                    "today_screentime_usage": today_screentime_seconds,
                    "average_screentime_usage": average_screentime_seconds,
                    "account_balance": account.account_balance,
                    "account_currency": account.account_currency,
                    "blocked_platforms": account.blocked_platforms,
                    "devices": [],
                    "applications": [],
                }

                # Store account reference
                self._accounts[account_id] = account

                # Process devices for this account
                for device in account.devices:
                    device_id = device.device_id
                    # Convert milliseconds to seconds (keep as seconds for compatibility)
                    time_used_ms = device.today_time_used or 0
                    time_used_seconds = int(time_used_ms / 1000) if time_used_ms else 0

                    device_data = {
                        "device_id": device.device_id,
                        "device_name": device.device_name,
                        "device_class": device.device_class,
                        "device_make": device.device_make,
                        "device_model": device.device_model,
                        "os_name": device.os_name,
                        "today_time_used": time_used_seconds,  # Store as seconds
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

    async def async_cleanup(self) -> None:
        """Clean up resources."""
        self._accounts.clear()
        self._devices.clear()
        self.api = None
