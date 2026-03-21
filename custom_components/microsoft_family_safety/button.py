"""Button platform for Microsoft Family Safety."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_FIRST_NAME,
    ATTR_REQUEST_ID,
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
    """Set up Microsoft Family Safety buttons."""
    coordinator: FamilySafetyDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[ButtonEntity] = []

    if coordinator.data:
        for account_id, account_data in coordinator.data.get("accounts", {}).items():
            account_name = account_data.get(ATTR_FIRST_NAME, "Unknown")

            # Create approve/deny buttons per account
            entities.append(
                FamilySafetyApproveRequestButton(
                    coordinator, entry, account_id, account_name,
                )
            )
            entities.append(
                FamilySafetyDenyRequestButton(
                    coordinator, entry, account_id, account_name,
                )
            )

    async_add_entities(entities)


class FamilySafetyApproveRequestButton(CoordinatorEntity, ButtonEntity):
    """Button to approve the oldest pending request for an account."""

    _attr_icon = "mdi:check-circle"

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        account_id: str,
        account_name: str,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._account_id = account_id
        self._account_name = account_name
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_approve_request"
        self._attr_name = f"{account_name} Approve Request"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info to link this entity to a child account device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._account_id)},
            name=f"{self._account_name} (Family Safety)",
            manufacturer="Microsoft",
            model="Family Safety Account",
        )

    def _get_oldest_request(self) -> dict | None:
        """Get the oldest pending request for this account."""
        if not self.coordinator.data:
            return None
        all_requests = self.coordinator.data.get("pending_requests", [])
        account_requests = [
            r for r in all_requests if r.get("puid") == self._account_id
        ]
        return account_requests[0] if account_requests else None

    @property
    def available(self) -> bool:
        """Return True if there's a pending request to approve."""
        return self._get_oldest_request() is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        request = self._get_oldest_request()
        attrs: dict[str, Any] = {ATTR_USER_ID: self._account_id}
        if request:
            attrs[ATTR_REQUEST_ID] = request.get("id")
            attrs["request_type"] = request.get("type")
            attrs["platform"] = request.get("platform")
            attrs["requested_time"] = request.get("requestedTime")
        return attrs

    async def async_press(self) -> None:
        """Approve the oldest pending request (1 hour extension)."""
        request = self._get_oldest_request()
        if request is None:
            _LOGGER.warning("No pending request to approve for %s", self._account_name)
            return
        request_id = request.get("id")
        _LOGGER.info(
            "Approving request %s for %s (1h extension)", request_id, self._account_name
        )
        await self.coordinator.async_approve_request(request_id, extension_time=3600)
        await self.coordinator.async_request_refresh()


class FamilySafetyDenyRequestButton(CoordinatorEntity, ButtonEntity):
    """Button to deny the oldest pending request for an account."""

    _attr_icon = "mdi:close-circle"

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        account_id: str,
        account_name: str,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._account_id = account_id
        self._account_name = account_name
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_deny_request"
        self._attr_name = f"{account_name} Deny Request"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info to link this entity to a child account device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._account_id)},
            name=f"{self._account_name} (Family Safety)",
            manufacturer="Microsoft",
            model="Family Safety Account",
        )

    def _get_oldest_request(self) -> dict | None:
        """Get the oldest pending request for this account."""
        if not self.coordinator.data:
            return None
        all_requests = self.coordinator.data.get("pending_requests", [])
        account_requests = [
            r for r in all_requests if r.get("puid") == self._account_id
        ]
        return account_requests[0] if account_requests else None

    @property
    def available(self) -> bool:
        """Return True if there's a pending request to deny."""
        return self._get_oldest_request() is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        request = self._get_oldest_request()
        attrs: dict[str, Any] = {ATTR_USER_ID: self._account_id}
        if request:
            attrs[ATTR_REQUEST_ID] = request.get("id")
            attrs["request_type"] = request.get("type")
            attrs["platform"] = request.get("platform")
        return attrs

    async def async_press(self) -> None:
        """Deny the oldest pending request."""
        request = self._get_oldest_request()
        if request is None:
            _LOGGER.warning("No pending request to deny for %s", self._account_name)
            return
        request_id = request.get("id")
        _LOGGER.info("Denying request %s for %s", request_id, self._account_name)
        await self.coordinator.async_deny_request(request_id)
        await self.coordinator.async_request_refresh()
