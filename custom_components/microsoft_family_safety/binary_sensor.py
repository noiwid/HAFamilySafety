"""Binary sensor platform for Microsoft Family Safety: allowed now, downtime."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import ATTR_PLATFORM, ATTR_USER_ID, DOMAIN
from .coordinator import FamilySafetyDataUpdateCoordinator
from .entity_base import ChildEntityMixin, MinuteUpdateMixin
from .screen_time import downtime_window

#: Platform -> the account data key holding its schedule.
SCHEDULE_KEYS = {"Windows": "screentime_policy", "Xbox": "xbox_screentime_policy"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Allowed Now and Downtime Active binary sensors.

    One of each per child and platform, once that platform's schedule has
    been read.
    """
    coordinator: FamilySafetyDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[tuple[str, str]] = set()

    def _add_new_entities() -> None:
        entities: list[BinarySensorEntity] = []
        for account_id, account_data in (coordinator.data or {}).get("accounts", {}).items():
            for platform, key in SCHEDULE_KEYS.items():
                if platform == "Xbox" and not coordinator.xbox_features:
                    continue
                if (account_id, platform) in known or not isinstance(
                    (account_data or {}).get(key), dict
                ):
                    continue
                known.add((account_id, platform))
                entities.append(
                    FamilySafetyAllowedNowSensor(coordinator, entry, account_id, platform)
                )
                entities.append(
                    FamilySafetyDowntimeSensor(coordinator, entry, account_id, platform)
                )
        if entities:
            async_add_entities(entities)

    _add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))


class FamilySafetyAllowedNowSensor(
    ChildEntityMixin, MinuteUpdateMixin, CoordinatorEntity, BinarySensorEntity
):
    """On while the child may use the platform right now.

    Off when a lock is in force, outside today's allowed hours, or once today's
    allowance is used up; on with limits turned off or extra time granted.
    Unknown while the schedule or today's usage is unknown. The minutes left
    and used move every minute: they are not recorded.
    """

    _unrecorded_attributes = frozenset({"remaining_minutes", "used_minutes"})

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        account_id: str,
        platform: str,
    ) -> None:
        super().__init__(coordinator)
        self._account_id = account_id
        self._platform = platform
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_{platform.lower()}_allowed_now"
        self._attr_name = f"{self._first_name()} {platform} Allowed Now"
        self._attr_icon = "mdi:microsoft-xbox" if platform == "Xbox" else "mdi:microsoft-windows"

    @property
    def is_on(self) -> bool | None:
        """True while the platform may be used now."""
        return self.coordinator.platform_status(self._account_id, self._platform)["allowed"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Why, and the minutes left."""
        status = self.coordinator.platform_status(self._account_id, self._platform)
        current = status.get("override") or {}
        until = current.get("valid_until")
        return {
            ATTR_USER_ID: self._account_id,
            ATTR_PLATFORM: self._platform,
            "reason": status["reason"],
            "remaining_minutes": status["remaining_minutes"],
            "allowance_minutes": status["allowance_minutes"],
            "used_minutes": status["used_minutes"],
            "in_allowed_hours": status["in_allowed_hours"],
            "override": current.get("type"),
            "override_until": (
                dt_util.as_local(dt_util.parse_datetime(until)).isoformat() if until else None
            ),
        }


class FamilySafetyDowntimeSensor(
    ChildEntityMixin, MinuteUpdateMixin, CoordinatorEntity, BinarySensorEntity
):
    """On while the current local time is outside today's allowed hours.

    ``downtime_start`` / ``downtime_end`` give the current downtime, or the
    next one, as local ISO times. Off with limits turned off; unknown while
    the schedule is unknown. Locks and the daily allowance do not count here:
    see Allowed Now for those.
    """

    _attr_icon = "mdi:sleep"

    def __init__(
        self,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry: ConfigEntry,
        account_id: str,
        platform: str,
    ) -> None:
        super().__init__(coordinator)
        self._account_id = account_id
        self._platform = platform
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{account_id}_{platform.lower()}_downtime"
        self._attr_name = f"{self._first_name()} {platform} Downtime Active"

    def _window(self) -> dict[str, Any]:
        policy = self._account_data().get(SCHEDULE_KEYS[self._platform])
        return downtime_window(policy if isinstance(policy, dict) else None, dt_util.now())

    @property
    def is_on(self) -> bool | None:
        """True during downtime."""
        return self._window()["active"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The current or next downtime."""
        window = self._window()
        return {
            ATTR_USER_ID: self._account_id,
            ATTR_PLATFORM: self._platform,
            "downtime_start": window["start"].isoformat() if window["start"] else None,
            "downtime_end": window["end"].isoformat() if window["end"] else None,
        }
