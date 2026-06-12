"""The Microsoft Family Safety integration."""
from __future__ import annotations

from datetime import datetime, timedelta
import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
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
    SERVICE_LOCK_ACCOUNT,
    SERVICE_UNLOCK_ACCOUNT,
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

SERVICE_LOCK_ACCOUNT_SCHEMA = vol.Schema({
    vol.Required("account_id"): cv.string,
})

SERVICE_UNLOCK_ACCOUNT_SCHEMA = vol.Schema({
    vol.Required("account_id"): cv.string,
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

    # Load persisted screentime policies (for lock/unlock survival across restarts)
    await coordinator.async_load_saved_screentime()

    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryAuthFailed:
        # Real auth failure — let HA trigger reauth flow
        raise
    except Exception as err:
        _LOGGER.warning("Microsoft Family Safety not ready, will retry: %s", err)
        raise ConfigEntryNotReady from err

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services (only once)
    if not hass.services.has_service(DOMAIN, SERVICE_BLOCK_APP):
        _register_services(hass)

    # Reload on options change (e.g. update interval)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update — reload the integration."""
    await hass.config_entries.async_reload(entry.entry_id)


def _register_services(hass: HomeAssistant) -> None:
    """Register integration services.

    Each service maps to a coordinator method; the args extractor turns the
    validated service call data into positional arguments for that method.
    """

    def _app_allowance(data: dict) -> str:
        return f"{data['hours']:02d}:{data.get('minutes', 0):02d}:00"

    # (service name, schema, coordinator method, args extractor)
    services = [
        (SERVICE_BLOCK_APP, SERVICE_BLOCK_APP_SCHEMA, "async_block_app",
         lambda d: (d["account_id"], d["app_id"])),
        (SERVICE_UNBLOCK_APP, SERVICE_BLOCK_APP_SCHEMA, "async_unblock_app",
         lambda d: (d["account_id"], d["app_id"])),
        (SERVICE_LOCK_PLATFORM, SERVICE_LOCK_PLATFORM_SCHEMA, "async_lock_platform",
         lambda d: (
             d["account_id"], d["platform"],
             datetime.now() + timedelta(hours=d.get("duration_hours", 24)),
         )),
        (SERVICE_UNLOCK_PLATFORM, SERVICE_UNLOCK_PLATFORM_SCHEMA, "async_unlock_platform",
         lambda d: (d["account_id"], d["platform"])),
        (SERVICE_APPROVE_REQUEST, SERVICE_APPROVE_REQUEST_SCHEMA, "async_approve_request",
         lambda d: (d["request_id"], d.get("extension_minutes", 60) * 60)),
        (SERVICE_DENY_REQUEST, SERVICE_DENY_REQUEST_SCHEMA, "async_deny_request",
         lambda d: (d["request_id"],)),
        (SERVICE_SET_SCREENTIME_LIMIT, SERVICE_SET_SCREENTIME_LIMIT_SCHEMA,
         "async_set_screentime_limit",
         lambda d: (d["account_id"], d["day_of_week"], d["hours"], d.get("minutes", 0))),
        (SERVICE_SET_SCREENTIME_INTERVALS, SERVICE_SET_SCREENTIME_INTERVALS_SCHEMA,
         "async_set_screentime_intervals",
         lambda d: (
             d["account_id"], d["day_of_week"],
             d["start_hour"], d.get("start_minute", 0),
             d["end_hour"], d.get("end_minute", 0),
         )),
        (SERVICE_SET_APP_TIME_LIMIT, SERVICE_SET_APP_TIME_LIMIT_SCHEMA,
         "async_set_app_time_limit",
         lambda d: (
             d["account_id"], d["app_id"], d["app_name"],
             d.get("platform", "windows"), _app_allowance(d),
             d.get("start_time", "07:00:00"), d.get("end_time", "22:00:00"),
         )),
        (SERVICE_REMOVE_APP_TIME_LIMIT, SERVICE_REMOVE_APP_TIME_LIMIT_SCHEMA,
         "async_remove_app_time_limit",
         lambda d: (
             d["account_id"], d["app_id"], d["app_name"],
             d.get("platform", "windows"),
         )),
        (SERVICE_BLOCK_WEBSITE, SERVICE_BLOCK_WEBSITE_SCHEMA, "async_block_website",
         lambda d: (d["account_id"], d["website"])),
        (SERVICE_REMOVE_WEBSITE, SERVICE_REMOVE_WEBSITE_SCHEMA, "async_remove_website",
         lambda d: (d["account_id"], d["website"])),
        (SERVICE_TOGGLE_WEB_FILTER, SERVICE_TOGGLE_WEB_FILTER_SCHEMA,
         "async_toggle_web_filter",
         lambda d: (d["account_id"], d["enabled"])),
        (SERVICE_SET_AGE_RATING, SERVICE_SET_AGE_RATING_SCHEMA, "async_set_age_rating",
         lambda d: (d["account_id"], d["age"])),
        (SERVICE_SET_ACQUISITION_POLICY, SERVICE_SET_ACQUISITION_POLICY_SCHEMA,
         "async_set_acquisition_policy",
         lambda d: (d["account_id"], d["require_approval"])),
        (SERVICE_LOCK_ACCOUNT, SERVICE_LOCK_ACCOUNT_SCHEMA, "async_lock_account",
         lambda d: (d["account_id"],)),
        (SERVICE_UNLOCK_ACCOUNT, SERVICE_UNLOCK_ACCOUNT_SCHEMA, "async_unlock_account",
         lambda d: (d["account_id"],)),
    ]

    def make_handler(method_name, extract_args):
        async def handler(call: ServiceCall) -> None:
            coordinator = _get_coordinator(hass)
            if coordinator is None:
                _LOGGER.error("No Family Safety coordinator available")
                return
            await getattr(coordinator, method_name)(*extract_args(call.data))
        return handler

    for name, schema, method, extract in services:
        hass.services.async_register(
            DOMAIN, name, make_handler(method, extract), schema=schema
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
