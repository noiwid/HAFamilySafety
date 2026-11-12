"""The Microsoft Family Safety integration."""
from __future__ import annotations

from datetime import timedelta
import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

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
    SERVICE_ALLOW_WEBSITE,
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
    SERVICE_REQUEST_REAUTH,
    CONF_ALLOWED_USERS,
)
from .coordinator import FamilySafetyDataUpdateCoordinator
from .permissions import guard_service
from .targeting import child_schema, resolve_account_id
from ._httpx_web_adapter import apply_httpx_web_transport_patch
from ._httpx_web_tuning import apply_httpx_web_tuning_patch

_LOGGER = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Service Schemas
# ──────────────────────────────────────────────────────────────────────


def _platform(*choices: str):
    """A platform in any case (``xbox``, ``XBOX``), given on as the canonical choice."""
    by_key = {choice.casefold(): choice for choice in choices}

    def validate(value):
        choice = by_key.get(str(value).strip().casefold())
        if choice is None:
            raise vol.Invalid(f"platform must be one of {', '.join(choices)}")
        return choice

    return validate


SERVICE_BLOCK_APP_SCHEMA = child_schema({
    vol.Required("app_id"): cv.string,
})

SERVICE_LOCK_PLATFORM_SCHEMA = child_schema({
    vol.Required("platform"): _platform("Windows", "Xbox", "Mobile"),
    vol.Optional("duration_hours", default=24): vol.All(
        vol.Coerce(int), vol.Range(min=1, max=168)
    ),
})

SERVICE_UNLOCK_PLATFORM_SCHEMA = child_schema({
    vol.Required("platform"): _platform("Windows", "Xbox", "Mobile"),
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

SERVICE_SET_SCREENTIME_LIMIT_SCHEMA = child_schema({
    vol.Required("day_of_week"): vol.All(vol.Coerce(int), vol.Range(min=0, max=6)),
    vol.Required("hours"): vol.All(vol.Coerce(int), vol.Range(min=0, max=24)),
    vol.Optional("minutes", default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=59)),
    vol.Optional("platform", default="Windows"): _platform("Windows", "Xbox"),
})

SERVICE_SET_SCREENTIME_INTERVALS_SCHEMA = child_schema({
    vol.Required("day_of_week"): vol.All(vol.Coerce(int), vol.Range(min=0, max=6)),
    vol.Required("start_hour"): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
    vol.Optional("start_minute", default=0): vol.In([0, 30]),
    # 24 = midnight at the end of the day (with end_minute 0).
    vol.Required("end_hour"): vol.All(vol.Coerce(int), vol.Range(min=0, max=24)),
    vol.Optional("end_minute", default=0): vol.In([0, 30]),
    vol.Optional("platform", default="Windows"): _platform("Windows", "Xbox"),
})

SERVICE_SET_APP_TIME_LIMIT_SCHEMA = child_schema({
    vol.Required("app_id"): cv.string,
    vol.Required("app_name"): cv.string,
    vol.Optional("platform", default="windows"): _platform("windows", "xbox", "mobile"),
    vol.Required("hours"): vol.All(vol.Coerce(int), vol.Range(min=0, max=24)),
    vol.Optional("minutes", default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=59)),
    vol.Optional("start_time", default="07:00:00"): cv.string,
    vol.Optional("end_time", default="22:00:00"): cv.string,
})

SERVICE_REMOVE_APP_TIME_LIMIT_SCHEMA = child_schema({
    vol.Required("app_id"): cv.string,
    vol.Required("app_name"): cv.string,
    vol.Optional("platform", default="windows"): _platform("windows", "xbox", "mobile"),
})

SERVICE_BLOCK_WEBSITE_SCHEMA = child_schema({
    vol.Required("website"): cv.string,
})

SERVICE_REMOVE_WEBSITE_SCHEMA = child_schema({
    vol.Required("website"): cv.string,
})

SERVICE_TOGGLE_WEB_FILTER_SCHEMA = child_schema({
    vol.Required("enabled"): cv.boolean,
})

SERVICE_SET_AGE_RATING_SCHEMA = child_schema({
    vol.Required("age"): vol.All(vol.Coerce(int), vol.Range(min=3, max=21)),
})

SERVICE_SET_ACQUISITION_POLICY_SCHEMA = child_schema({
    vol.Required("require_approval"): cv.boolean,
})

SERVICE_LOCK_ACCOUNT_SCHEMA = child_schema({})

SERVICE_UNLOCK_ACCOUNT_SCHEMA = child_schema({})

SERVICE_REQUEST_REAUTH_SCHEMA = vol.Schema({})


def _get_coordinator(
    hass: HomeAssistant, account_id: str | None = None
) -> FamilySafetyDataUpdateCoordinator | None:
    """The coordinator whose roster holds ``account_id``, else the first one.

    With two Microsoft accounts configured, each entry has its own token: a
    call sent to the wrong one is refused by Microsoft.
    """
    coordinators = [
        c for c in hass.data.get(DOMAIN, {}).values()
        if isinstance(c, FamilySafetyDataUpdateCoordinator)
    ]
    for coordinator in coordinators:
        if account_id is not None and str(account_id) in (
            str(k) for k in ((coordinator.data or {}).get("accounts") or {})
        ):
            return coordinator
    return coordinators[0] if coordinators else None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Microsoft Family Safety from a config entry."""
    apply_httpx_web_transport_patch(hass)
    apply_httpx_web_tuning_patch()
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
    _LOADED_OPTIONS[entry.entry_id] = _reload_relevant(entry.options)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


#: entry_id -> the options the entry was loaded with, minus those read live.
_LOADED_OPTIONS: dict[str, dict] = {}


def _reload_relevant(options) -> dict:
    """Options whose change needs a reload. The allowed users are read at each action."""
    return {k: v for k, v in dict(options).items() if k != CONF_ALLOWED_USERS}


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload on an options change, unless only the allowed users changed."""
    if _LOADED_OPTIONS.get(entry.entry_id) == _reload_relevant(entry.options):
        return
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
             dt_util.now() + timedelta(hours=d.get("duration_hours", 24)),
         )),
        (SERVICE_UNLOCK_PLATFORM, SERVICE_UNLOCK_PLATFORM_SCHEMA, "async_unlock_platform",
         lambda d: (d["account_id"], d["platform"])),
        (SERVICE_APPROVE_REQUEST, SERVICE_APPROVE_REQUEST_SCHEMA, "async_approve_request",
         lambda d: (d["request_id"], d.get("extension_minutes", 60) * 60)),
        (SERVICE_DENY_REQUEST, SERVICE_DENY_REQUEST_SCHEMA, "async_deny_request",
         lambda d: (d["request_id"],)),
        (SERVICE_SET_SCREENTIME_LIMIT, SERVICE_SET_SCREENTIME_LIMIT_SCHEMA,
         "async_set_screentime_limit",
         lambda d: (
             d["account_id"], d["day_of_week"], d["hours"], d.get("minutes", 0),
             d.get("platform", "Windows"),
         )),
        (SERVICE_SET_SCREENTIME_INTERVALS, SERVICE_SET_SCREENTIME_INTERVALS_SCHEMA,
         "async_set_screentime_intervals",
         lambda d: (
             d["account_id"], d["day_of_week"],
             d["start_hour"], d.get("start_minute", 0),
             d["end_hour"], d.get("end_minute", 0),
             d.get("platform", "Windows"),
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
        (SERVICE_ALLOW_WEBSITE, SERVICE_BLOCK_WEBSITE_SCHEMA, "async_allow_website",
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
        (SERVICE_REQUEST_REAUTH, SERVICE_REQUEST_REAUTH_SCHEMA, "async_request_reauth",
         lambda d: ()),
    ]

    def make_handler(method_name, extract_args, per_child):
        async def handler(call: ServiceCall) -> None:
            data = dict(call.data)
            if per_child:
                # account_id as before, or any entity of the child.
                data["account_id"] = resolve_account_id(hass, data)
            coordinator = _get_coordinator(hass, data.get("account_id"))
            if coordinator is None:
                raise HomeAssistantError("No Microsoft Family Safety coordinator available")
            try:
                await getattr(coordinator, method_name)(*extract_args(data))
            except HomeAssistantError:
                raise
            except Exception as err:  # noqa: BLE001 - report as a service error
                # A raw exception surfaced as HTTP 500 on the REST API and could
                # not be skipped with continue_on_error in automations (#54).
                raise HomeAssistantError(
                    f"Microsoft Family Safety {call.service} failed: {err}"
                ) from err
        return handler

    for name, schema, method, extract in services:
        per_child = schema not in (
            SERVICE_APPROVE_REQUEST_SCHEMA,
            SERVICE_DENY_REQUEST_SCHEMA,
            SERVICE_REQUEST_REAUTH_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN, name, guard_service(hass, make_handler(method, extract, per_child)),
            schema=schema,
        )


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the Xbox Family Settings refresh token kept for this entry."""
    from .xbox_family_api import XboxFamilyAPI

    await XboxFamilyAPI(hass, entry.entry_id).async_unlink()


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
