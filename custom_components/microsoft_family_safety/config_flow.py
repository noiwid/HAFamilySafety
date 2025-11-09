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

        # Try to update/fetch data to validate authentication
        _LOGGER.debug("Fetching Family Safety data...")
        await api.update()
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
