"""Sensor platform for Microsoft Family Safety."""
from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_ACCOUNT_BALANCE,
    ATTR_ACCOUNT_CURRENCY,
    ATTR_AVERAGE_SCREENTIME,
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
    DOMAIN,
)
from .coordinator import FamilySafetyDataUpdateCoordinator

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

    entities: list[SensorEntity] = []

    if coordinator.data:
        for account_id, account_data in coordinator.data.get("accounts", {}).items():
            entities.extend(_create_account_sensors(coordinator, entry, account_id, account_data))

        for device_id in coordinator.data.get("devices", {}):
            entities.extend(_create_device_sensors(coordinator, entry, device_id))

    async_add_entities(entities)


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


class FamilySafetyScreenTimeSensor(FamilySafetyAccountSensor):
    """Sensor for account screen time."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.TOTAL
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

        total_seconds = account_data.get("today_screentime_usage", 0)

        return {
            ATTR_USER_ID: account_data.get(ATTR_USER_ID),
            ATTR_AVERAGE_SCREENTIME: account_data.get("average_screentime_usage", 0),
            "state_class": "total",
            "date": datetime.now().date().isoformat(),
            **_format_duration_attributes(total_seconds),
        }


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

        return {
            "blocked_count": len(blocked_apps),
            "applications": applications,
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

        total_seconds = device_data.get(ATTR_TODAY_TIME_USED, 0)

        return {
            ATTR_DEVICE_ID: device_data.get(ATTR_DEVICE_ID),
            ATTR_DEVICE_NAME: device_data.get(ATTR_DEVICE_NAME),
            "state_class": "total",
            "date": datetime.now().date().isoformat(),
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
