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

    known_accounts: set[str] = set()

    def _add_new_entities() -> None:
        """Add buttons for accounts that appeared since last update."""
        entities: list[ButtonEntity] = []
        data = coordinator.data or {}

        for account_id, account_data in data.get("accounts", {}).items():
            if account_id in known_accounts:
                continue
            known_accounts.add(account_id)
            account_name = account_data.get(ATTR_FIRST_NAME, "Unknown")

            # Create approve/deny buttons per account
            entities.append(
                FamilySafetyRequestButton(
                    coordinator, entry, account_id, account_name, approve=True,
                )
            )
            entities.append(
                FamilySafetyRequestButton(
                    coordinator, entry, account_id, account_name, approve=False,
                )
            )

        if entities:
            async_add_entities(entities)

    _add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))


class FamilySafetyRequestButton(CoordinatorEntity, ButtonEntity):
    """Button to approve or deny the oldest pending request for an account."""

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        account_id: str,
        account_name: str,
        approve: bool,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._account_id = account_id
        self._account_name = account_name
        self._entry = entry
        self._approve = approve
        action = "approve" if approve else "deny"
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_{action}_request"
        self._attr_name = f"{account_name} {action.capitalize()} Request"
        self._attr_icon = "mdi:check-circle" if approve else "mdi:close-circle"

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
        """Return True if there's a pending request to act on."""
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
            if self._approve:
                attrs["requested_time"] = request.get("requestedTime")
        return attrs

    async def async_press(self) -> None:
        """Approve (1 hour extension) or deny the oldest pending request."""
        request = self._get_oldest_request()
        action = "approve" if self._approve else "deny"
        if request is None:
            _LOGGER.warning(
                "No pending request to %s for %s", action, self._account_name
            )
            return
        request_id = request.get("id")
        if self._approve:
            _LOGGER.info(
                "Approving request %s for %s (1h extension)",
                request_id, self._account_name,
            )
            await self.coordinator.async_approve_request(
                request_id, extension_time=3600
            )
        else:
            _LOGGER.info("Denying request %s for %s", request_id, self._account_name)
            await self.coordinator.async_deny_request(request_id)
        await self.coordinator.async_request_refresh()
