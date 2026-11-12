"""Sensor platform for Microsoft Family Safety."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_ACCOUNT_BALANCE,
    ATTR_ACCOUNT_CURRENCY,
    ATTR_AVERAGE_SCREENTIME,
    ATTR_BLOCKED,
    ATTR_DEVICE_ID,
    ATTR_DEVICE_MODEL,
    ATTR_DEVICE_NAME,
    ATTR_FIRST_NAME,
    ATTR_LAST_SEEN,
    ATTR_OS_NAME,
    ATTR_PROFILE_PICTURE,
    ATTR_SURNAME,
    ATTR_TODAY_TIME_USED,
    ATTR_USER_ID,
    DAY_KEYS,
    DOMAIN,
)
from .coordinator import FamilySafetyDataUpdateCoordinator
from .entity_base import MinuteUpdateMixin

_LOGGER = logging.getLogger(__name__)


def _format_duration_attributes(total_seconds: int) -> dict[str, Any]:
    """Format duration in seconds to hours/minutes/seconds attributes.

    Returns a dictionary with formatted_time, hours, minutes, seconds, and total_seconds.
    This is compatible with Family Link-style attributes.
    """
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    return {
        "total_seconds": total_seconds,
        "formatted_time": f"{hours:02d}:{minutes:02d}:{seconds:02d}",
        "hours": hours,
        "minutes": minutes,
        "seconds": seconds,
    }


def _create_account_sensors(
    coordinator: FamilySafetyDataUpdateCoordinator,
    entry: ConfigEntry,
    account_id: str,
    account_data: dict[str, Any],
) -> list[SensorEntity]:
    """Create all sensors for an account."""
    sensors = [
        FamilySafetyScreenTimeSensor(coordinator, entry, account_id),
        FamilySafetyAccountInfoSensor(coordinator, entry, account_id),
        FamilySafetyApplicationCountSensor(coordinator, entry, account_id),
        FamilySafetyPendingRequestsSensor(coordinator, entry, account_id),
        FamilySafetyWebFilterSensor(coordinator, entry, account_id),
        FamilySafetyWebActivitySensor(coordinator, entry, account_id),
        FamilySafetyScreenTimePolicySensor(coordinator, entry, account_id),
    ]

    if account_data.get("account_balance") is not None:
        sensors.append(FamilySafetyBalanceSensor(coordinator, entry, account_id))

    return sensors


def _create_device_sensors(
    coordinator: FamilySafetyDataUpdateCoordinator,
    entry: ConfigEntry,
    device_id: str,
) -> list[SensorEntity]:
    """Create all sensors for a device."""
    return [
        FamilySafetyDeviceScreenTimeSensor(coordinator, entry, device_id),
        FamilySafetyDeviceInfoSensor(coordinator, entry, device_id),
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Microsoft Family Safety sensors."""
    coordinator: FamilySafetyDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Integration-level connection/health sensor (issue #23)
    async_add_entities([FamilySafetyConnectionSensor(coordinator, entry)])

    known_accounts: set[str] = set()
    known_xbox_accounts: set[str] = set()
    known_windows_accounts: set[str] = set()
    known_devices: set[str] = set()

    def _add_new_entities() -> None:
        """Add sensors for accounts/devices that appeared since last update."""
        entities: list[SensorEntity] = []
        data = coordinator.data or {}

        for account_id, account_data in data.get("accounts", {}).items():
            if account_id not in known_accounts:
                known_accounts.add(account_id)
                entities.extend(
                    _create_account_sensors(coordinator, entry, account_id, account_data)
                )
            # Xbox play time and time left, for children Microsoft has Xbox
            # data for (the Xbox features on, the schedule or play time read).
            if (
                coordinator.xbox_features
                and account_id not in known_xbox_accounts
                and (
                    (account_data or {}).get("xbox_today_usage") is not None
                    or _xbox_schedule(account_data) is not None
                )
            ):
                known_xbox_accounts.add(account_id)
                entities.extend([
                    FamilySafetyXboxScreenTimeSensor(coordinator, entry, account_id),
                    FamilySafetyXboxTimeRemainingSensor(coordinator, entry, account_id),
                ])
            # Windows time left, once the Windows schedule has been read.
            if account_id not in known_windows_accounts and isinstance(
                (account_data or {}).get("screentime_policy"), dict
            ):
                known_windows_accounts.add(account_id)
                entities.append(
                    FamilySafetyTimeRemainingSensor(coordinator, entry, account_id)
                )

        for device_id in data.get("devices", {}):
            if device_id not in known_devices:
                known_devices.add(device_id)
                entities.extend(_create_device_sensors(coordinator, entry, device_id))

        if entities:
            async_add_entities(entities)

    _add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))


class FamilySafetyAccountSensor(CoordinatorEntity, SensorEntity):
    """Base class for account-related sensors."""

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        account_id: str,
    ) -> None:
        """Initialize the account sensor."""
        super().__init__(coordinator)
        self._account_id = account_id
        self._entry = entry

    def _get_account_data(self) -> dict[str, Any] | None:
        """Get account data from coordinator."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("accounts", {}).get(self._account_id)

    def _get_account_name(self) -> str:
        """Get the account first name for entity naming."""
        account_data = self._get_account_data()
        return account_data.get(ATTR_FIRST_NAME, "Unknown") if account_data else "Unknown"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info to link this entity to a child account device."""
        account_data = self._get_account_data()
        first_name = account_data.get(ATTR_FIRST_NAME, "Unknown") if account_data else "Unknown"
        surname = account_data.get(ATTR_SURNAME, "") if account_data else ""
        full_name = f"{first_name} {surname}".strip()
        return DeviceInfo(
            identifiers={(DOMAIN, self._account_id)},
            name=f"{full_name} (Family Safety)",
            manufacturer="Microsoft",
            model="Family Safety Account",
            entry_type=None,
        )


class FamilySafetyDeviceSensor(CoordinatorEntity, SensorEntity):
    """Base class for device-related sensors."""

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        device_id: str,
    ) -> None:
        """Initialize the device sensor."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._entry = entry

    def _get_device_data(self) -> dict[str, Any] | None:
        """Get device data from coordinator."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("devices", {}).get(self._device_id)

    def _get_device_name(self) -> str:
        """Get the device name for entity naming."""
        device_data = self._get_device_data()
        return device_data.get(ATTR_DEVICE_NAME, "Unknown Device") if device_data else "Unknown Device"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info to link this entity to a physical device."""
        device_data = self._get_device_data()
        if not device_data:
            return DeviceInfo(
                identifiers={(DOMAIN, self._device_id)},
                name="Unknown Device",
                manufacturer="Microsoft",
            )
        account_id = device_data.get("account_id")
        info = DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=device_data.get(ATTR_DEVICE_NAME, "Unknown Device"),
            manufacturer=device_data.get("device_make", "Unknown"),
            model=device_data.get("device_model"),
        )
        if account_id:
            # `via_device` is deprecated (removed in HA 2027.8): the link now
            # needs the registry id of the child's device. The account sensors
            # fill in its name and model; creating it bare here only
            # guarantees it exists whatever the order entities are added in.
            parent = dr.async_get(self.hass).async_get_or_create(
                config_entry_id=self.coordinator.entry.entry_id,
                identifiers={(DOMAIN, account_id)},
            )
            info["via_device_id"] = parent.id
        return info


class FamilySafetyScreenTimeSensor(FamilySafetyAccountSensor):
    """Sensor for account screen time."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:clock-outline"

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        account_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, account_id)
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_screentime"
        self._attr_name = f"{self._get_account_name()} Screen Time"

    @property
    def native_value(self) -> int | None:
        """Return the screen time in minutes."""
        account_data = self._get_account_data()
        return account_data.get("today_screentime_usage", 0) if account_data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        account_data = self._get_account_data()
        if not account_data:
            return {}

        # Value is already in minutes from coordinator, convert to seconds for formatting
        total_minutes = account_data.get("today_screentime_usage", 0)
        total_seconds = total_minutes * 60

        return {
            ATTR_USER_ID: account_data.get(ATTR_USER_ID),
            ATTR_AVERAGE_SCREENTIME: account_data.get("average_screentime_usage", 0),
            "date": account_data.get("screen_time_date") or dt_util.now().date().isoformat(),
            "raw_microsoft_minutes": account_data.get("raw_today_screentime_usage"),
            "raw_microsoft_ms": account_data.get("raw_today_screentime_ms"),
            "last_api_poll": account_data.get("screen_time_polled_at"),
            "update_interval_seconds": (
                int(self.coordinator.update_interval.total_seconds())
                if self.coordinator.update_interval
                else None
            ),
            **_format_duration_attributes(total_seconds),
        }


def _xbox_schedule(account_data: dict[str, Any] | None) -> dict[str, Any] | None:
    """The child's Xbox schedule as the coordinator keeps it, if read."""
    policy = (account_data or {}).get("xbox_screentime_policy")
    return policy if isinstance(policy, dict) else None


class FamilySafetyXboxScreenTimeSensor(FamilySafetyAccountSensor):
    """Xbox play today, from the Xbox activity report.

    The account Screen Time sensor covers every platform; this one is the
    Xbox alone. Unknown until the report has been read today.
    """

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:microsoft-xbox"

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        account_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, account_id)
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_xbox_screentime"
        self._attr_name = f"{self._get_account_name()} Xbox Screen Time"

    @property
    def native_value(self) -> int | None:
        """Return today's Xbox play in minutes."""
        value = (self._get_account_data() or {}).get("xbox_today_usage")
        return value if isinstance(value, int) else None


class FamilySafetyTimeRemainingSensor(MinuteUpdateMixin, FamilySafetyAccountSensor):
    """Minutes the child may still use Windows today.

    The same rules as the "Allowed Now" binary sensor (see
    screen_time.platform_allowance): 0 while locked, outside today's allowed
    hours or once the allowance is used; otherwise the smaller of the
    allowance left and the time to the end of the current allowed window.
    With limits turned off it counts down to midnight. Unknown while the
    schedule or today's usage is unknown.
    """

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_icon = "mdi:timer-sand"
    _platform = "Windows"
    _unrecorded_attributes = frozenset({"used_minutes", "allowed_ranges"})

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        account_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, account_id)
        self._attr_unique_id = (
            f"{entry.entry_id}_{account_id}_{self._platform.lower()}_time_remaining"
        )
        self._attr_name = f"{self._get_account_name()} {self._platform} Time Remaining"

    def _status(self) -> dict[str, Any]:
        return self.coordinator.platform_status(self._account_id, self._platform)

    def _schedule(self) -> dict[str, Any] | None:
        policy = (self._get_account_data() or {}).get("screentime_policy")
        return policy if isinstance(policy, dict) else None

    @property
    def native_value(self) -> int | None:
        """Return the minutes left today."""
        return self._status()["remaining_minutes"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return why, today's allowance and allowed ranges."""
        status = self._status()
        attrs: dict[str, Any] = {
            ATTR_USER_ID: self._account_id,
            "allowed_now": status["allowed"],
            "reason": status["reason"],
            "allowance_minutes": status["allowance_minutes"],
            "used_minutes": status["used_minutes"],
        }
        policy = self._schedule()
        if policy is not None:
            attrs["limits_enabled"] = policy.get("isEnabled", policy.get("IsEnabled")) is not False
            daily = policy.get("dailyRestrictions") or policy.get("DailyRestrictions") or {}
            day_key = DAY_KEYS[(dt_util.now().weekday() + 1) % 7]
            day = daily.get(day_key) or daily.get(day_key.capitalize())
            if isinstance(day, dict):
                # Begin/end pairs only, not a 48-slot timeline.
                attrs["allowed_ranges"] = next(
                    (
                        ranges
                        for key in ("allottedIntervals", "allowedIntervals", "AllowedIntervals")
                        if isinstance(ranges := day.get(key), list)
                        and ranges
                        and all(isinstance(r, dict) for r in ranges)
                    ),
                    [],
                )
        return attrs


class FamilySafetyXboxTimeRemainingSensor(FamilySafetyTimeRemainingSensor):
    """Xbox time left today (Xbox schedule, Xbox play time, Xbox lock or extra time)."""

    _platform = "Xbox"

    def _schedule(self) -> dict[str, Any] | None:
        return _xbox_schedule(self._get_account_data())


class FamilySafetyAccountInfoSensor(FamilySafetyAccountSensor):
    """Sensor for account information."""

    _attr_icon = "mdi:account"

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        account_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, account_id)
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_info"
        self._attr_name = f"{self._get_account_name()} Account Info"

    @property
    def native_value(self) -> str | None:
        """Return the account name."""
        account_data = self._get_account_data()
        if not account_data:
            return None

        first_name = account_data.get(ATTR_FIRST_NAME, "")
        surname = account_data.get(ATTR_SURNAME, "")
        return f"{first_name} {surname}".strip()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        account_data = self._get_account_data()
        if not account_data:
            return {}

        return {
            ATTR_USER_ID: account_data.get(ATTR_USER_ID),
            ATTR_FIRST_NAME: account_data.get(ATTR_FIRST_NAME),
            ATTR_SURNAME: account_data.get(ATTR_SURNAME),
            ATTR_PROFILE_PICTURE: account_data.get(ATTR_PROFILE_PICTURE),
            "device_count": len(account_data.get("devices", [])),
            "application_count": len(account_data.get("applications", [])),
        }

    @property
    def entity_picture(self) -> str | None:
        """Return the entity picture."""
        account_data = self._get_account_data()
        return account_data.get(ATTR_PROFILE_PICTURE) if account_data else None


class FamilySafetyApplicationCountSensor(FamilySafetyAccountSensor):
    """Sensor for application count."""

    _attr_icon = "mdi:apps"
    _attr_state_class = SensorStateClass.MEASUREMENT
    # The per-app list (usage changes every poll, plus the limits) would be
    # written to the recorder on every update; the state keeps the count.
    _unrecorded_attributes = frozenset({"applications"})

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        account_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, account_id)
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_app_count"
        self._attr_name = f"{self._get_account_name()} Applications"

    @property
    def native_value(self) -> int | None:
        """Return the application count."""
        account_data = self._get_account_data()
        return len(account_data.get("applications", [])) if account_data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        account_data = self._get_account_data()
        if not account_data:
            return {}

        applications = account_data.get("applications", [])
        blocked_apps = [app for app in applications if app.get("blocked")]
        names = {app.get("app_id"): app.get("app_name") for app in applications}

        return {
            "blocked_count": len(blocked_apps),
            "applications": applications,
            # Apps pause_app paused: "ours" while still blocked, "parent"
            # once someone unblocked them in the meantime.
            "paused_apps": [
                {"app_id": app_id, "app_name": names.get(app_id) or app_id, "paused": who}
                for app_id, who in sorted(
                    self.coordinator.paused_apps(self._account_id).items()
                )
            ],
        }


class FamilySafetyBalanceSensor(FamilySafetyAccountSensor):
    """Sensor for account balance."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:cash"

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        account_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, account_id)
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_balance"

        account_data = self._get_account_data()
        self._attr_name = f"{self._get_account_name()} Balance"
        if account_data:
            self._attr_native_unit_of_measurement = account_data.get(
                ATTR_ACCOUNT_CURRENCY, "USD"
            )

    @property
    def native_value(self) -> float | None:
        """Return the account balance."""
        account_data = self._get_account_data()
        return account_data.get(ATTR_ACCOUNT_BALANCE) if account_data else None


class FamilySafetyDeviceScreenTimeSensor(FamilySafetyDeviceSensor):
    """Sensor for device screen time."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:cellphone-clock"

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        device_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, device_id)
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_screentime"
        self._attr_name = f"{self._get_device_name()} Screen Time"

    @property
    def native_value(self) -> int | None:
        """Return the screen time in minutes."""
        device_data = self._get_device_data()
        return device_data.get(ATTR_TODAY_TIME_USED, 0) if device_data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        device_data = self._get_device_data()
        if not device_data:
            return {}

        # Value is already in minutes from coordinator, convert to seconds for formatting
        total_minutes = device_data.get(ATTR_TODAY_TIME_USED, 0)
        total_seconds = total_minutes * 60

        return {
            ATTR_DEVICE_ID: device_data.get(ATTR_DEVICE_ID),
            ATTR_DEVICE_NAME: device_data.get(ATTR_DEVICE_NAME),
            "date": dt_util.now().date().isoformat(),
            **_format_duration_attributes(total_seconds),
        }


class FamilySafetyDeviceInfoSensor(FamilySafetyDeviceSensor):
    """Sensor for device information."""

    _attr_icon = "mdi:cellphone-information"

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        device_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, device_id)
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_info"
        self._attr_name = f"{self._get_device_name()} Info"

    @property
    def native_value(self) -> str | None:
        """Return the device name."""
        device_data = self._get_device_data()
        return device_data.get(ATTR_DEVICE_NAME) if device_data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        device_data = self._get_device_data()
        if not device_data:
            return {}

        return {
            ATTR_DEVICE_ID: device_data.get(ATTR_DEVICE_ID),
            ATTR_DEVICE_NAME: device_data.get(ATTR_DEVICE_NAME),
            ATTR_DEVICE_MODEL: device_data.get("device_model"),
            ATTR_OS_NAME: device_data.get(ATTR_OS_NAME),
            ATTR_LAST_SEEN: device_data.get(ATTR_LAST_SEEN),
            "device_make": device_data.get("device_make"),
            "device_class": device_data.get("device_class"),
        }


class FamilySafetyPendingRequestsSensor(FamilySafetyAccountSensor):
    """Sensor for pending screen time requests."""

    _attr_icon = "mdi:message-alert"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        account_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, account_id)
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_pending_requests"
        self._attr_name = f"{self._get_account_name()} Pending Requests"

    @property
    def native_value(self) -> int:
        """Return the number of pending requests for this account."""
        if not self.coordinator.data:
            return 0
        all_requests = self.coordinator.data.get("pending_requests", [])
        account_requests = [
            r for r in all_requests if r.get("puid") == self._account_id
        ]
        return len(account_requests)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return pending requests details."""
        if not self.coordinator.data:
            return {}
        all_requests = self.coordinator.data.get("pending_requests", [])
        account_requests = [
            r for r in all_requests if r.get("puid") == self._account_id
        ]
        return {
            ATTR_USER_ID: self._account_id,
            "requests": account_requests,
        }


class FamilySafetyWebFilterSensor(FamilySafetyAccountSensor):
    """Sensor for web filtering status."""

    _attr_icon = "mdi:web"

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        account_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, account_id)
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_web_filter"
        self._attr_name = f"{self._get_account_name()} Web Filter"

    @property
    def native_value(self) -> str | None:
        """Return the web filter status."""
        account_data = self._get_account_data()
        if not account_data:
            return None
        web_data = account_data.get("web_browsing")
        if web_data is None:
            return "unknown"
        if isinstance(web_data, dict):
            # API returns "enabled" (not "isEnabled")
            is_enabled = web_data.get("enabled", web_data.get("isEnabled", web_data.get("IsEnabled")))
            if is_enabled is True:
                return "enabled"
            if is_enabled is False:
                return "disabled"
        return "unknown"

    @property
    def icon(self) -> str:
        """Return icon based on filter state."""
        if self.native_value == "enabled":
            return "mdi:web-check"
        if self.native_value == "disabled":
            return "mdi:web-off"
        return "mdi:web"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return web filtering details."""
        account_data = self._get_account_data()
        if not account_data:
            return {}
        web_data = account_data.get("web_browsing")
        if web_data is None:
            return {ATTR_USER_ID: self._account_id}
        attrs = {ATTR_USER_ID: self._account_id}
        if isinstance(web_data, dict):
            # Include all useful fields from the API response
            for key in (
                "enabled", "isEnabled", "IsEnabled",
                "filterLevel", "FilterLevel",
                "categories", "Categories",
                "useAllowedListOnly", "UseAllowedListOnly",
                "blockedBrowsersEnabled", "BlockedBrowsersEnabled",
                "restrictUnknown", "RestrictUnknown",
                "warnWhenRestricted", "WarnWhenRestricted",
                "exceptions", "Exceptions",
                "blockedSites", "allowedSites", "BlockedSites", "AllowedSites",
                "contentRatingAge", "ContentRatingAge",
            ):
                if key in web_data:
                    attrs[key] = web_data[key]
        content_data = account_data.get("content_settings")
        if isinstance(content_data, dict):
            attrs["content_settings"] = content_data
            for source, target in (
                ("maxAgeRating", "max_age_rating"),
                ("MaxAgeRating", "max_age_rating"),
                ("acquisitionPolicy", "acquisition_policy"),
                ("AcquisitionPolicy", "acquisition_policy"),
            ):
                if source in content_data and target not in attrs:
                    attrs[target] = content_data[source]
        return attrs


class FamilySafetyWebActivitySensor(FamilySafetyAccountSensor):
    """Websites visited in Edge over the last 7 days, with the Bing searches (#65)."""

    _attr_icon = "mdi:web-clock"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "visits"
    # Up to 50 sites, searches and blocked URLs: keep them out of the recorder.
    _unrecorded_attributes = frozenset({"sites", "searches", "blocked_sites"})

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        account_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, account_id)
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_web_activity"
        self._attr_name = f"{self._get_account_name()} Websites Visited"

    def _activity(self) -> dict[str, Any] | None:
        account_data = self._get_account_data()
        return account_data.get("web_activity") if account_data else None

    @property
    def native_value(self) -> int | None:
        """Return the number of visits over the last 7 days."""
        activity = self._activity()
        return activity.get("total_visits") if activity else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the sites (most visited first) and the latest searches."""
        activity = self._activity()
        if not activity:
            return {}
        return {key: value for key, value in activity.items() if key != "total_visits"}


class FamilySafetyScreenTimePolicySensor(FamilySafetyAccountSensor):
    """Sensor for screen time policy details."""

    _attr_icon = "mdi:clock-check"

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        account_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, account_id)
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_screentime_policy"
        self._attr_name = f"{self._get_account_name()} Screen Time Policy"

    @property
    def native_value(self) -> str | None:
        """Return whether screen time limits are enabled."""
        account_data = self._get_account_data()
        if not account_data:
            return None
        policy = account_data.get("screentime_policy")
        if policy is None:
            return "unknown"
        if isinstance(policy, dict):
            is_enabled = policy.get("isEnabled", policy.get("IsEnabled"))
            if is_enabled is True:
                return "enabled"
            if is_enabled is False:
                return "disabled"
        state = self.coordinator.is_policy_enabled(self._account_id)
        if state is True:
            return "enabled"
        if state is False:
            return "disabled"
        return "unknown"

    @property
    def icon(self) -> str:
        """Return icon based on policy state."""
        if self.native_value == "enabled":
            return "mdi:clock-check"
        if self.native_value == "disabled":
            return "mdi:clock-remove"
        return "mdi:clock-outline"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return screen time policy details."""
        account_data = self._get_account_data()
        if not account_data:
            return {}
        policy = account_data.get("screentime_policy")
        attrs = {ATTR_USER_ID: self._account_id}
        if policy and isinstance(policy, dict):
            daily = policy.get("dailyRestrictions", policy.get("DailyRestrictions"))
            if daily and isinstance(daily, dict):
                for day_name, day_data in daily.items():
                    if isinstance(day_data, dict):
                        allowance = day_data.get("allowance", day_data.get("Allowance", ""))
                        attrs[f"{day_name.lower()}_allowance"] = allowance
                        intervals = day_data.get(
                            "allowedIntervals", day_data.get("AllowedIntervals")
                        )
                        if isinstance(intervals, list):
                            attrs[f"{day_name.lower()}_allowed_intervals"] = intervals
        # The Xbox allowed hours, e.g. [{"begin": "07:00", "end": "22:00"}]. Only
        # those read from the Xbox Family Settings service, which the console
        # enforces.
        xbox = account_data.get("xbox_screentime_policy")
        xbox_daily = (
            xbox.get("dailyRestrictions")
            if isinstance(xbox, dict) and xbox.get("source") == "xapprove"
            else None
        )
        if isinstance(xbox_daily, dict):
            for day_name, day_data in xbox_daily.items():
                intervals = day_data.get("allowedIntervals") if isinstance(day_data, dict) else None
                if isinstance(intervals, list):
                    attrs[f"xbox_{day_name.lower()}_allowed_intervals"] = intervals
        return attrs


class FamilySafetyConnectionSensor(CoordinatorEntity, SensorEntity):
    """Integration-level connection/health sensor (issue #23).

    Surfaces whether the mobile API and the web (cookie) session are working,
    so users can see at a glance if the integration is connected, degraded
    (mobile only, web cookies expired), or disconnected — addressing the
    "test connection / connection state" request in issue #23.
    """

    _attr_icon = "mdi:lan-connect"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the connection sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_connection"
        self._attr_name = "Microsoft Family Safety Connection"

    @property
    def available(self) -> bool:
        """The connection sensor is always available to report status."""
        return True

    @property
    def device_info(self) -> DeviceInfo:
        """Link to a shared integration device."""
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry.entry_id}_service")},
            name="Microsoft Family Safety",
            manufacturer="Microsoft",
            model="Family Safety Integration",
        )

    @property
    def native_value(self) -> str:
        """Return the overall connection state."""
        return self.coordinator.connection_state()["state"]

    @property
    def icon(self) -> str:
        """Return icon based on connection state."""
        state = self.native_value
        if state == "connected":
            return "mdi:lan-connect"
        if state == "degraded":
            return "mdi:lan-pending"
        return "mdi:lan-disconnect"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return detailed connection diagnostics."""
        return self.coordinator.connection_state()
