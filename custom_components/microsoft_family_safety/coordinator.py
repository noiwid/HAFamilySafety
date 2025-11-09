"""DataUpdateCoordinator for Microsoft Family Safety."""
from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from pyfamilysafety import FamilySafety
from pyfamilysafety.account import Account
from pyfamilysafety.authenticator import Authenticator
from pyfamilysafety.device import Device
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
            # Initialize authenticator with refresh token
            authenticator = await Authenticator.create(
                token=refresh_token,
                use_refresh_token=True
            )

            # Initialize Family Safety API
            self.api = FamilySafety(authenticator)

            # Enable experimental mode to get more data
            self.api.experimental = True
            _LOGGER.debug("Family Safety API client initialized successfully with experimental mode")
        except Exception as err:
            _LOGGER.error("Failed to initialize Family Safety API: %s", err)
            raise ConfigEntryAuthFailed(ERROR_AUTH_FAILED) from err

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Family Safety API."""
        if self.api is None:
            await self._async_setup_api()

        try:
            # Update all accounts
            await self.api.update()

            # Get all accounts
            accounts_data = {}
            devices_data = {}

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
                        "app_id": app.application_id,
                        "app_name": app.application_name,
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

    async def async_block_device(self, device_id: str, duration: int | None = None) -> None:
        """Block a device."""
        if device_id not in self._devices:
            raise ValueError(f"Device {device_id} not found")

        device = self._devices[device_id]
        account = self._get_account_for_device(device_id)

        if account is None:
            raise ValueError(f"Account not found for device {device_id}")

        try:
            # Override device to block it
            # valid_until can be None for indefinite block
            await account.override_device(
                target=device.device_id,
                override=True,
                valid_until=duration
            )

            # Refresh data after action
            await self.async_request_refresh()

        except Exception as err:
            _LOGGER.error("Failed to block device %s: %s", device_id, err)
            raise

    async def async_unblock_device(self, device_id: str) -> None:
        """Unblock a device."""
        if device_id not in self._devices:
            raise ValueError(f"Device {device_id} not found")

        device = self._devices[device_id]
        account = self._get_account_for_device(device_id)

        if account is None:
            raise ValueError(f"Account not found for device {device_id}")

        try:
            # Override device to unblock it
            await account.override_device(
                target=device.device_id,
                override=False,
                valid_until=None
            )

            # Refresh data after action
            await self.async_request_refresh()

        except Exception as err:
            _LOGGER.error("Failed to unblock device %s: %s", device_id, err)
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

    async def async_cleanup(self) -> None:
        """Clean up resources."""
        self._accounts.clear()
        self._devices.clear()
        self.api = None
