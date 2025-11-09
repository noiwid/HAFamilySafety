"""Number entities for Microsoft Family Safety."""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from pyfamilysafety.enum import OverrideTarget

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import FamilySafetyDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Microsoft Family Safety number entities."""
    coordinator: FamilySafetyDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []

    # Create number entities for each account's platforms
    for account_id, account_data in coordinator.data.get("accounts", {}).items():
        platforms = coordinator.get_platforms_for_account(account_id)

        for platform in platforms:
            entities.append(
                FamilySafetyTimeAllowanceNumber(
                    coordinator,
                    account_id,
                    account_data,
                    platform,
                )
            )

    async_add_entities(entities)


class FamilySafetyTimeAllowanceNumber(CoordinatorEntity, NumberEntity):
    """Number entity to control time allowance via overrides."""

    _attr_native_min_value = 0
    _attr_native_max_value = 480  # 8 hours max
    _attr_native_step = 15  # 15 minute increments
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = "min"
    _attr_icon = "mdi:timer-outline"

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        account_id: str,
        account_data: dict[str, Any],
        platform: OverrideTarget,
    ) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator)
        self._account_id = account_id
        self._platform = platform
        self._attr_name = f"{account_data['first_name']} {platform.name.title()} Time Allowance"
        self._attr_unique_id = f"{account_id}_{platform.name.lower()}_time_allowance"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"account_{account_id}")},
            "name": f"{account_data['first_name']} {account_data['surname']}",
            "manufacturer": "Microsoft",
            "model": "Family Safety Account",
        }

    @property
    def native_value(self) -> float | None:
        """Return the current value (minutes of override remaining)."""
        account_data = self._get_account_data()
        if not account_data:
            return 0

        blocked_platforms = account_data.get("blocked_platforms", [])

        # Check if this platform has an active override
        for blocked_platform in blocked_platforms:
            if blocked_platform.target == self._platform:
                # Calculate minutes remaining until valid_until
                if blocked_platform.valid_until:
                    try:
                        # Parse the valid_until datetime
                        if isinstance(blocked_platform.valid_until, datetime):
                            valid_until = blocked_platform.valid_until
                        else:
                            # Assume ISO format string
                            valid_until = datetime.fromisoformat(str(blocked_platform.valid_until).replace('Z', '+00:00'))

                        # Calculate minutes remaining
                        now = datetime.now(valid_until.tzinfo) if valid_until.tzinfo else datetime.now()
                        remaining = valid_until - now
                        minutes = max(0, int(remaining.total_seconds() / 60))

                        return float(minutes)
                    except Exception as err:
                        _LOGGER.warning("Failed to parse valid_until: %s", err)
                        return 0

        # No override = 0 minutes
        return 0

    async def async_set_native_value(self, value: float) -> None:
        """Set the time allowance (creates or cancels override)."""
        minutes = int(value)

        _LOGGER.debug(
            "Setting time allowance for %s %s to %d minutes",
            self._account_id,
            self._platform.name,
            minutes
        )

        try:
            if minutes == 0:
                # Cancel override (block device)
                await self.coordinator.async_unblock_platform(self._account_id, self._platform)
            else:
                # Create override with duration
                await self.coordinator.async_block_platform(
                    self._account_id,
                    self._platform,
                    duration_minutes=minutes
                )

            # Update the coordinator
            await self.coordinator.async_request_refresh()

        except Exception as err:
            _LOGGER.error("Failed to set time allowance: %s", err)
            raise

    def _get_account_data(self) -> dict[str, Any] | None:
        """Get account data from coordinator."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("accounts", {}).get(self._account_id)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success and self._get_account_data() is not None
