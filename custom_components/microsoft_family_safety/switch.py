"""Switch platform for Microsoft Family Safety."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_APP_ID,
    ATTR_APP_NAME,
    ATTR_BLOCKED,
    ATTR_DEVICE_NAME,
    ATTR_FIRST_NAME,
    ATTR_PLATFORM,
    ATTR_USER_ID,
    AVAILABLE_PLATFORMS,
    CONF_PLATFORMS,
    DEFAULT_PLATFORMS,
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
        for account_id, account_data in coordinator.data.get("accounts", {}).items():
            account_name = account_data.get(ATTR_FIRST_NAME, "Unknown")

            # Create app block switches for each application
            for app in account_data.get("applications", []):
                entities.append(
                    FamilySafetyAppBlockSwitch(
                        coordinator, entry, account_id, account_name,
                        app["app_id"], app["app_name"],
                    )
                )

            # Create per-platform lock switches only for selected platforms
            enabled_platforms = entry.options.get(CONF_PLATFORMS, DEFAULT_PLATFORMS)
            for platform in enabled_platforms:
                entities.append(
                    FamilySafetyPlatformLockSwitch(
                        coordinator, entry, account_id, account_name, platform,
                    )
                )

            # Create account-wide lock switch (recommended replacement)
            entities.append(
                FamilySafetyAccountLockSwitch(
                    coordinator, entry, account_id, account_name,
                )
            )

    async_add_entities(entities)


class FamilySafetyAppBlockSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to block/unblock an application."""

    _attr_icon = "mdi:application"

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        account_id: str,
        account_name: str,
        app_id: str,
        app_name: str,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._account_id = account_id
        self._account_name = account_name
        self._app_id = app_id
        self._app_name = app_name
        self._entry = entry
        # Sanitize app_id for unique_id (remove special chars)
        safe_app_id = app_id.replace(":", "_").replace(" ", "_")
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_app_{safe_app_id}"
        self._attr_name = f"{account_name} App {app_name}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info to link this entity to a child account device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._account_id)},
            name=f"{self._account_name} (Family Safety)",
            manufacturer="Microsoft",
            model="Family Safety Account",
        )

    def _get_app_data(self) -> dict[str, Any] | None:
        """Get app data from coordinator."""
        if not self.coordinator.data:
            return None
        account = self.coordinator.data.get("accounts", {}).get(self._account_id)
        if not account:
            return None
        for app in account.get("applications", []):
            if app["app_id"] == self._app_id:
                return app
        return None

    @property
    def is_on(self) -> bool | None:
        """Return True if the app is BLOCKED (switch ON = blocked)."""
        app_data = self._get_app_data()
        if app_data is None:
            return None
        return app_data.get("blocked", False)

    @property
    def icon(self) -> str:
        """Return icon based on state."""
        if self.is_on:
            return "mdi:application-off"  # Blocked
        return "mdi:application"  # Allowed

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        app_data = self._get_app_data()
        attrs = {
            ATTR_USER_ID: self._account_id,
            ATTR_APP_ID: self._app_id,
            ATTR_APP_NAME: self._app_name,
        }
        if app_data:
            attrs["usage_minutes"] = app_data.get("usage_minutes", 0)
            attrs["icon_url"] = app_data.get("icon")
        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Block the application."""
        _LOGGER.info("Blocking app %s for account %s", self._app_name, self._account_name)
        await self.coordinator.async_block_app(self._account_id, self._app_id)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Unblock the application."""
        _LOGGER.info("Unblocking app %s for account %s", self._app_name, self._account_name)
        await self.coordinator.async_unblock_app(self._account_id, self._app_id)


class FamilySafetyPlatformLockSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to lock/unlock a platform (Windows/Xbox/Mobile)."""

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        account_id: str,
        account_name: str,
        platform: str,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._account_id = account_id
        self._account_name = account_name
        self._platform = platform
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_platform_{platform.lower()}"
        self._attr_name = f"{account_name} {platform} Lock"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info to link this entity to a child account device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._account_id)},
            name=f"{self._account_name} (Family Safety)",
            manufacturer="Microsoft",
            model="Family Safety Account",
        )

    @property
    def is_on(self) -> bool | None:
        """Return True if the platform is LOCKED (switch ON = locked)."""
        if not self.coordinator.data:
            return None
        account = self.coordinator.data.get("accounts", {}).get(self._account_id)
        if not account:
            return None
        blocked_platforms = account.get("blocked_platforms", [])
        return self._platform in blocked_platforms

    @property
    def icon(self) -> str:
        """Return icon based on platform and state."""
        icons = {
            "Windows": ("mdi:microsoft-windows", "mdi:microsoft-windows"),
            "Xbox": ("mdi:microsoft-xbox", "mdi:microsoft-xbox"),
            "Mobile": ("mdi:cellphone-lock", "mdi:cellphone"),
        }
        locked, unlocked = icons.get(self._platform, ("mdi:lock", "mdi:lock-open"))
        if self.is_on:
            return locked
        return unlocked

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        return {
            ATTR_USER_ID: self._account_id,
            ATTR_PLATFORM: self._platform,
            ATTR_BLOCKED: self.is_on,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Lock the platform (deprecated — use Account Lock switch instead)."""
        _LOGGER.warning(
            "Platform lock switch is deprecated. Use the Account Lock switch instead. "
            "Attempting legacy lock for %s / %s",
            self._platform, self._account_name,
        )
        await self.coordinator.async_lock_platform(self._account_id, self._platform)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Unlock the platform (deprecated — use Account Lock switch instead)."""
        _LOGGER.warning(
            "Platform lock switch is deprecated. Use the Account Lock switch instead. "
            "Attempting legacy unlock for %s / %s",
            self._platform, self._account_name,
        )
        await self.coordinator.async_unlock_platform(self._account_id, self._platform)


class FamilySafetyAccountLockSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to lock/unlock an entire child account via screen time zeroing.

    ON  = account locked (all 7 days screen time set to 0, all intervals blocked)
    OFF = account unlocked (screen time quotas restored to saved values)
    """

    _attr_icon = "mdi:lock"

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        account_id: str,
        account_name: str,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._account_id = account_id
        self._account_name = account_name
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_account_lock"
        self._attr_name = f"{account_name} Lock"
        self._optimistic_state: bool | None = None

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info to link this entity to a child account device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._account_id)},
            name=f"{self._account_name} (Family Safety)",
            manufacturer="Microsoft",
            model="Family Safety Account",
        )

    @property
    def is_on(self) -> bool | None:
        """Return True if the account is locked (all screen time = 0)."""
        if self._optimistic_state is not None:
            return self._optimistic_state
        return self.coordinator.is_account_locked(self._account_id)

    @property
    def icon(self) -> str:
        """Return icon based on state."""
        if self.is_on:
            return "mdi:lock"
        return "mdi:lock-open"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        has_saved = self._account_id in self.coordinator._saved_screentime
        return {
            ATTR_USER_ID: self._account_id,
            "has_saved_policy": has_saved,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Lock the account (optimistic update)."""
        _LOGGER.info("Locking account %s", self._account_name)
        self._optimistic_state = True
        self.async_write_ha_state()
        try:
            await self.coordinator.async_lock_account(self._account_id)
            self._optimistic_state = None
        except Exception:
            self._optimistic_state = None
            self.async_write_ha_state()
            raise

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Unlock the account (optimistic update)."""
        _LOGGER.info("Unlocking account %s", self._account_name)
        self._optimistic_state = False
        self.async_write_ha_state()
        try:
            await self.coordinator.async_unlock_account(self._account_id)
            self._optimistic_state = None
        except Exception:
            self._optimistic_state = None
            self.async_write_ha_state()
            raise
