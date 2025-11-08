"""Sensor platform for Microsoft Family Safety."""
from __future__ import annotations

from datetime import datetime, timedelta
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Microsoft Family Safety sensors."""
    coordinator: FamilySafetyDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = []

    if coordinator.data:
        # Create sensors for each account
        for account_id, account_data in coordinator.data.get("accounts", {}).items():
            # Account screentime sensor
            entities.append(
                FamilySafetyScreenTimeSensor(
                    coordinator,
                    entry,
                    account_id,
                )
            )

            # Account info sensor
            entities.append(
                FamilySafetyAccountInfoSensor(
                    coordinator,
                    entry,
                    account_id,
                )
            )

            # Application count sensors
            entities.append(
                FamilySafetyApplicationCountSensor(
                    coordinator,
                    entry,
                    account_id,
                )
            )

            # Account balance sensor (if available)
            if account_data.get("account_balance") is not None:
                entities.append(
                    FamilySafetyBalanceSensor(
                        coordinator,
                        entry,
                        account_id,
                    )
                )

        # Create sensors for each device
        for device_id, device_data in coordinator.data.get("devices", {}).items():
            # Device screen time sensor
            entities.append(
                FamilySafetyDeviceScreenTimeSensor(
                    coordinator,
                    entry,
                    device_id,
                )
            )

            # Device info sensor
            entities.append(
                FamilySafetyDeviceInfoSensor(
                    coordinator,
                    entry,
                    device_id,
                )
            )

    async_add_entities(entities)


class FamilySafetyScreenTimeSensor(CoordinatorEntity, SensorEntity):
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
        super().__init__(coordinator)
        self._account_id = account_id
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_screentime"

        account_data = self._get_account_data()
        if account_data:
            name = account_data.get(ATTR_FIRST_NAME, "Unknown")
            self._attr_name = f"{name} Screen Time"

    def _get_account_data(self) -> dict[str, Any] | None:
        """Get account data from coordinator."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("accounts", {}).get(self._account_id)

    @property
    def native_value(self) -> int | None:
        """Return the screen time in minutes."""
        account_data = self._get_account_data()
        if not account_data:
            return None
        return account_data.get("today_screentime_usage", 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        account_data = self._get_account_data()
        if not account_data:
            return {}

        return {
            ATTR_AVERAGE_SCREENTIME: account_data.get("average_screentime_usage", 0),
            ATTR_USER_ID: account_data.get(ATTR_USER_ID),
        }


class FamilySafetyAccountInfoSensor(CoordinatorEntity, SensorEntity):
    """Sensor for account information."""

    _attr_icon = "mdi:account"

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        account_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._account_id = account_id
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_info"

        account_data = self._get_account_data()
        if account_data:
            name = account_data.get(ATTR_FIRST_NAME, "Unknown")
            self._attr_name = f"{name} Account Info"

    def _get_account_data(self) -> dict[str, Any] | None:
        """Get account data from coordinator."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("accounts", {}).get(self._account_id)

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

        attrs = {
            ATTR_USER_ID: account_data.get(ATTR_USER_ID),
            ATTR_FIRST_NAME: account_data.get(ATTR_FIRST_NAME),
            ATTR_SURNAME: account_data.get(ATTR_SURNAME),
            ATTR_PROFILE_PICTURE: account_data.get(ATTR_PROFILE_PICTURE),
            "device_count": len(account_data.get("devices", [])),
            "application_count": len(account_data.get("applications", [])),
        }

        return attrs

    @property
    def entity_picture(self) -> str | None:
        """Return the entity picture."""
        account_data = self._get_account_data()
        if not account_data:
            return None
        return account_data.get(ATTR_PROFILE_PICTURE)


class FamilySafetyApplicationCountSensor(CoordinatorEntity, SensorEntity):
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
        super().__init__(coordinator)
        self._account_id = account_id
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_app_count"

        account_data = self._get_account_data()
        if account_data:
            name = account_data.get(ATTR_FIRST_NAME, "Unknown")
            self._attr_name = f"{name} Applications"

    def _get_account_data(self) -> dict[str, Any] | None:
        """Get account data from coordinator."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("accounts", {}).get(self._account_id)

    @property
    def native_value(self) -> int | None:
        """Return the application count."""
        account_data = self._get_account_data()
        if not account_data:
            return None
        return len(account_data.get("applications", []))

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


class FamilySafetyBalanceSensor(CoordinatorEntity, SensorEntity):
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
        super().__init__(coordinator)
        self._account_id = account_id
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_balance"

        account_data = self._get_account_data()
        if account_data:
            name = account_data.get(ATTR_FIRST_NAME, "Unknown")
            self._attr_name = f"{name} Balance"
            self._attr_native_unit_of_measurement = account_data.get(
                ATTR_ACCOUNT_CURRENCY, "USD"
            )

    def _get_account_data(self) -> dict[str, Any] | None:
        """Get account data from coordinator."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("accounts", {}).get(self._account_id)

    @property
    def native_value(self) -> float | None:
        """Return the account balance."""
        account_data = self._get_account_data()
        if not account_data:
            return None
        return account_data.get(ATTR_ACCOUNT_BALANCE)


class FamilySafetyDeviceScreenTimeSensor(CoordinatorEntity, SensorEntity):
    """Sensor for device screen time."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:cellphone-clock"

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        device_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_screentime"

        device_data = self._get_device_data()
        if device_data:
            name = device_data.get(ATTR_DEVICE_NAME, "Unknown Device")
            self._attr_name = f"{name} Screen Time"

    def _get_device_data(self) -> dict[str, Any] | None:
        """Get device data from coordinator."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("devices", {}).get(self._device_id)

    @property
    def native_value(self) -> int | None:
        """Return the screen time in minutes."""
        device_data = self._get_device_data()
        if not device_data:
            return None
        return device_data.get(ATTR_TODAY_TIME_USED, 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        device_data = self._get_device_data()
        if not device_data:
            return {}

        return {
            ATTR_DEVICE_ID: device_data.get(ATTR_DEVICE_ID),
            ATTR_DEVICE_NAME: device_data.get(ATTR_DEVICE_NAME),
        }


class FamilySafetyDeviceInfoSensor(CoordinatorEntity, SensorEntity):
    """Sensor for device information."""

    _attr_icon = "mdi:cellphone-information"

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        device_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_info"

        device_data = self._get_device_data()
        if device_data:
            name = device_data.get(ATTR_DEVICE_NAME, "Unknown Device")
            self._attr_name = f"{name} Info"

    def _get_device_data(self) -> dict[str, Any] | None:
        """Get device data from coordinator."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("devices", {}).get(self._device_id)

    @property
    def native_value(self) -> str | None:
        """Return the device name."""
        device_data = self._get_device_data()
        if not device_data:
            return None
        return device_data.get(ATTR_DEVICE_NAME)

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
