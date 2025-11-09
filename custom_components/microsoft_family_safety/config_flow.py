"""Config flow for Microsoft Family Safety integration."""
from __future__ import annotations

import logging
from typing import Any

from pyfamilysafety import FamilySafety
from pyfamilysafety.authenticator import Authenticator
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from .const import (
    CONF_REDIRECT_URL,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    ERROR_AUTH_FAILED,
    INTEGRATION_NAME,
    MS_AUTH_PARAMS,
    MS_LOGIN_URL,
)

_LOGGER = logging.getLogger(__name__)


async def validate_redirect_url(hass: HomeAssistant, redirect_url: str) -> dict[str, Any]:
    """Validate the redirect URL by attempting to authenticate."""
    try:
        _LOGGER.debug("Starting authentication with redirect URL: %s", redirect_url)

        # Create authenticator from redirect URL
        _LOGGER.debug("Creating authenticator...")
        authenticator = await Authenticator.create(
            token=redirect_url,
            use_refresh_token=False
        )
        _LOGGER.debug("Authenticator created successfully, refresh_token: %s",
                     authenticator.refresh_token[:20] if authenticator.refresh_token else None)

        # Try to initialize Family Safety API
        _LOGGER.debug("Initializing Family Safety API...")
        api = FamilySafety(authenticator)

        # DO NOT enable experimental mode - it causes issues with current pyfamilysafety version
        # api.experimental = True
        _LOGGER.debug("Family Safety API initialized (experimental mode disabled)")

        # Ensure accounts is initialized as a list (workaround for potential None return)
        if not hasattr(api, 'accounts') or api.accounts is None:
            _LOGGER.warning("api.accounts is not properly initialized, setting to empty list")
            api.accounts = []

        # Try to update/fetch data to validate authentication
        _LOGGER.debug("Fetching Family Safety data...")
        _LOGGER.debug("Before update - api.accounts: %s", api.accounts)

        try:
            # Call update to fetch accounts
            await api.update()
            _LOGGER.debug("Update completed successfully")

            # Check again if accounts became None after update
            if api.accounts is None:
                _LOGGER.error("api.accounts became None after update - this is a pyfamilysafety bug")
                api.accounts = []

        except TypeError as type_err:
            _LOGGER.error("TypeError during update: %s", str(type_err))
            if "'NoneType' object is not iterable" in str(type_err):
                _LOGGER.error("API returned None for accounts - this indicates Account.from_dict() returned None")
                # Force accounts to empty list to prevent crash
                api.accounts = []
                _LOGGER.info("Continuing with empty accounts list...")
            else:
                raise
        except Exception as err:
            _LOGGER.error("Unexpected error during update: %s", str(err))
            raise

        _LOGGER.debug("After update - api.accounts: %s (type: %s)", api.accounts, type(api.accounts))

        # Check if accounts is None or empty
        if api.accounts is None:
            _LOGGER.error("api.accounts is None after update() - should have been fixed to []")
            api.accounts = []

        if len(api.accounts) == 0:
            _LOGGER.error("api.accounts is empty after update() - no Family Safety child accounts found")
            _LOGGER.error(
                "This could mean: "
                "1) You are not the family organizer, "
                "2) No child accounts are configured in Family Safety, "
                "3) There is an API incompatibility issue"
            )
            raise InvalidAuth(
                "No Family Safety child accounts found. "
                "Please verify: "
                "1) You are logged in as the family organizer "
                "2) You have child accounts configured at https://account.microsoft.com/family "
                "3) Family Safety is enabled for at least one child"
            )

        _LOGGER.debug("Data fetched, found %d accounts", len(api.accounts))

        # If we got accounts, authentication is valid
        if not api.accounts:
            _LOGGER.error("No accounts found after successful authentication")
            raise InvalidAuth("No accounts found - authentication may be invalid")

        # Return info about the first account for naming and the refresh token
        first_account = api.accounts[0]
        _LOGGER.info("Authentication successful for user: %s", first_account.first_name)
        return {
            "title": f"{INTEGRATION_NAME} - {first_account.first_name}",
            "accounts": len(api.accounts),
            "refresh_token": authenticator.refresh_token,
        }

    except InvalidAuth:
        raise
    except Exception as err:
        _LOGGER.exception("Authentication failed with exception: %s", err)
        raise InvalidAuth(f"Authentication error: {str(err)}") from err


class FamilySafetyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Microsoft Family Safety."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._redirect_url: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step - show authentication instructions."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # User clicked "Next" to continue to redirect URL entry
            return await self.async_step_auth()

        # Build the authentication URL
        auth_url = f"{MS_LOGIN_URL}?"
        auth_url += "&".join([f"{k}={v}" for k, v in MS_AUTH_PARAMS.items()])

        description_placeholders = {
            "auth_url": auth_url,
        }

        return self.async_show_form(
            step_id="user",
            description_placeholders=description_placeholders,
            errors=errors,
        )

    async def async_step_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle redirect URL entry step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            redirect_url = user_input.get(CONF_REDIRECT_URL, "").strip()

            if not redirect_url:
                errors["base"] = "no_redirect_url"
            else:
                try:
                    # Validate the redirect URL
                    info = await validate_redirect_url(self.hass, redirect_url)

                    # Check if already configured
                    refresh_token = info["refresh_token"]
                    await self.async_set_unique_id(refresh_token[:20])
                    self._abort_if_unique_id_configured()

                    # Create the config entry
                    return self.async_create_entry(
                        title=info["title"],
                        data={
                            CONF_REFRESH_TOKEN: refresh_token,
                        },
                    )

                except InvalidAuth:
                    errors["base"] = ERROR_AUTH_FAILED
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.exception("Unexpected exception during authentication")
                    errors["base"] = "unknown"

        # Build the authentication URL for instructions
        auth_url = f"{MS_LOGIN_URL}?"
        auth_url += "&".join([f"{k}={v}" for k, v in MS_AUTH_PARAMS.items()])

        description_placeholders = {
            "auth_url": auth_url,
        }

        return self.async_show_form(
            step_id="auth",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_REDIRECT_URL): str,
                }
            ),
            description_placeholders=description_placeholders,
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Handle reauth if token expires."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle reauth confirmation."""
        errors: dict[str, str] = {}

        if user_input is not None:
            redirect_url = user_input.get(CONF_REDIRECT_URL, "").strip()

            if not redirect_url:
                errors["base"] = "no_redirect_url"
            else:
                try:
                    # Validate the new redirect URL
                    info = await validate_redirect_url(self.hass, redirect_url)

                    # Update the entry
                    entry = self.hass.config_entries.async_get_entry(
                        self.context["entry_id"]
                    )
                    if entry:
                        self.hass.config_entries.async_update_entry(
                            entry,
                            data={
                                CONF_REFRESH_TOKEN: info["refresh_token"],
                            },
                        )
                        await self.hass.config_entries.async_reload(entry.entry_id)
                        return self.async_abort(reason="reauth_successful")

                except InvalidAuth:
                    errors["base"] = ERROR_AUTH_FAILED
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.exception("Unexpected exception during reauth")
                    errors["base"] = "unknown"

        # Build the authentication URL for instructions
        auth_url = f"{MS_LOGIN_URL}?"
        auth_url += "&".join([f"{k}={v}" for k, v in MS_AUTH_PARAMS.items()])

        description_placeholders = {
            "auth_url": auth_url,
        }

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_REDIRECT_URL): str,
                }
            ),
            description_placeholders=description_placeholders,
            errors=errors,
        )


class InvalidAuth(HomeAssistantError):
    """Error to indicate authentication failure."""
