"""Screen-time changes for today only, put back at the next local midnight.

Microsoft's schedules are weekly: the daily limit or the allowed hours of a
weekday apply to every such day until changed again, and Microsoft has no
one-day override for them. A today-only change therefore writes today's
weekday and keeps the values it replaced; at the next local midnight (or at
the first poll after it, when Home Assistant was down or the clock skipped
midnight) the replaced values are written back.

Rules:

* Only the fields a call changes are saved, per child, platform and date.
* A second today-only call on the same day keeps the values saved by the
  first one, so midnight returns to the schedule from before the first call.
* A day is put back only while it still holds a value written here. When a
  parent changed it in the meantime (Family Safety app, website, entity or a
  normal service call), their value stays and the saved one is dropped.
* A normal write of a field for the same weekday drops the saved value for
  that field: the new value is meant to last.
* Writes to one child's platform schedule are serialized.

The platform-specific reads and writes live in the coordinator
(``async_read_day`` / ``async_write_day``); which fields a platform supports
comes from ``today_only_fields``, so a new field only needs a reader and a
writer there.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import FamilySafetyDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1

#: Daily allowance, in minutes (int).
FIELD_LIMIT = "limit"
#: Allowed hours, as 48 half-hour slots (list of bool, slot 0 = 00:00-00:30).
FIELD_ALLOWED_HOURS = "allowed_hours"

#: A change that still cannot be put back after this many days is dropped.
MAX_PENDING_DAYS = 7


def today_only_store(hass: HomeAssistant, entry_id: str) -> Store:
    """The Store of an entry's today-only changes (removed with the entry)."""
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.today_only.{entry_id}")


def today_weekday(now: datetime | None = None) -> int:
    """Today's weekday in Microsoft's numbering (0 = Sunday)."""
    return ((now or dt_util.now()).weekday() + 1) % 7


def slots_to_ranges(slots: Iterable[bool]) -> list[dict[str, str]]:
    """48 half-hour slots as [{"start": "HH:MM", "end": "HH:MM"}] (end 24:00 = midnight)."""

    def fmt(slot: int) -> str:
        return f"{slot // 2:02d}:{30 if slot % 2 else 0:02d}"

    ranges: list[dict[str, str]] = []
    start: int | None = None
    for index, allowed in enumerate(list(slots) + [False]):
        if allowed and start is None:
            start = index
        elif not allowed and start is not None:
            ranges.append({"start": fmt(start), "end": fmt(index)})
            start = None
    return ranges


def _display(field: str, value: Any) -> Any:
    return slots_to_ranges(value) if field == FIELD_ALLOWED_HOURS else value


#: Attribute names per field: (value set for today, value put back at midnight).
_ATTRIBUTES = {
    FIELD_LIMIT: ("limit_minutes", "restore_limit_minutes"),
    FIELD_ALLOWED_HOURS: ("allowed_hours", "restore_allowed_hours"),
}


class TodayOnlyChanges:
    """Keeps, persists and puts back today-only schedule changes for one entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: FamilySafetyDataUpdateCoordinator,
        entry_id: str,
    ) -> None:
        self.hass = hass
        self._coordinator = coordinator
        self._store: Store = today_only_store(hass, entry_id)
        #: "child|platform|YYYY-MM-DD" -> {"child_id", "platform", "date",
        #: "day_of_week", "fields": {field: {"saved", "set", "written"}}}
        self._records: dict[str, dict[str, Any]] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._restore_task: asyncio.Task | None = None

    # --- persistence and timers -------------------------------------------
    async def async_load(self) -> None:
        data = await self._store.async_load()
        records = data.get("records") if isinstance(data, dict) else None
        self._records = {
            key: record
            for key, record in (records or {}).items()
            if isinstance(record, dict) and isinstance(record.get("fields"), dict)
        }

    async def _async_save(self) -> None:
        await self._store.async_save({"records": self._records})

    @callback
    def async_start(self) -> Callable[[], None]:
        """Put changes back at every local midnight; returns the unsubscribe."""

        @callback
        def _midnight(_now: datetime) -> None:
            self.async_schedule_restore_due()

        return async_track_time_change(self.hass, _midnight, hour=0, minute=0, second=0)

    @callback
    def async_schedule_restore_due(self, known_children: set[str] | None = None) -> None:
        """Start putting back overdue changes in the background, if any."""
        if not self._due_keys() or (self._restore_task and not self._restore_task.done()):
            return
        self._restore_task = self.hass.async_create_task(
            self.async_restore_due(known_children), f"{DOMAIN} today-only restore"
        )

    def lock(self, child_id: Any, platform: str) -> asyncio.Lock:
        """The lock every write to one child's platform schedule takes."""
        return self._locks.setdefault((str(child_id), platform), asyncio.Lock())

    # --- queries ------------------------------------------------------------
    @staticmethod
    def _key(child_id: str, platform: str, day: date) -> str:
        return f"{child_id}|{platform}|{day.isoformat()}"

    def _due_keys(self) -> list[str]:
        today = dt_util.now().date().isoformat()
        return sorted(
            (k for k, r in self._records.items() if str(r.get("date")) < today),
            key=lambda k: str(self._records[k].get("date")),
        )

    @staticmethod
    def restores_at(day: date) -> datetime:
        """Local midnight at the end of ``day`` (time-zone and DST aware)."""
        return dt_util.start_of_local_day(day + timedelta(days=1))

    def pending(self, child_id: Any) -> dict[str, dict[str, Any]]:
        """Pending changes of a child by platform, for entity attributes."""
        result: dict[str, dict[str, Any]] = {}
        child = str(child_id)
        for record in sorted(self._records.values(), key=lambda r: str(r.get("date"))):
            if record.get("child_id") != child or not record["fields"]:
                continue
            day = date.fromisoformat(record["date"])
            entry: dict[str, Any] = {
                "date": record["date"],
                "restores_at": self.restores_at(day).isoformat(),
            }
            for field, value in record["fields"].items():
                set_name, restore_name = _ATTRIBUTES.get(field, (field, f"restore_{field}"))
                entry[set_name] = _display(field, value.get("set"))
                entry[restore_name] = _display(field, value.get("saved"))
            # The newest record wins when an older one is still waiting.
            result[record["platform"]] = entry
        return result

    # --- changes ------------------------------------------------------------
    async def async_apply(
        self,
        child_id: Any,
        platform: str,
        day_of_week: int | None,
        field: str,
        value: Any,
    ) -> None:
        """Write ``field`` for today only, keeping the value it replaces."""
        child = str(child_id)
        now = dt_util.now()
        weekday = today_weekday(now)
        if day_of_week is not None and day_of_week != weekday:
            raise HomeAssistantError(
                f"today_only changes today (day_of_week {weekday}), not day {day_of_week}; "
                "leave day_of_week out or pass today's"
            )
        if field not in self._coordinator.today_only_fields(platform):
            raise HomeAssistantError(f"{platform} does not support a today-only {field} change")
        key = self._key(child, platform, now.date())
        async with self.lock(child, platform):
            record = self._records.get(key)
            held = (record or {}).get("fields", {}).get(field)
            # Refuse before writing when today's value cannot be read:
            # without it there is nothing to put back at midnight.
            current = (await self._coordinator.async_read_day(child, platform, weekday))[field]
            if held is not None and current != held["saved"] and current not in held["written"]:
                # A parent changed the day since the first today-only call:
                # theirs is the weekly value to come back to.
                _LOGGER.info(
                    "The %s %s of account %s changed since the first today-only "
                    "change; putting back the new value at midnight",
                    platform, field, child,
                )
                held = None
            if held is None:
                if current == value:
                    # Already today's value and nothing is pending: a no-op.
                    if record is not None and field in record["fields"]:
                        del record["fields"][field]
                        if not record["fields"]:
                            del self._records[key]
                        await self._async_save()
                        self._coordinator.async_update_listeners()
                    return
                held = {"saved": current, "written": []}
            if record is None:
                record = self._records[key] = {
                    "child_id": child,
                    "platform": platform,
                    "date": now.date().isoformat(),
                    "day_of_week": weekday,
                    "fields": {},
                }
            if value not in held["written"]:
                held["written"].append(value)
            held["set"] = value
            record["fields"][field] = held
            # Saved before the write: a write that reached Microsoft but whose
            # answer was lost must still be put back.
            await self._async_save()
            await self._coordinator.async_write_day(child, platform, weekday, field, value)
            if value == held["saved"]:
                # Back to the weekly value: nothing left to put back.
                del record["fields"][field]
                if not record["fields"]:
                    del self._records[key]
                await self._async_save()
        self._coordinator.async_update_listeners()

    async def async_forget(
        self, child_id: Any, platform: str, day_of_week: int, field: str
    ) -> None:
        """A normal write replaced ``field`` for that weekday: keep it at midnight.

        Called with the platform lock held.
        """
        child = str(child_id)
        changed = False
        for key, record in list(self._records.items()):
            if (
                record.get("child_id") == child
                and record.get("platform") == platform
                and record.get("day_of_week") == day_of_week
                and field in record["fields"]
            ):
                del record["fields"][field]
                changed = True
                if not record["fields"]:
                    del self._records[key]
        if changed:
            _LOGGER.debug(
                "A %s %s change for day %s replaced a today-only change; it stays",
                platform, field, day_of_week,
            )
            await self._async_save()
            self._coordinator.async_update_listeners()

    async def async_restore(self, child_id: Any, platform: str | None = None) -> None:
        """Put back a child's pending changes now (all platforms, or one)."""
        child = str(child_id)
        keys = [
            key for key, record in self._records.items()
            if record.get("child_id") == child
            and (platform is None or record.get("platform") == platform)
        ]
        failed = [key for key in keys if not await self._async_restore_record(key)]
        if failed:
            raise HomeAssistantError(
                "Could not put back every today-only change; it is retried at the next poll"
            )

    async def async_restore_due(self, known_children: set[str] | None = None) -> None:
        """Put back every change from an earlier day."""
        cutoff = (dt_util.now().date() - timedelta(days=MAX_PENDING_DAYS)).isoformat()
        for key in self._due_keys():
            record = self._records.get(key)
            if record is None:
                continue
            if known_children and record.get("child_id") not in known_children:
                _LOGGER.info(
                    "Dropping a today-only change for account %s: no longer in the family",
                    record.get("child_id"),
                )
                self._records.pop(key, None)
                await self._async_save()
                continue
            if not await self._async_restore_record(key) and str(record.get("date")) < cutoff:
                _LOGGER.warning(
                    "Dropping the today-only %s change of %s for account %s: it could "
                    "not be put back for %d days",
                    record.get("platform"), record.get("date"), record.get("child_id"),
                    MAX_PENDING_DAYS,
                )
                self._records.pop(key, None)
                await self._async_save()
        self._coordinator.async_update_listeners()

    async def _async_restore_record(self, key: str) -> bool:
        """Put back one record. False while something still has to be retried."""
        record = self._records.get(key)
        if record is None:
            return True
        child, platform = record["child_id"], record["platform"]
        day = int(record["day_of_week"])
        wrote = False
        async with self.lock(child, platform):
            if self._records.get(key) is not record:
                return True
            try:
                current = await self._coordinator.async_read_day(child, platform, day)
            except Exception as err:  # noqa: BLE001 - retried at the next poll
                _LOGGER.warning(
                    "Could not read the %s schedule of account %s to put back a "
                    "today-only change; retrying later: %s", platform, child, err,
                )
                return False
            for field, held in list(record["fields"].items()):
                now_value = current.get(field)
                if now_value == held["saved"]:
                    # Already back (or the write never reached Microsoft).
                    del record["fields"][field]
                    continue
                if now_value not in held.get("written", [held.get("set")]):
                    _LOGGER.info(
                        "The %s %s of account %s changed after the today-only change "
                        "of %s; keeping the new value",
                        platform, field, child, record["date"],
                    )
                    del record["fields"][field]
                    continue
                try:
                    await self._coordinator.async_write_day(
                        child, platform, day, field, held["saved"]
                    )
                except Exception as err:  # noqa: BLE001 - retried at the next poll
                    _LOGGER.warning(
                        "Could not put back the %s %s of account %s; retrying "
                        "later: %s", platform, field, child, err,
                    )
                    continue
                wrote = True
                del record["fields"][field]
            if not record["fields"]:
                self._records.pop(key, None)
            await self._async_save()
        if wrote:
            _LOGGER.info(
                "Put back the %s schedule of account %s after a today-only change",
                platform, child,
            )
            await self._coordinator.async_request_refresh()
        self._coordinator.async_update_listeners()
        return key not in self._records
