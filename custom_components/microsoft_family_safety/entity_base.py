"""Pieces shared by the entities of several platforms."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_change

from .const import ATTR_FIRST_NAME, ATTR_SURNAME, DOMAIN


class MinuteUpdateMixin:
    """Re-evaluate every minute: allowed hours and time left move with the clock.

    The tick writes the state only when what the entity shows has changed.
    """

    _last_snapshot: Any = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_change(self.hass, self._async_minute_tick, second=0)
        )

    def _snapshot(self) -> Any:
        value = self.is_on if isinstance(self, BinarySensorEntity) else self.native_value
        return value, tuple(sorted((self.extra_state_attributes or {}).items(), key=str))

    @callback
    def _async_minute_tick(self, _now: datetime) -> None:
        if self._snapshot() != self._last_snapshot:
            self.async_write_ha_state()

    @callback
    def async_write_ha_state(self) -> None:
        self._last_snapshot = self._snapshot()
        super().async_write_ha_state()


class ChildEntityMixin:
    """A child's polled data and the child's device, for coordinator entities."""

    _account_id: Any

    def _account_data(self) -> dict[str, Any]:
        return ((self.coordinator.data or {}).get("accounts") or {}).get(self._account_id) or {}

    def _first_name(self) -> str:
        return self._account_data().get(ATTR_FIRST_NAME, "Unknown")

    @property
    def device_info(self) -> DeviceInfo:
        """Link to the child's device."""
        account = self._account_data()
        full_name = f"{account.get(ATTR_FIRST_NAME, 'Unknown')} {account.get(ATTR_SURNAME, '')}".strip()
        return DeviceInfo(
            identifiers={(DOMAIN, self._account_id)},
            name=f"{full_name} (Family Safety)",
            manufacturer="Microsoft",
            model="Family Safety Account",
        )
