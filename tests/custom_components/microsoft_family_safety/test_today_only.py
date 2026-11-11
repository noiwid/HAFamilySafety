"""Today-only screen-time changes and their restore at local midnight.

The coordinator runs against a fake mobile schedule API (and a fake Xbox
Family Settings client) that keeps one value per platform, weekday and field,
so each test can check what Microsoft would hold after every step.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

import custom_components.microsoft_family_safety as integration
from custom_components.microsoft_family_safety.const import DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)
from custom_components.microsoft_family_safety.today_only import (
    FIELD_ALLOWED_HOURS,
    FIELD_LIMIT,
    slots_to_ranges,
)

CHILD = "1234567890123456"
OTHER = "6543210987654321"
XUID = "2533274800000001"
WEDNESDAY = 3  # Microsoft numbering, 0 = Sunday
THURSDAY = 4
DAY_SLOTS = [False] * 14 + [True] * 28 + [False] * 6  # 07:00-21:00
LONG_SLOTS = [False] * 14 + [True] * 32 + [False] * 2  # 07:00-23:00


def _local(text: str) -> datetime:
    return dt_util.parse_datetime(text).replace(tzinfo=dt_util.get_default_time_zone())


class FakeSchedules:
    """What Microsoft holds: {(platform, weekday): {"limit", "allowed_hours"}}."""

    def __init__(self) -> None:
        self.days = {
            (platform, day): {FIELD_LIMIT: 120, FIELD_ALLOWED_HOURS: list(DAY_SLOTS)}
            for platform in ("Windows", "Xbox")
            for day in range(7)
        }
        self.writes: list[tuple] = []
        self.fail_reads = False
        self.fail_writes = False

    # mobile API (FamilySafetyWebAPI)
    async def get_mobile_day(self, child_id, day_of_week, platform="Windows"):
        if self.fail_reads:
            raise RuntimeError("mobile API down")
        day = self.days[(platform, day_of_week)]
        return {FIELD_LIMIT: day[FIELD_LIMIT], FIELD_ALLOWED_HOURS: list(day[FIELD_ALLOWED_HOURS])}

    def _write(self, platform, day, field, value):
        if self.fail_writes:
            raise RuntimeError("mobile API down")
        self.writes.append((platform, day, field, value))
        self.days[(platform, day)][field] = value

    async def set_mobile_screentime_allowance(self, child_id, day, hours, minutes):
        self._write("Windows", day, FIELD_LIMIT, hours * 60 + minutes)
        return True

    async def set_mobile_screentime_intervals(self, child_id, day, slots):
        self._write("Windows", day, FIELD_ALLOWED_HOURS, list(slots))
        return True

    async def set_mobile_xbox_screentime_allowance(self, child_id, day, hours, minutes):
        self._write("Xbox", day, FIELD_LIMIT, hours * 60 + minutes)
        return True


class FakeXboxFamily:
    """xapprove: the same Xbox days as the fake mobile payload."""

    def __init__(self, schedules: FakeSchedules) -> None:
        self.schedules = schedules
        self.set_day_calls: list[tuple] = []

    async def async_get_schedule(self, xuid):
        return {
            "isEnabled": True,
            "dailyRestrictions": [
                {
                    "dayOfWeek": day,
                    "allowance": {
                        "hour": self.schedules.days[("Xbox", day)][FIELD_LIMIT] // 60,
                        "minute": self.schedules.days[("Xbox", day)][FIELD_LIMIT] % 60,
                    },
                    "allowedIntervals": [],
                }
                for day in range(7)
            ],
        }

    async def async_set_day(self, xuid, day_of_week, *, minutes=None, enabled=None):
        self.set_day_calls.append((xuid, day_of_week, minutes))
        self.schedules.days[("Xbox", day_of_week)][FIELD_LIMIT] = minutes


def _make_coordinator(hass, entry, schedules: FakeSchedules):
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.web_api = schedules
    coord.async_request_refresh = AsyncMock()
    coord._set_screentime_allowance = AsyncMock(
        side_effect=lambda c, d, h, m: schedules._write("Windows", d, FIELD_LIMIT, h * 60 + m)
    )
    coord._set_screentime_intervals = AsyncMock(
        side_effect=lambda c, d, s: schedules._write("Windows", d, FIELD_ALLOWED_HOURS, list(s))
    )
    coord.data = {"accounts": {CHILD: {}, OTHER: {}}}
    return coord


@pytest.fixture
def schedules():
    return FakeSchedules()


@pytest.fixture
def entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"}, entry_id="entry1")
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
async def coordinator(hass, entry, schedules, freezer):
    freezer.move_to(_local("2026-11-11 10:00:00"))  # a Wednesday
    coord = _make_coordinator(hass, entry, schedules)
    await coord.today_only.async_load()
    unsub = coord.today_only.async_start()
    yield coord
    unsub()


async def _midnight(hass, freezer, text="2026-11-12 00:00:00"):
    now = _local(text)
    freezer.move_to(now)
    async_fire_time_changed(hass, now)
    await hass.async_block_till_done()


async def test_limit_today_only_restored_at_midnight(hass, coordinator, schedules, freezer):
    await coordinator.async_set_screentime_limit(CHILD, None, 3, 0, "Windows", True)

    assert schedules.writes == [("Windows", WEDNESDAY, FIELD_LIMIT, 180)]
    pending = coordinator.today_only.pending(CHILD)
    assert pending == {
        "Windows": {
            "date": "2026-11-11",
            "restores_at": _local("2026-11-12 00:00:00").isoformat(),
            "limit_minutes": 180,
            "restore_limit_minutes": 120,
        }
    }

    await _midnight(hass, freezer)
    assert schedules.days[("Windows", WEDNESDAY)][FIELD_LIMIT] == 120
    assert schedules.writes[-1] == ("Windows", WEDNESDAY, FIELD_LIMIT, 120)
    assert coordinator.today_only.pending(CHILD) == {}


async def test_allowed_hours_today_only_restored(hass, coordinator, schedules, freezer):
    await coordinator.async_set_screentime_intervals(
        CHILD, WEDNESDAY, 7, 0, 23, 0, "Windows", True
    )
    assert schedules.days[("Windows", WEDNESDAY)][FIELD_ALLOWED_HOURS] == LONG_SLOTS
    pending = coordinator.today_only.pending(CHILD)["Windows"]
    assert pending["allowed_hours"] == [{"start": "07:00", "end": "23:00"}]
    assert pending["restore_allowed_hours"] == [{"start": "07:00", "end": "21:00"}]
    # Other days are never touched.
    assert {w[1] for w in schedules.writes} == {WEDNESDAY}

    await _midnight(hass, freezer)
    assert schedules.days[("Windows", WEDNESDAY)][FIELD_ALLOWED_HOURS] == DAY_SLOTS
    assert coordinator.today_only.pending(CHILD) == {}


async def test_second_call_keeps_the_original_value(hass, coordinator, schedules, freezer):
    await coordinator.async_set_screentime_limit(CHILD, None, 3, 0, "Windows", True)
    await coordinator.async_set_screentime_limit(CHILD, None, 4, 30, "Windows", True)
    pending = coordinator.today_only.pending(CHILD)["Windows"]
    assert pending["limit_minutes"] == 270
    assert pending["restore_limit_minutes"] == 120

    await _midnight(hass, freezer)
    assert schedules.days[("Windows", WEDNESDAY)][FIELD_LIMIT] == 120


async def test_back_to_the_weekly_value_drops_the_record(hass, coordinator, schedules):
    await coordinator.async_set_screentime_limit(CHILD, None, 3, 0, "Windows", True)
    await coordinator.async_set_screentime_limit(CHILD, None, 2, 0, "Windows", True)
    assert schedules.days[("Windows", WEDNESDAY)][FIELD_LIMIT] == 120
    assert coordinator.today_only.pending(CHILD) == {}


async def test_same_value_writes_nothing_to_restore(hass, coordinator, schedules):
    await coordinator.async_set_screentime_limit(CHILD, None, 2, 0, "Windows", True)
    assert coordinator.today_only.pending(CHILD) == {}


async def test_parent_change_is_kept(hass, coordinator, schedules, freezer, caplog):
    await coordinator.async_set_screentime_limit(CHILD, None, 3, 0, "Windows", True)
    # A parent sets another value in the Family Safety app.
    schedules.days[("Windows", WEDNESDAY)][FIELD_LIMIT] = 60
    writes = len(schedules.writes)

    await _midnight(hass, freezer)
    assert schedules.days[("Windows", WEDNESDAY)][FIELD_LIMIT] == 60
    assert len(schedules.writes) == writes
    assert coordinator.today_only.pending(CHILD) == {}
    assert "keeping the new value" in caplog.text


async def test_parent_change_between_two_today_only_calls(hass, coordinator, schedules, freezer):
    await coordinator.async_set_screentime_limit(CHILD, None, 3, 0, "Windows", True)
    schedules.days[("Windows", WEDNESDAY)][FIELD_LIMIT] = 60  # parent, in the app
    await coordinator.async_set_screentime_limit(CHILD, None, 4, 0, "Windows", True)
    assert coordinator.today_only.pending(CHILD)["Windows"]["restore_limit_minutes"] == 60

    await _midnight(hass, freezer)
    assert schedules.days[("Windows", WEDNESDAY)][FIELD_LIMIT] == 60


async def test_normal_call_cancels_only_its_field(hass, coordinator, schedules, freezer):
    await coordinator.async_set_screentime_limit(CHILD, None, 3, 0, "Windows", True)
    await coordinator.async_set_screentime_intervals(
        CHILD, None, 7, 0, 23, 0, "Windows", True
    )
    # A normal write of Wednesday's limit is meant to last.
    await coordinator.async_set_screentime_limit(CHILD, WEDNESDAY, 5, 0)
    pending = coordinator.today_only.pending(CHILD)["Windows"]
    assert "limit_minutes" not in pending
    assert pending["allowed_hours"] == [{"start": "07:00", "end": "23:00"}]
    # A normal write of another day cancels nothing.
    await coordinator.async_set_screentime_intervals(CHILD, THURSDAY, 8, 0, 20, 0)
    assert "allowed_hours" in coordinator.today_only.pending(CHILD)["Windows"]

    await _midnight(hass, freezer)
    day = schedules.days[("Windows", WEDNESDAY)]
    assert day[FIELD_LIMIT] == 300
    assert day[FIELD_ALLOWED_HOURS] == DAY_SLOTS


async def test_restart_across_midnight(hass, entry, coordinator, schedules, freezer):
    await coordinator.async_set_screentime_limit(CHILD, None, 3, 0, "Windows", True)

    # Home Assistant is down at midnight and starts the next morning.
    freezer.move_to(_local("2026-11-12 07:30:00"))
    restarted = _make_coordinator(hass, entry, schedules)
    await restarted.today_only.async_load()
    assert restarted.today_only.pending(CHILD)["Windows"]["restore_limit_minutes"] == 120

    # The first poll puts it back.
    restarted.today_only.async_schedule_restore_due({CHILD, OTHER})
    await hass.async_block_till_done()
    assert schedules.days[("Windows", WEDNESDAY)][FIELD_LIMIT] == 120
    assert restarted.today_only.pending(CHILD) == {}


async def test_failed_restore_is_retried(hass, coordinator, schedules, freezer):
    await coordinator.async_set_screentime_limit(CHILD, None, 3, 0, "Windows", True)
    schedules.fail_reads = True
    await _midnight(hass, freezer)
    assert schedules.days[("Windows", WEDNESDAY)][FIELD_LIMIT] == 180
    assert "Windows" in coordinator.today_only.pending(CHILD)

    schedules.fail_reads = False
    coordinator.today_only.async_schedule_restore_due({CHILD})
    await hass.async_block_till_done()
    assert schedules.days[("Windows", WEDNESDAY)][FIELD_LIMIT] == 120


async def test_unreadable_day_refuses_before_writing(hass, coordinator, schedules):
    schedules.fail_reads = True
    with pytest.raises(RuntimeError):
        await coordinator.async_set_screentime_limit(CHILD, None, 3, 0, "Windows", True)
    assert schedules.writes == []
    assert coordinator.today_only.pending(CHILD) == {}


async def test_failed_write_is_still_put_back(hass, coordinator, schedules, freezer):
    """The answer of a write can be lost after Microsoft applied it."""
    schedules.fail_writes = True
    with pytest.raises(RuntimeError):
        await coordinator.async_set_screentime_limit(CHILD, None, 3, 0, "Windows", True)
    schedules.fail_writes = False
    schedules.days[("Windows", WEDNESDAY)][FIELD_LIMIT] = 180  # it did land

    await _midnight(hass, freezer)
    assert schedules.days[("Windows", WEDNESDAY)][FIELD_LIMIT] == 120


async def test_other_day_is_refused(hass, coordinator, schedules):
    with pytest.raises(HomeAssistantError, match="today_only changes today"):
        await coordinator.async_set_screentime_limit(CHILD, THURSDAY, 3, 0, "Windows", True)
    # Today's own number is accepted.
    await coordinator.async_set_screentime_limit(CHILD, WEDNESDAY, 3, 0, "Windows", True)
    assert schedules.writes == [("Windows", WEDNESDAY, FIELD_LIMIT, 180)]


async def test_xbox_limit_through_xbox_family_settings(hass, coordinator, schedules, freezer):
    xbox = FakeXboxFamily(schedules)
    coordinator.xbox_family = xbox
    coordinator._xapprove_xuid_for = AsyncMock(return_value=XUID)

    await coordinator.async_set_screentime_limit(CHILD, None, 1, 30, "Xbox", True)
    assert xbox.set_day_calls == [(XUID, WEDNESDAY, 90)]
    assert schedules.days[("Windows", WEDNESDAY)][FIELD_LIMIT] == 120  # untouched
    pending = coordinator.today_only.pending(CHILD)
    assert list(pending) == ["Xbox"]
    assert pending["Xbox"]["restore_limit_minutes"] == 120

    await _midnight(hass, freezer)
    assert xbox.set_day_calls[-1] == (XUID, WEDNESDAY, 120)
    assert coordinator.today_only.pending(CHILD) == {}


async def test_xbox_limit_through_the_mobile_api(hass, coordinator, schedules, freezer):
    await coordinator.async_set_screentime_limit(CHILD, None, 0, 45, "Xbox", True)
    assert schedules.writes == [("Xbox", WEDNESDAY, FIELD_LIMIT, 45)]

    # A normal Xbox write (the Xbox number entity) for another day keeps it.
    await coordinator.async_set_xbox_screentime_limit(CHILD, THURSDAY, 2, 0)
    assert "Xbox" in coordinator.today_only.pending(CHILD)

    await _midnight(hass, freezer)
    assert schedules.days[("Xbox", WEDNESDAY)][FIELD_LIMIT] == 120


async def test_xbox_number_cancels_the_xbox_restore(hass, coordinator, schedules, freezer):
    await coordinator.async_set_screentime_limit(CHILD, None, 0, 45, "Xbox", True)
    await coordinator.async_set_xbox_screentime_limit(CHILD, WEDNESDAY, 2, 30)
    assert coordinator.today_only.pending(CHILD) == {}
    await _midnight(hass, freezer)
    assert schedules.days[("Xbox", WEDNESDAY)][FIELD_LIMIT] == 150


async def test_xbox_allowed_hours_are_refused(hass, coordinator, schedules):
    for today_only in (True, False):
        with pytest.raises(HomeAssistantError, match="Xbox allowed hours"):
            await coordinator.async_set_screentime_intervals(
                CHILD, WEDNESDAY, 7, 0, 23, 0, "Xbox", today_only
            )
    assert schedules.writes == []


async def test_restore_now(hass, coordinator, schedules):
    await coordinator.async_set_screentime_limit(CHILD, None, 3, 0, "Windows", True)
    await coordinator.async_set_screentime_limit(CHILD, None, 1, 0, "Xbox", True)

    await coordinator.async_restore_today_only(CHILD, "Xbox")
    assert schedules.days[("Xbox", WEDNESDAY)][FIELD_LIMIT] == 120
    assert list(coordinator.today_only.pending(CHILD)) == ["Windows"]

    await coordinator.async_restore_today_only(CHILD)
    assert schedules.days[("Windows", WEDNESDAY)][FIELD_LIMIT] == 120
    assert coordinator.today_only.pending(CHILD) == {}
    # Nothing pending: a no-op.
    await coordinator.async_restore_today_only(CHILD)


async def test_store_pruning(hass, coordinator, schedules, freezer, caplog):
    await coordinator.async_set_screentime_limit(CHILD, None, 3, 0, "Windows", True)
    await coordinator.async_set_screentime_limit(OTHER, None, 3, 0, "Windows", True)

    # The second child left the family; the first cannot be put back.
    schedules.fail_reads = True
    freezer.move_to(_local("2026-11-13 08:00:00"))
    await coordinator.today_only.async_restore_due({CHILD})
    assert coordinator.today_only.pending(OTHER) == {}
    assert "Windows" in coordinator.today_only.pending(CHILD)

    # Still failing more than a week later: dropped.
    freezer.move_to(_local("2026-11-19 08:00:00"))
    await coordinator.today_only.async_restore_due({CHILD})
    assert coordinator.today_only.pending(CHILD) == {}
    assert "could not be put back for 7 days" in caplog.text

    # And the file follows.
    restarted = _make_coordinator(hass, coordinator.entry, schedules)
    await restarted.today_only.async_load()
    assert restarted.today_only.pending(CHILD) == {}


async def test_restores_at_follows_dst(hass, coordinator, freezer):
    await hass.config.async_set_time_zone("Europe/Berlin")
    # Clocks go back on Sunday 2026-10-25 at 03:00.
    freezer.move_to(_local("2026-10-24 12:00:00"))
    await coordinator.async_set_screentime_limit(CHILD, None, 3, 0, "Windows", True)
    assert (
        coordinator.today_only.pending(CHILD)["Windows"]["restores_at"]
        == "2026-10-25T00:00:00+02:00"
    )
    freezer.move_to(_local("2026-10-25 12:00:00"))
    await coordinator.async_restore_today_only(CHILD)
    await coordinator.async_set_screentime_limit(CHILD, None, 3, 0, "Windows", True)
    assert (
        coordinator.today_only.pending(CHILD)["Windows"]["restores_at"]
        == "2026-10-26T00:00:00+01:00"
    )


def test_slots_to_ranges():
    assert slots_to_ranges(DAY_SLOTS) == [{"start": "07:00", "end": "21:00"}]
    assert slots_to_ranges([True] * 48) == [{"start": "00:00", "end": "24:00"}]
    assert slots_to_ranges([False] * 48) == []


# --- services ---------------------------------------------------------------

@pytest.fixture
async def services(hass, coordinator):
    hass.data[DOMAIN] = {"entry1": coordinator}
    integration._register_services(hass)
    return coordinator


async def test_services(hass, services, schedules):
    await hass.services.async_call(
        DOMAIN, "set_screentime_limit",
        {"account_id": CHILD, "hours": 3, "today_only": True}, blocking=True,
    )
    await hass.services.async_call(
        DOMAIN, "set_screentime_intervals",
        {"account_id": CHILD, "start_hour": 7, "end_hour": 23, "today_only": True},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN, "set_screentime_limit",
        {"account_id": CHILD, "platform": "Xbox", "hours": 1, "today_only": True},
        blocking=True,
    )
    pending = services.today_only.pending(CHILD)
    assert pending["Windows"]["limit_minutes"] == 180
    assert pending["Windows"]["allowed_hours"] == [{"start": "07:00", "end": "23:00"}]
    assert pending["Xbox"]["limit_minutes"] == 60

    with pytest.raises(HomeAssistantError, match="day_of_week is required"):
        await hass.services.async_call(
            DOMAIN, "set_screentime_limit", {"account_id": CHILD, "hours": 3}, blocking=True
        )
    with pytest.raises(HomeAssistantError, match="Xbox allowed hours"):
        await hass.services.async_call(
            DOMAIN, "set_screentime_intervals",
            {"account_id": CHILD, "platform": "Xbox", "day_of_week": 1,
             "start_hour": 7, "end_hour": 23},
            blocking=True,
        )

    await hass.services.async_call(
        DOMAIN, "restore_today_only", {"account_id": CHILD, "platform": "Windows"},
        blocking=True,
    )
    assert list(services.today_only.pending(CHILD)) == ["Xbox"]
    assert schedules.days[("Windows", WEDNESDAY)] == {
        FIELD_LIMIT: 120, FIELD_ALLOWED_HOURS: DAY_SLOTS,
    }


async def test_normal_service_call_unchanged(hass, services, schedules):
    """Without today_only the service writes the given weekday, as before."""
    await hass.services.async_call(
        DOMAIN, "set_screentime_limit",
        {"account_id": CHILD, "day_of_week": 1, "hours": 2, "minutes": 30}, blocking=True,
    )
    services._set_screentime_allowance.assert_awaited_once_with(CHILD, 1, 2, 30)
    assert services.today_only.pending(CHILD) == {}
