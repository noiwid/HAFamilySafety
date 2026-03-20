"""Number platform for Microsoft Family Safety — daily screen time limits."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTR_FIRST_NAME, ATTR_SURNAME, DOMAIN
from .coordinator import FamilySafetyDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# Mapping: day_of_week int → (API day name, display label FR-friendly)
DAYS = [
    (0, "sunday", "Sunday"),
    (1, "monday", "Monday"),
    (2, "tuesday", "Tuesday"),
    (3, "wednesday", "Wednesday"),
    (4, "thursday", "Thursday"),
    (5, "friday", "Friday"),
    (6, "saturday", "Saturday"),
]


def _parse_allowance_to_minutes(allowance: str | None) -> int:
    """Parse an allowance string like '02:30:00' to total minutes."""
    if not allowance:
        return 0
    try:
        parts = allowance.split(":")
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0
        return hours * 60 + minutes
    except (ValueError, IndexError):
        return 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Microsoft Family Safety number entities."""
    coordinator: FamilySafetyDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[NumberEntity] = []

    if coordinator.data:
        for account_id, account_data in coordinator.data.get("accounts", {}).items():
            for day_index, day_key, day_label in DAYS:
                entities.append(
                    FamilySafetyDailyLimitNumber(
                        coordinator, entry, account_id, day_index, day_key, day_label,
                    )
                )

    async_add_entities(entities)


class FamilySafetyDailyLimitNumber(CoordinatorEntity, NumberEntity):
    """Number entity to view/set daily screen time limit for a specific day."""

    _attr_native_min_value = 0
    _attr_native_max_value = 1440  # 24 hours in minutes
    _attr_native_step = 15
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:clock-edit-outline"

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        account_id: str,
        day_index: int,
        day_key: str,
        day_label: str,
    ) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator)
        self._account_id = account_id
        self._day_index = day_index
        self._day_key = day_key
        self._entry = entry

        account_data = self._get_account_data()
        account_name = account_data.get(ATTR_FIRST_NAME, "Unknown") if account_data else "Unknown"

        self._account_name = account_name
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_limit_{day_key}"
        self._attr_name = f"{account_name} {day_label} Limit"

    def _get_account_data(self) -> dict[str, Any] | None:
        """Get account data from coordinator."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("accounts", {}).get(self._account_id)

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
        )

    @property
    def native_value(self) -> float | None:
        """Return current daily allowance in minutes."""
        account_data = self._get_account_data()
        if not account_data:
            return None
        policy = account_data.get("screentime_policy")
        if not policy or not isinstance(policy, dict):
            return None
        daily = policy.get("dailyRestrictions", policy.get("DailyRestrictions"))
        if not daily or not isinstance(daily, dict):
            return None
        day_data = daily.get(self._day_key, daily.get(self._day_key.capitalize()))
        if not day_data or not isinstance(day_data, dict):
            return None
        allowance = day_data.get("allowance", day_data.get("Allowance"))
        return _parse_allowance_to_minutes(allowance)

    async def async_set_native_value(self, value: float) -> None:
        """Set the daily screen time limit."""
        total_minutes = int(value)
        hours = total_minutes // 60
        minutes = total_minutes % 60
        _LOGGER.info(
            "Setting %s screen time limit for %s to %dh%02dm",
            self._day_key, self._account_name, hours, minutes,
        )
        await self.coordinator.async_set_screentime_limit(
            self._account_id, self._day_index, hours, minutes
        )
