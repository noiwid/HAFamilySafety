"""Switch platform for Microsoft Family Safety."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_BLOCKED,
    ATTR_DEVICE_ID,
    ATTR_DEVICE_MODEL,
    ATTR_DEVICE_NAME,
    ATTR_LAST_SEEN,
    ATTR_OS_NAME,
    DOMAIN,
)
from .coordinator import FamilySafetyDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Microsoft Family Safety switches."""
    coordinator: FamilySafetyDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SwitchEntity] = []

    if coordinator.data:
        # Create a switch for each device
        for device_id in coordinator.data.get("devices", {}):
            entities.append(
                FamilySafetyDeviceSwitch(
                    coordinator,
                    entry,
                    device_id,
                )
            )

    async_add_entities(entities)


class FamilySafetyDeviceSwitch(CoordinatorEntity, SwitchEntity):
    """Switch for blocking/unblocking a device."""

    _attr_icon = "mdi:cellphone-lock"

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        device_id: str,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_lock"

        device_data = self._get_device_data()
        if device_data:
            name = device_data.get(ATTR_DEVICE_NAME, "Unknown Device")
            self._attr_name = f"{name}"
        else:
            self._attr_name = f"Device {device_id}"

    def _get_device_data(self) -> dict[str, Any] | None:
        """Get device data from coordinator."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("devices", {}).get(self._device_id)

    @property
    def is_on(self) -> bool:
        """Return true if device is unblocked (switch ON = device active)."""
        device_data = self._get_device_data()
        if not device_data:
            return False

        # Switch ON means device is UNBLOCKED (active)
        # Switch OFF means device is BLOCKED
        blocked = device_data.get(ATTR_BLOCKED, False)
        return not blocked

    @property
    def icon(self) -> str:
        """Return the icon based on state."""
        if self.is_on:
            return "mdi:cellphone-check"
        return "mdi:cellphone-lock"

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
            ATTR_BLOCKED: device_data.get(ATTR_BLOCKED, False),
            "device_make": device_data.get("device_make"),
            "device_class": device_data.get("device_class"),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the switch (unblock the device)."""
        try:
            await self.coordinator.async_unblock_device(self._device_id)
            _LOGGER.info("Unblocked device %s", self._device_id)
        except Exception as err:
            _LOGGER.error("Failed to unblock device %s: %s", self._device_id, err)
            raise

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the switch (block the device)."""
        try:
            await self.coordinator.async_block_device(self._device_id)
            _LOGGER.info("Blocked device %s", self._device_id)
        except Exception as err:
            _LOGGER.error("Failed to block device %s: %s", self._device_id, err)
            raise

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return super().available and self._get_device_data() is not None
