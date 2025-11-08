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
    CONF_TOKEN,
    DOMAIN,
    ERROR_AUTH_FAILED,
    INTEGRATION_NAME,
    MS_AUTH_PARAMS,
    MS_LOGIN_URL,
)

_LOGGER = logging.getLogger(__name__)


async def validate_token(hass: HomeAssistant, token: str) -> dict[str, Any]:
    """Validate the token by attempting to authenticate."""
    try:
        # Try to initialize authenticator
        authenticator = Authenticator(token=token)

        # Try to initialize Family Safety API
        api = FamilySafety(auth=authenticator)

        # Try to update/fetch data to validate token
        await api.update()

        # If we got accounts, token is valid
        if not api.accounts:
            raise InvalidAuth("No accounts found - token may be invalid")

        # Return info about the first account for naming
        first_account = api.accounts[0]
        return {
            "title": f"{INTEGRATION_NAME} - {first_account.first_name}",
            "accounts": len(api.accounts),
        }

    except Exception as err:
        _LOGGER.error("Token validation failed: %s", err)
        raise InvalidAuth from err


class FamilySafetyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Microsoft Family Safety."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._token: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step - show authentication instructions."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # User clicked "Next" to continue to token entry
            return await self.async_step_token()

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

    async def async_step_token(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle token entry step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            token = user_input.get(CONF_TOKEN, "").strip()

            if not token:
                errors["base"] = "no_token"
            else:
                try:
                    # Validate the token
                    info = await validate_token(self.hass, token)

                    # Check if already configured
                    await self.async_set_unique_id(token[:20])
                    self._abort_if_unique_id_configured()

                    # Create the config entry
                    return self.async_create_entry(
                        title=info["title"],
                        data={
                            CONF_TOKEN: token,
                        },
                    )

                except InvalidAuth:
                    errors["base"] = ERROR_AUTH_FAILED
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.exception("Unexpected exception during token validation")
                    errors["base"] = "unknown"

        # Build the authentication URL for instructions
        auth_url = f"{MS_LOGIN_URL}?"
        auth_url += "&".join([f"{k}={v}" for k, v in MS_AUTH_PARAMS.items()])

        description_placeholders = {
            "auth_url": auth_url,
        }

        return self.async_show_form(
            step_id="token",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TOKEN): str,
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
            token = user_input.get(CONF_TOKEN, "").strip()

            if not token:
                errors["base"] = "no_token"
            else:
                try:
                    # Validate the new token
                    await validate_token(self.hass, token)

                    # Update the entry
                    entry = self.hass.config_entries.async_get_entry(
                        self.context["entry_id"]
                    )
                    if entry:
                        self.hass.config_entries.async_update_entry(
                            entry,
                            data={
                                **entry.data,
                                CONF_TOKEN: token,
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
                    vol.Required(CONF_TOKEN): str,
                }
            ),
            description_placeholders=description_placeholders,
            errors=errors,
        )


class InvalidAuth(HomeAssistantError):
    """Error to indicate authentication failure."""
