"""The Microsoft Family Safety integration."""
from __future__ import annotations

import logging

import voluptuous as vol
from pyfamilysafety.enum import OverrideTarget

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from .const import (
    DOMAIN,
    PLATFORMS,
    SERVICE_APPROVE_REQUEST,
    SERVICE_BLOCK_DEVICE,
    SERVICE_DENY_REQUEST,
    SERVICE_UNBLOCK_DEVICE,
)
from .coordinator import FamilySafetyDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# Service schemas
SERVICE_BLOCK_DEVICE_SCHEMA = vol.Schema(
    {
        vol.Required("account_id"): cv.string,
        vol.Required("platform"): vol.In(["windows", "mobile", "xbox"]),
        vol.Optional("duration_minutes"): cv.positive_int,
    }
)

SERVICE_UNBLOCK_DEVICE_SCHEMA = vol.Schema(
    {
        vol.Required("account_id"): cv.string,
        vol.Required("platform"): vol.In(["windows", "mobile", "xbox"]),
    }
)

SERVICE_APPROVE_REQUEST_SCHEMA = vol.Schema(
    {
        vol.Required("request_id"): cv.string,
        vol.Required("extension_time"): cv.positive_int,
    }
)

SERVICE_DENY_REQUEST_SCHEMA = vol.Schema(
    {
        vol.Required("request_id"): cv.string,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Microsoft Family Safety from a config entry."""
    # Create coordinator
    coordinator = FamilySafetyDataUpdateCoordinator(hass, entry)

    # Fetch initial data
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        _LOGGER.error("Failed to initialize Microsoft Family Safety: %s", err)
        raise ConfigEntryNotReady from err

    # Store coordinator
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Forward entry setup to platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services
    await async_setup_services(hass)

    return True


async def async_setup_services(hass: HomeAssistant) -> None:
    """Set up services for Microsoft Family Safety."""

    async def handle_block_device(call: ServiceCall) -> None:
        """Handle block platform service call."""
        account_id = call.data["account_id"]
        platform_str = call.data["platform"].lower()
        duration_minutes = call.data.get("duration_minutes")

        # Map platform string to OverrideTarget enum
        platform_map = {
            "windows": OverrideTarget.WINDOWS,
            "mobile": OverrideTarget.MOBILE,
            "xbox": OverrideTarget.XBOX,
        }
        platform = platform_map.get(platform_str)

        if not platform:
            _LOGGER.error("Invalid platform: %s", platform_str)
            return

        # Find coordinator that has this account
        for entry_id, coordinator in hass.data[DOMAIN].items():
            if isinstance(coordinator, FamilySafetyDataUpdateCoordinator):
                if coordinator.data and account_id in coordinator.data.get("accounts", {}):
                    await coordinator.async_block_platform(account_id, platform, duration_minutes)
                    return

        _LOGGER.error("Account %s not found in any Family Safety integration", account_id)

    async def handle_unblock_device(call: ServiceCall) -> None:
        """Handle unblock platform service call."""
        account_id = call.data["account_id"]
        platform_str = call.data["platform"].lower()

        # Map platform string to OverrideTarget enum
        platform_map = {
            "windows": OverrideTarget.WINDOWS,
            "mobile": OverrideTarget.MOBILE,
            "xbox": OverrideTarget.XBOX,
        }
        platform = platform_map.get(platform_str)

        if not platform:
            _LOGGER.error("Invalid platform: %s", platform_str)
            return

        # Find coordinator that has this account
        for entry_id, coordinator in hass.data[DOMAIN].items():
            if isinstance(coordinator, FamilySafetyDataUpdateCoordinator):
                if coordinator.data and account_id in coordinator.data.get("accounts", {}):
                    await coordinator.async_unblock_platform(account_id, platform)
                    return

        _LOGGER.error("Account %s not found in any Family Safety integration", account_id)

    async def handle_approve_request(call: ServiceCall) -> None:
        """Handle approve request service call."""
        request_id = call.data["request_id"]
        extension_time = call.data["extension_time"]

        # Use first coordinator (requests are global)
        coordinators = [
            c for c in hass.data[DOMAIN].values()
            if isinstance(c, FamilySafetyDataUpdateCoordinator)
        ]

        if coordinators:
            await coordinators[0].async_approve_request(request_id, extension_time)
        else:
            _LOGGER.error("No Family Safety coordinator found")

    async def handle_deny_request(call: ServiceCall) -> None:
        """Handle deny request service call."""
        request_id = call.data["request_id"]

        # Use first coordinator (requests are global)
        coordinators = [
            c for c in hass.data[DOMAIN].values()
            if isinstance(c, FamilySafetyDataUpdateCoordinator)
        ]

        if coordinators:
            await coordinators[0].async_deny_request(request_id)
        else:
            _LOGGER.error("No Family Safety coordinator found")

    # Register services only once
    if not hass.services.has_service(DOMAIN, SERVICE_BLOCK_DEVICE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_BLOCK_DEVICE,
            handle_block_device,
            schema=SERVICE_BLOCK_DEVICE_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_UNBLOCK_DEVICE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_UNBLOCK_DEVICE,
            handle_unblock_device,
            schema=SERVICE_UNBLOCK_DEVICE_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_APPROVE_REQUEST):
        hass.services.async_register(
            DOMAIN,
            SERVICE_APPROVE_REQUEST,
            handle_approve_request,
            schema=SERVICE_APPROVE_REQUEST_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_DENY_REQUEST):
        hass.services.async_register(
            DOMAIN,
            SERVICE_DENY_REQUEST,
            handle_deny_request,
            schema=SERVICE_DENY_REQUEST_SCHEMA,
        )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        # Clean up coordinator
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_cleanup()

        # Remove services if this was the last entry
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_BLOCK_DEVICE)
            hass.services.async_remove(DOMAIN, SERVICE_UNBLOCK_DEVICE)
            hass.services.async_remove(DOMAIN, SERVICE_APPROVE_REQUEST)
            hass.services.async_remove(DOMAIN, SERVICE_DENY_REQUEST)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
