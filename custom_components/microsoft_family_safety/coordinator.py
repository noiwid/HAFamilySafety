"""DataUpdateCoordinator for Microsoft Family Safety."""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from pyfamilysafety import FamilySafety
from pyfamilysafety.account import Account
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


def _ms_to_minutes(milliseconds: int | None) -> int:
    """Convert milliseconds to minutes.

    Args:
        milliseconds: Duration in milliseconds, can be None

    Returns:
        Duration in minutes as integer, or 0 if input is None
    """
    if not milliseconds:
        return 0
    return int(milliseconds / 60000)


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
            self.api = await FamilySafety.create(
                token=refresh_token,
                use_refresh_token=True,
                experimental=False
            )

            _LOGGER.debug("Family Safety API client initialized successfully")
        except Exception as err:
            _LOGGER.error("Failed to initialize Family Safety API: %s", err)
            raise ConfigEntryAuthFailed(ERROR_AUTH_FAILED) from err

    def _transform_account_data(self, account: Account) -> tuple[str, dict[str, Any]]:
        """Transform an Account object to dictionary format.

        Args:
            account: The pyfamilysafety Account object

        Returns:
            Tuple of (account_id, account_data_dict)
        """
        account_id = account.user_id
        account_data = {
            "user_id": account.user_id,
            "first_name": account.first_name,
            "surname": account.surname,
            "profile_picture": account.profile_picture,
            "today_screentime_usage": _ms_to_minutes(account.today_screentime_usage),
            "average_screentime_usage": _ms_to_minutes(account.average_screentime_usage),
            "account_balance": account.account_balance,
            "account_currency": account.account_currency,
            "blocked_platforms": account.blocked_platforms,
            "devices": [],
            "applications": [
                {
                    "app_id": app.app_id,
                    "app_name": app.name,
                    "blocked": app.blocked,
                }
                for app in account.applications
            ],
        }
        return account_id, account_data

    def _transform_device_data(self, device: Device, account_id: str) -> tuple[str, dict[str, Any]]:
        """Transform a Device object to dictionary format.

        Args:
            device: The pyfamilysafety Device object
            account_id: The parent account ID

        Returns:
            Tuple of (device_id, device_data_dict)
        """
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

    async def _debug_fetch_schedules(self) -> None:
        """DEBUG: Try to fetch screen time schedules/limits from API.

        This is a temporary debug method to test if Microsoft exposes
        screen time limits via GET /v4/devicelimits/schedules/{USER_ID}

        Check Home Assistant logs for results (filter by 'DEBUG SCHEDULES:').
        """
        if not self.api or not hasattr(self.api, 'accounts'):
            return

        for account in self.api.accounts:
            user_id = account.user_id
            _LOGGER.warning(
                "DEBUG SCHEDULES: Testing for user %s (%s)",
                user_id, account.first_name
            )

            # Try to access the internal API client (it's 'api' not '_api')
            api_client = None
            if hasattr(self.api, 'api'):
                api_client = self.api.api
                _LOGGER.warning("DEBUG SCHEDULES: api attributes: %s", dir(api_client))

            # Try direct HTTP request if we can find the session
            session = None
            headers = {}

            if api_client:
                # Look for session in various possible locations
                for attr in ['_session', 'session', '_client', 'client']:
                    if hasattr(api_client, attr):
                        session = getattr(api_client, attr)
                        _LOGGER.warning("DEBUG SCHEDULES: Found session at api.%s", attr)
                        break

                # Look for headers
                for attr in ['_headers', 'headers']:
                    if hasattr(api_client, attr):
                        headers = getattr(api_client, attr)
                        _LOGGER.warning("DEBUG SCHEDULES: Found headers at api.%s", attr)
                        break

            if session:
                base_url = "https://family.microsoft.com/api"
                schedules_url = f"{base_url}/v4/devicelimits/schedules/{user_id}"

                _LOGGER.warning("DEBUG SCHEDULES: Trying GET %s", schedules_url)

                try:
                    async with session.get(schedules_url, headers=headers) as resp:
                        status = resp.status
                        text = await resp.text()
                        _LOGGER.warning(
                            "DEBUG SCHEDULES: Response status=%s, body=%s",
                            status, text[:2000] if text else "(empty)"
                        )
                except Exception as err:
                    _LOGGER.warning(
                        "DEBUG SCHEDULES: HTTP request failed: %s (%s)",
                        type(err).__name__, err
                    )
            else:
                _LOGGER.warning(
                    "DEBUG SCHEDULES: Cannot find HTTP session in API client"
                )

            # Only test once, not for each account
            break

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Family Safety API."""
        if self.api is None:
            await self._async_setup_api()

        try:
            await self.api.update()

            if not hasattr(self.api, 'accounts') or self.api.accounts is None:
                _LOGGER.warning("API accounts is None after update, initializing to empty list")
                self.api.accounts = []

            # DEBUG: Try to fetch screen time schedules/limits
            await self._debug_fetch_schedules()

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
