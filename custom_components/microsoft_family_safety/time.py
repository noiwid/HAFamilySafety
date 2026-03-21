"""Time platform for Microsoft Family Safety — screen time interval start/end."""
from __future__ import annotations

from datetime import time as dt_time
import logging
from typing import Any

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTR_FIRST_NAME, ATTR_SURNAME, DOMAIN
from .coordinator import FamilySafetyDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

DAYS = [
    (0, "sunday", "Sunday"),
    (1, "monday", "Monday"),
    (2, "tuesday", "Tuesday"),
    (3, "wednesday", "Wednesday"),
    (4, "thursday", "Thursday"),
    (5, "friday", "Friday"),
    (6, "saturday", "Saturday"),
]


def _parse_time(time_str: str | None) -> dt_time | None:
    """Parse a time string like '07:00:00' or '07:00' to datetime.time."""
    if not time_str:
        return None
    try:
        parts = time_str.split(":")
        return dt_time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    except (ValueError, IndexError):
        return None


def _intervals_to_start_end(intervals: list[bool]) -> tuple[dt_time | None, dt_time | None]:
    """Convert 48-boolean interval list to start/end times.

    Each slot = 30 minutes. Slot 0 = 00:00, slot 1 = 00:30, etc.
    Returns the first True slot as start and last True slot + 30min as end.
    """
    if not intervals or len(intervals) != 48:
        return None, None
    first_true = None
    last_true = None
    for i, val in enumerate(intervals):
        if val:
            if first_true is None:
                first_true = i
            last_true = i
    if first_true is None:
        return None, None
    start_h, start_m = divmod(first_true * 30, 60)
    end_minutes = (last_true + 1) * 30
    end_h, end_m = divmod(min(end_minutes, 1440), 60)
    if end_h >= 24:
        end_h, end_m = 23, 59
    return dt_time(start_h, start_m), dt_time(end_h, end_m)


def _extract_day_times(
    policy: dict[str, Any] | None, day_key: str
) -> tuple[dt_time | None, dt_time | None]:
    """Extract start/end time for a day from screentime policy data."""
    if not policy or not isinstance(policy, dict):
        return None, None
    daily = policy.get("dailyRestrictions", policy.get("DailyRestrictions"))
    if not daily or not isinstance(daily, dict):
        return None, None
    day_data = daily.get(day_key, daily.get(day_key.capitalize()))
    if not day_data or not isinstance(day_data, dict):
        return None, None

    # Try format 1: allowedIntervals as list of 48 booleans
    intervals = day_data.get("allowedIntervals", day_data.get("AllowedIntervals"))
    if isinstance(intervals, list) and len(intervals) == 48:
        return _intervals_to_start_end(intervals)

    # Try format 2: intervals as list of {start, end} objects
    interval_list = day_data.get("intervals", day_data.get("Intervals",
                     day_data.get("allottedIntervals", day_data.get("AllottedIntervals"))))
    if isinstance(interval_list, list) and interval_list:
        first = interval_list[0]
        if isinstance(first, dict):
            start_str = first.get("start", first.get("Start"))
            end_str = first.get("end", first.get("End"))
            return _parse_time(start_str), _parse_time(end_str)

    return None, None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Microsoft Family Safety time entities."""
    coordinator: FamilySafetyDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[TimeEntity] = []

    if coordinator.data:
        for account_id in coordinator.data.get("accounts", {}):
            for day_index, day_key, day_label in DAYS:
                entities.append(
                    FamilySafetyIntervalTime(
                        coordinator, entry, account_id,
                        day_index, day_key, day_label, is_start=True,
                    )
                )
                entities.append(
                    FamilySafetyIntervalTime(
                        coordinator, entry, account_id,
                        day_index, day_key, day_label, is_start=False,
                    )
                )

    async_add_entities(entities)


class FamilySafetyIntervalTime(CoordinatorEntity, TimeEntity):
    """Time entity for screen time interval start or end."""

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        account_id: str,
        day_index: int,
        day_key: str,
        day_label: str,
        is_start: bool,
    ) -> None:
        """Initialize the time entity."""
        super().__init__(coordinator)
        self._account_id = account_id
        self._day_index = day_index
        self._day_key = day_key
        self._is_start = is_start
        self._entry = entry

        account_data = self._get_account_data()
        account_name = account_data.get(ATTR_FIRST_NAME, "Unknown") if account_data else "Unknown"
        self._account_name = account_name

        kind = "Start" if is_start else "End"
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_interval_{day_key}_{kind.lower()}"
        self._attr_name = f"{account_name} {day_label} {kind}"
        self._attr_icon = "mdi:clock-start" if is_start else "mdi:clock-end"

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
    def native_value(self) -> dt_time | None:
        """Return current start or end time."""
        account_data = self._get_account_data()
        if not account_data:
            return None
        policy = account_data.get("screentime_policy")
        start, end = _extract_day_times(policy, self._day_key)
        return start if self._is_start else end

    async def async_set_value(self, value: dt_time) -> None:
        """Set the interval start or end time.

        When either start or end is changed, we re-send both to the API.
        The other value is read from the current state.
        """
        account_data = self._get_account_data()
        policy = account_data.get("screentime_policy") if account_data else None
        current_start, current_end = _extract_day_times(policy, self._day_key)

        if self._is_start:
            start = value
            end = current_end or dt_time(22, 0)
        else:
            start = current_start or dt_time(7, 0)
            end = value

        _LOGGER.info(
            "Setting %s interval for %s to %s-%s",
            self._day_key, self._account_name, start, end,
        )
        try:
            await self.coordinator.async_set_screentime_intervals(
                self._account_id,
                self._day_index,
                start.hour,
                start.minute,
                end.hour,
                end.minute,
            )
        except Exception as err:
            _LOGGER.error(
                "Failed to set %s interval for %s: %s",
                self._day_key, self._account_name, err,
            )
            raise HomeAssistantError(
                f"Failed to set screen time interval: {err}"
            ) from err
