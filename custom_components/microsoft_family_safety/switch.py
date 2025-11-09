"""Switch platform for Microsoft Family Safety."""
from __future__ import annotations

import logging
from typing import Any

from pyfamilysafety.enum import OverrideTarget

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import FamilySafetyDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# Platform mapping
PLATFORM_NAMES = {
    OverrideTarget.WINDOWS: "Windows",
    OverrideTarget.MOBILE: "Mobile",
    OverrideTarget.XBOX: "Xbox",
}

PLATFORM_ICONS = {
    OverrideTarget.WINDOWS: "mdi:microsoft-windows",
    OverrideTarget.MOBILE: "mdi:cellphone",
    OverrideTarget.XBOX: "mdi:microsoft-xbox",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Microsoft Family Safety switches."""
    coordinator: FamilySafetyDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SwitchEntity] = []

    if coordinator.data:
        # Create a switch for each platform per account
        for account_id in coordinator.data.get("accounts", {}):
            platforms = coordinator.get_platforms_for_account(account_id)
            for platform in platforms:
                entities.append(
                    FamilySafetyPlatformSwitch(
                        coordinator,
                        entry,
                        account_id,
                        platform,
                    )
                )

    async_add_entities(entities)


class FamilySafetyPlatformSwitch(CoordinatorEntity, SwitchEntity):
    """Switch for blocking/unblocking a platform (Windows/Mobile/Xbox)."""

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        account_id: str,
        platform: OverrideTarget,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._account_id = account_id
        self._platform = platform
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_{platform.name.lower()}"

        account_data = self._get_account_data()
        if account_data:
            account_name = account_data.get("first_name", "Unknown")
            platform_name = PLATFORM_NAMES.get(platform, platform.name)
            self._attr_name = f"{account_name} {platform_name}"
        else:
            self._attr_name = f"{PLATFORM_NAMES.get(platform, platform.name)}"

    def _get_account_data(self) -> dict[str, Any] | None:
        """Get account data from coordinator."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("accounts", {}).get(self._account_id)

    def _get_platform_devices(self) -> list[dict[str, Any]]:
        """Get all devices for this platform."""
        if not self.coordinator.data:
            return []

        devices = []
        for device_id, device_data in self.coordinator.data.get("devices", {}).items():
            if device_data.get("account_id") == self._account_id:
                device_platform = self.coordinator._get_platform_from_os(
                    device_data.get("os_name", "")
                )
                if device_platform == self._platform:
                    devices.append(device_data)

        return devices

    @property
    def is_on(self) -> bool:
        """Return true if platform is unblocked (switch ON = devices active)."""
        # Check if any device on this platform is NOT blocked
        # If all devices are blocked, switch is OFF
        # If any device is unblocked, switch is ON
        devices = self._get_platform_devices()
        if not devices:
            return False

        # Switch ON means at least one device is UNBLOCKED
        # Switch OFF means all devices are BLOCKED
        return any(not device.get("blocked", False) for device in devices)

    @property
    def icon(self) -> str:
        """Return the icon based on platform and state."""
        base_icon = PLATFORM_ICONS.get(self._platform, "mdi:devices")
        if not self.is_on:
            # Add -lock suffix when blocked
            if self._platform == OverrideTarget.WINDOWS:
                return "mdi:microsoft-windows-classic"
            elif self._platform == OverrideTarget.MOBILE:
                return "mdi:cellphone-lock"
            elif self._platform == OverrideTarget.XBOX:
                return "mdi:gamepad-variant"
        return base_icon

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        devices = self._get_platform_devices()
        account_data = self._get_account_data()

        attributes = {
            "platform": PLATFORM_NAMES.get(self._platform, self._platform.name),
            "account_id": self._account_id,
            "device_count": len(devices),
        }

        if account_data:
            attributes["account_name"] = f"{account_data.get('first_name', '')} {account_data.get('surname', '')}".strip()

        # Add device list
        device_list = []
        for device in devices:
            device_list.append({
                "name": device.get("device_name"),
                "model": device.get("device_model"),
                "os": device.get("os_name"),
                "blocked": device.get("blocked", False),
            })
        attributes["devices"] = device_list

        return attributes

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the switch (unblock the platform)."""
        try:
            await self.coordinator.async_unblock_platform(self._account_id, self._platform)
            _LOGGER.info(
                "Unblocked platform %s for account %s",
                PLATFORM_NAMES.get(self._platform),
                self._account_id
            )
        except Exception as err:
            _LOGGER.error(
                "Failed to unblock platform %s: %s",
                PLATFORM_NAMES.get(self._platform),
                err
            )
            raise

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the switch (block the platform)."""
        try:
            await self.coordinator.async_block_platform(self._account_id, self._platform)
            _LOGGER.info(
                "Blocked platform %s for account %s",
                PLATFORM_NAMES.get(self._platform),
                self._account_id
            )
        except Exception as err:
            _LOGGER.error(
                "Failed to block platform %s: %s",
                PLATFORM_NAMES.get(self._platform),
                err
            )
            raise

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return super().available and self._get_account_data() is not None
