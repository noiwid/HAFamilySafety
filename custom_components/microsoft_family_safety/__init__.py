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
    ALL_SERVICES,
    DOMAIN,
    PLATFORMS,
    SERVICE_APPROVE_REQUEST,
    SERVICE_BLOCK_APP,
    SERVICE_BLOCK_WEBSITE,
    SERVICE_DENY_REQUEST,
    SERVICE_LOCK_PLATFORM,
    SERVICE_REMOVE_APP_TIME_LIMIT,
    SERVICE_REMOVE_WEBSITE,
    SERVICE_SET_ACQUISITION_POLICY,
    SERVICE_SET_AGE_RATING,
    SERVICE_SET_APP_TIME_LIMIT,
    SERVICE_SET_SCREENTIME_INTERVALS,
    SERVICE_SET_SCREENTIME_LIMIT,
    SERVICE_TOGGLE_WEB_FILTER,
    SERVICE_UNBLOCK_APP,
    SERVICE_UNLOCK_PLATFORM,
)
from .coordinator import FamilySafetyDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Service Schemas
# ──────────────────────────────────────────────────────────────────────

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

# New service schemas (web API)

SERVICE_SET_SCREENTIME_LIMIT_SCHEMA = vol.Schema({
    vol.Required("account_id"): cv.string,
    vol.Required("day_of_week"): vol.All(vol.Coerce(int), vol.Range(min=0, max=6)),
    vol.Required("hours"): vol.All(vol.Coerce(int), vol.Range(min=0, max=24)),
    vol.Optional("minutes", default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=59)),
})

SERVICE_SET_SCREENTIME_INTERVALS_SCHEMA = vol.Schema({
    vol.Required("account_id"): cv.string,
    vol.Required("day_of_week"): vol.All(vol.Coerce(int), vol.Range(min=0, max=6)),
    vol.Required("start_hour"): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
    vol.Optional("start_minute", default=0): vol.In([0, 30]),
    vol.Required("end_hour"): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
    vol.Optional("end_minute", default=0): vol.In([0, 30]),
})

SERVICE_SET_APP_TIME_LIMIT_SCHEMA = vol.Schema({
    vol.Required("account_id"): cv.string,
    vol.Required("app_id"): cv.string,
    vol.Required("app_name"): cv.string,
    vol.Optional("platform", default="windows"): vol.In(["windows", "xbox", "mobile"]),
    vol.Required("hours"): vol.All(vol.Coerce(int), vol.Range(min=0, max=24)),
    vol.Optional("minutes", default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=59)),
    vol.Optional("start_time", default="07:00:00"): cv.string,
    vol.Optional("end_time", default="22:00:00"): cv.string,
})

SERVICE_REMOVE_APP_TIME_LIMIT_SCHEMA = vol.Schema({
    vol.Required("account_id"): cv.string,
    vol.Required("app_id"): cv.string,
    vol.Required("app_name"): cv.string,
    vol.Optional("platform", default="windows"): vol.In(["windows", "xbox", "mobile"]),
})

SERVICE_BLOCK_WEBSITE_SCHEMA = vol.Schema({
    vol.Required("account_id"): cv.string,
    vol.Required("website"): cv.string,
})

SERVICE_REMOVE_WEBSITE_SCHEMA = vol.Schema({
    vol.Required("account_id"): cv.string,
    vol.Required("website"): cv.string,
})

SERVICE_TOGGLE_WEB_FILTER_SCHEMA = vol.Schema({
    vol.Required("account_id"): cv.string,
    vol.Required("enabled"): cv.boolean,
})

SERVICE_SET_AGE_RATING_SCHEMA = vol.Schema({
    vol.Required("account_id"): cv.string,
    vol.Required("age"): vol.All(vol.Coerce(int), vol.Range(min=3, max=21)),
})

SERVICE_SET_ACQUISITION_POLICY_SCHEMA = vol.Schema({
    vol.Required("account_id"): cv.string,
    vol.Required("require_approval"): cv.boolean,
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

    # ── Existing services (via pyfamilysafety) ───────────────────────────

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

    # ── New services (via web API) ───────────────────────────────────────

    async def handle_set_screentime_limit(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass)
        if coordinator is None:
            _LOGGER.error("No Family Safety coordinator available")
            return
        await coordinator.async_set_screentime_limit(
            call.data["account_id"],
            call.data["day_of_week"],
            call.data["hours"],
            call.data.get("minutes", 0),
        )

    async def handle_set_screentime_intervals(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass)
        if coordinator is None:
            _LOGGER.error("No Family Safety coordinator available")
            return
        await coordinator.async_set_screentime_intervals(
            call.data["account_id"],
            call.data["day_of_week"],
            call.data["start_hour"],
            call.data.get("start_minute", 0),
            call.data["end_hour"],
            call.data.get("end_minute", 0),
        )

    async def handle_set_app_time_limit(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass)
        if coordinator is None:
            _LOGGER.error("No Family Safety coordinator available")
            return
        hours = call.data["hours"]
        minutes = call.data.get("minutes", 0)
        allowance = f"{hours:02d}:{minutes:02d}:00"
        await coordinator.async_set_app_time_limit(
            call.data["account_id"],
            call.data["app_id"],
            call.data["app_name"],
            call.data.get("platform", "windows"),
            allowance,
            call.data.get("start_time", "07:00:00"),
            call.data.get("end_time", "22:00:00"),
        )

    async def handle_remove_app_time_limit(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass)
        if coordinator is None:
            _LOGGER.error("No Family Safety coordinator available")
            return
        await coordinator.async_remove_app_time_limit(
            call.data["account_id"],
            call.data["app_id"],
            call.data["app_name"],
            call.data.get("platform", "windows"),
        )

    async def handle_block_website(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass)
        if coordinator is None:
            _LOGGER.error("No Family Safety coordinator available")
            return
        await coordinator.async_block_website(
            call.data["account_id"], call.data["website"]
        )

    async def handle_remove_website(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass)
        if coordinator is None:
            _LOGGER.error("No Family Safety coordinator available")
            return
        await coordinator.async_remove_website(
            call.data["account_id"], call.data["website"]
        )

    async def handle_toggle_web_filter(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass)
        if coordinator is None:
            _LOGGER.error("No Family Safety coordinator available")
            return
        await coordinator.async_toggle_web_filter(
            call.data["account_id"], call.data["enabled"]
        )

    async def handle_set_age_rating(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass)
        if coordinator is None:
            _LOGGER.error("No Family Safety coordinator available")
            return
        await coordinator.async_set_age_rating(
            call.data["account_id"], call.data["age"]
        )

    async def handle_set_acquisition_policy(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass)
        if coordinator is None:
            _LOGGER.error("No Family Safety coordinator available")
            return
        await coordinator.async_set_acquisition_policy(
            call.data["account_id"], call.data["require_approval"]
        )

    # ── Register all services ────────────────────────────────────────────

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
    hass.services.async_register(
        DOMAIN, SERVICE_SET_SCREENTIME_LIMIT, handle_set_screentime_limit,
        schema=SERVICE_SET_SCREENTIME_LIMIT_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_SCREENTIME_INTERVALS, handle_set_screentime_intervals,
        schema=SERVICE_SET_SCREENTIME_INTERVALS_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_APP_TIME_LIMIT, handle_set_app_time_limit,
        schema=SERVICE_SET_APP_TIME_LIMIT_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REMOVE_APP_TIME_LIMIT, handle_remove_app_time_limit,
        schema=SERVICE_REMOVE_APP_TIME_LIMIT_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_BLOCK_WEBSITE, handle_block_website,
        schema=SERVICE_BLOCK_WEBSITE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REMOVE_WEBSITE, handle_remove_website,
        schema=SERVICE_REMOVE_WEBSITE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_TOGGLE_WEB_FILTER, handle_toggle_web_filter,
        schema=SERVICE_TOGGLE_WEB_FILTER_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_AGE_RATING, handle_set_age_rating,
        schema=SERVICE_SET_AGE_RATING_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_ACQUISITION_POLICY, handle_set_acquisition_policy,
        schema=SERVICE_SET_ACQUISITION_POLICY_SCHEMA,
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_cleanup()

        # Unregister services if no more entries
        if not hass.data[DOMAIN]:
            for service in ALL_SERVICES:
                hass.services.async_remove(DOMAIN, service)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
