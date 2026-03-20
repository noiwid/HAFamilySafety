"""The Microsoft Family Safety integration."""
from __future__ import annotations

from datetime import datetime, timedelta
import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_APP_ID,
    ATTR_EXTENSION_TIME,
    ATTR_PLATFORM,
    ATTR_REQUEST_ID,
    ATTR_USER_ID,
    DOMAIN,
    PLATFORMS,
    SERVICE_APPROVE_REQUEST,
    SERVICE_BLOCK_APP,
    SERVICE_DENY_REQUEST,
    SERVICE_LOCK_PLATFORM,
    SERVICE_UNBLOCK_APP,
    SERVICE_UNLOCK_PLATFORM,
)
from .coordinator import FamilySafetyDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

SERVICE_BLOCK_APP_SCHEMA = vol.Schema({
    vol.Required("account_id"): cv.string,
    vol.Required("app_id"): cv.string,
})

SERVICE_LOCK_PLATFORM_SCHEMA = vol.Schema({
    vol.Required("account_id"): cv.string,
    vol.Required("platform"): vol.In(["Windows", "Xbox", "Mobile"]),
    vol.Optional("duration_hours", default=24): vol.All(
        vol.Coerce(int), vol.Range(min=1, max=168)
    ),
})

SERVICE_UNLOCK_PLATFORM_SCHEMA = vol.Schema({
    vol.Required("account_id"): cv.string,
    vol.Required("platform"): vol.In(["Windows", "Xbox", "Mobile"]),
})

SERVICE_APPROVE_REQUEST_SCHEMA = vol.Schema({
    vol.Required("request_id"): cv.string,
    vol.Optional("extension_minutes", default=60): vol.All(
        vol.Coerce(int), vol.Range(min=15, max=480)
    ),
})

SERVICE_DENY_REQUEST_SCHEMA = vol.Schema({
    vol.Required("request_id"): cv.string,
})


def _get_coordinator(hass: HomeAssistant) -> FamilySafetyDataUpdateCoordinator | None:
    """Get the first available coordinator."""
    if DOMAIN not in hass.data:
        return None
    for coordinator in hass.data[DOMAIN].values():
        if isinstance(coordinator, FamilySafetyDataUpdateCoordinator):
            return coordinator
    return None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Microsoft Family Safety from a config entry."""
    coordinator = FamilySafetyDataUpdateCoordinator(hass, entry)

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        _LOGGER.error("Failed to initialize Microsoft Family Safety: %s", err)
        raise ConfigEntryNotReady from err

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services (only once)
    if not hass.services.has_service(DOMAIN, SERVICE_BLOCK_APP):
        _register_services(hass)

    return True


def _register_services(hass: HomeAssistant) -> None:
    """Register integration services."""

    async def handle_block_app(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass)
        if coordinator is None:
            _LOGGER.error("No Family Safety coordinator available")
            return
        await coordinator.async_block_app(
            call.data["account_id"], call.data["app_id"]
        )

    async def handle_unblock_app(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass)
        if coordinator is None:
            _LOGGER.error("No Family Safety coordinator available")
            return
        await coordinator.async_unblock_app(
            call.data["account_id"], call.data["app_id"]
        )

    async def handle_lock_platform(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass)
        if coordinator is None:
            _LOGGER.error("No Family Safety coordinator available")
            return
        duration_hours = call.data.get("duration_hours", 24)
        valid_until = datetime.now() + timedelta(hours=duration_hours)
        await coordinator.async_lock_platform(
            call.data["account_id"], call.data["platform"], valid_until
        )

    async def handle_unlock_platform(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass)
        if coordinator is None:
            _LOGGER.error("No Family Safety coordinator available")
            return
        await coordinator.async_unlock_platform(
            call.data["account_id"], call.data["platform"]
        )

    async def handle_approve_request(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass)
        if coordinator is None:
            _LOGGER.error("No Family Safety coordinator available")
            return
        extension_seconds = call.data.get("extension_minutes", 60) * 60
        await coordinator.async_approve_request(
            call.data["request_id"], extension_seconds
        )

    async def handle_deny_request(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass)
        if coordinator is None:
            _LOGGER.error("No Family Safety coordinator available")
            return
        await coordinator.async_deny_request(call.data["request_id"])

    hass.services.async_register(
        DOMAIN, SERVICE_BLOCK_APP, handle_block_app, schema=SERVICE_BLOCK_APP_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UNBLOCK_APP, handle_unblock_app, schema=SERVICE_BLOCK_APP_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_LOCK_PLATFORM, handle_lock_platform, schema=SERVICE_LOCK_PLATFORM_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UNLOCK_PLATFORM, handle_unlock_platform, schema=SERVICE_UNLOCK_PLATFORM_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_APPROVE_REQUEST, handle_approve_request, schema=SERVICE_APPROVE_REQUEST_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DENY_REQUEST, handle_deny_request, schema=SERVICE_DENY_REQUEST_SCHEMA
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_cleanup()

        # Unregister services if no more entries
        if not hass.data[DOMAIN]:
            for service in [
                SERVICE_BLOCK_APP, SERVICE_UNBLOCK_APP,
                SERVICE_LOCK_PLATFORM, SERVICE_UNLOCK_PLATFORM,
                SERVICE_APPROVE_REQUEST, SERVICE_DENY_REQUEST,
            ]:
                hass.services.async_remove(DOMAIN, service)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
