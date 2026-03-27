"""Config flow for Microsoft Family Safety integration."""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import unquote

from pyfamilysafety.authenticator import Authenticator
from pyfamilysafety.exceptions import HttpException
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
import homeassistant.helpers.config_validation as cv
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from .const import (
    AVAILABLE_PLATFORMS,
    CONF_AUTH_URL,
    CONF_PLATFORMS,
    CONF_REDIRECT_URL,
    CONF_REFRESH_TOKEN,
    CONF_UPDATE_INTERVAL,
    DEFAULT_PLATFORMS,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    ERROR_AUTH_FAILED,
    INTEGRATION_NAME,
    MS_AUTH_PARAMS,
    MS_LOGIN_URL,
)

_LOGGER = logging.getLogger(__name__)


def _build_auth_url() -> str:
    """Build the Microsoft authentication URL.

    Returns:
        Complete authentication URL with all required parameters
    """
    params = "&".join([f"{k}={v}" for k, v in MS_AUTH_PARAMS.items()])
    return f"{MS_LOGIN_URL}?{params}"


async def validate_redirect_url(hass: HomeAssistant, redirect_url: str) -> dict[str, Any]:
    """Validate the redirect URL by attempting to authenticate."""
    try:
        _LOGGER.debug("Config flow received - testing credentials")

        # URL-decode the redirect URL to handle encoded characters (e.g. %24 -> $)
        # Microsoft's OAuth redirect often contains URL-encoded special characters
        redirect_url = unquote(redirect_url)

        authenticator = await Authenticator.create(
            token=redirect_url,
            use_refresh_token=False
        )

        _LOGGER.debug(
            "Authentication success, expiry time %s, returning refresh_token.",
            authenticator.expires
        )

        return {
            "title": INTEGRATION_NAME,
            "refresh_token": authenticator.refresh_token,
        }

    except HttpException as err:
        _LOGGER.error("HTTP error during authentication: %s", err)
        raise InvalidAuth from err
    except Exception as err:
        _LOGGER.error("Unexpected error during authentication: %s", err)
        raise InvalidAuth(f"Cannot connect: {err}") from err


class FamilySafetyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Microsoft Family Safety."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> FamilySafetyOptionsFlow:
        """Get the options flow for this handler."""
        return FamilySafetyOptionsFlow(config_entry)

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._redirect_url: str | None = None
        self._detected_source: str | None = None
        self._detected_url: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step - detect auth addon and show instructions."""
        from .auth.addon_client import AddonCookieClient

        # Detect available auth source (addon API or encrypted file)
        addon_client = AddonCookieClient(self.hass)
        source_type, detected_url = await addon_client.detect_auth_source()

        self._detected_source = source_type
        self._detected_url = detected_url

        _LOGGER.debug("Detected auth source: %s, URL: %s", source_type, detected_url)

        if user_input is not None:
            return await self.async_step_auth()

        description_placeholders = {
            "auth_url": _build_auth_url(),
        }

        # Add addon status info to placeholders
        if source_type == "api":
            description_placeholders["addon_status"] = f"Family Safety Auth addon detected at {detected_url}"
        elif source_type == "file":
            description_placeholders["addon_status"] = "Cookies found in shared storage"
        else:
            description_placeholders["addon_status"] = "No addon detected - web API features (screen time reads) will be unavailable until configured"

        return self.async_show_form(
            step_id="user",
            description_placeholders=description_placeholders,
            errors={},
        )

    async def async_step_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle redirect URL entry step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            redirect_url = user_input.get(CONF_REDIRECT_URL, "").strip()
            update_interval = user_input.get(
                CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
            )
            platforms = user_input.get(CONF_PLATFORMS, DEFAULT_PLATFORMS)
            auth_url = user_input.get(CONF_AUTH_URL, "").strip() or None

            # If user provided a custom auth URL, validate it
            if auth_url:
                from .auth.addon_client import AddonCookieClient

                addon_client = AddonCookieClient(self.hass, auth_url=auth_url)
                if not await addon_client._check_url_available(auth_url):
                    _LOGGER.warning(
                        "Custom auth URL %s is not reachable, saving anyway", auth_url
                    )

            if not redirect_url:
                errors["base"] = "no_redirect_url"
            else:
                try:
                    info = await validate_redirect_url(self.hass, redirect_url)

                    refresh_token = info["refresh_token"]
                    await self.async_set_unique_id(refresh_token[:20])
                    self._abort_if_unique_id_configured()

                    # Build data dict with auth_url if we have one
                    data = {
                        CONF_REFRESH_TOKEN: refresh_token,
                    }
                    # Store auth URL: user-provided > auto-detected
                    effective_auth_url = auth_url or self._detected_url
                    if effective_auth_url:
                        data[CONF_AUTH_URL] = effective_auth_url

                    return self.async_create_entry(
                        title=info["title"],
                        data=data,
                        options={
                            CONF_UPDATE_INTERVAL: update_interval,
                            CONF_PLATFORMS: platforms,
                        },
                    )

                except InvalidAuth:
                    errors["base"] = ERROR_AUTH_FAILED
                except Exception:
                    _LOGGER.exception("Unexpected exception during authentication")
                    errors["base"] = "unknown"

        description_placeholders = {
            "auth_url": _build_auth_url(),
        }

        # Build schema - include auth URL field if no addon was auto-detected
        schema_fields = {
            vol.Required(CONF_REDIRECT_URL): str,
            vol.Optional(
                CONF_UPDATE_INTERVAL,
                default=DEFAULT_UPDATE_INTERVAL,
            ): vol.All(vol.Coerce(int), vol.Range(min=30, max=3600)),
            vol.Optional(
                CONF_PLATFORMS,
                default=DEFAULT_PLATFORMS,
            ): cv.multi_select(
                {p: p for p in AVAILABLE_PLATFORMS}
            ),
        }

        # Only show auth URL field if addon was NOT auto-detected
        if self._detected_source == "none":
            schema_fields[vol.Optional(CONF_AUTH_URL, default="")] = str

        return self.async_show_form(
            step_id="auth",
            data_schema=vol.Schema(schema_fields),
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
                    info = await validate_redirect_url(self.hass, redirect_url)

                    entry = self.hass.config_entries.async_get_entry(
                        self.context["entry_id"]
                    )
                    if entry:
                        # Preserve existing auth_url when reauthing
                        new_data = {
                            CONF_REFRESH_TOKEN: info["refresh_token"],
                        }
                        if CONF_AUTH_URL in entry.data:
                            new_data[CONF_AUTH_URL] = entry.data[CONF_AUTH_URL]

                        self.hass.config_entries.async_update_entry(
                            entry,
                            data=new_data,
                        )
                        await self.hass.config_entries.async_reload(entry.entry_id)
                        return self.async_abort(reason="reauth_successful")

                except InvalidAuth:
                    errors["base"] = ERROR_AUTH_FAILED
                except Exception:
                    _LOGGER.exception("Unexpected exception during reauth")
                    errors["base"] = "unknown"

        description_placeholders = {
            "auth_url": _build_auth_url(),
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


class FamilySafetyOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Microsoft Family Safety."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_interval = self._config_entry.options.get(
            CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
        )
        current_platforms = self._config_entry.options.get(
            CONF_PLATFORMS, DEFAULT_PLATFORMS
        )
        current_auth_url = self._config_entry.data.get(CONF_AUTH_URL, "")

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_UPDATE_INTERVAL,
                        default=current_interval,
                    ): vol.All(vol.Coerce(int), vol.Range(min=30, max=3600)),
                    vol.Optional(
                        CONF_PLATFORMS,
                        default=current_platforms,
                    ): cv.multi_select(
                        {p: p for p in AVAILABLE_PLATFORMS}
                    ),
                    vol.Optional(
                        CONF_AUTH_URL,
                        default=current_auth_url,
                    ): str,
                }
            ),
        )


class InvalidAuth(HomeAssistantError):
    """Error to indicate authentication failure."""
