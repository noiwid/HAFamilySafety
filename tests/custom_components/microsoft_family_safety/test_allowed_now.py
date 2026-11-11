"""Allowed now, time remaining and downtime: pure rules and the entities on them."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.util import dt as dt_util

from custom_components.microsoft_family_safety import binary_sensor, sensor
from custom_components.microsoft_family_safety.const import DAY_KEYS, DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)
from custom_components.microsoft_family_safety.screen_time import (
    downtime_window,
    parse_allowance_minutes,
    platform_allowance,
    xbox_policy_override,
)
from custom_components.microsoft_family_safety.xbox_family_api import schedule_to_policy

CHILD = "1234567890123456"
# A Wednesday, 16:10 local.
NOW = datetime(2026, 11, 4, 16, 10, tzinfo=dt_util.get_default_time_zone())


def _iso(value: datetime) -> str:
    return dt_util.as_utc(value).isoformat().replace("+00:00", "Z")


def _policy(allowance="02:00:00", begin="07:00:00", end="20:00:00", enabled=True):
    return {
        "isEnabled": enabled,
        "dailyRestrictions": {
            key: {"allowance": allowance, "allowedIntervals": [{"begin": begin, "end": end}]}
            for key in DAY_KEYS
        },
    }


def _at(hour, minute=0):
    return NOW.replace(hour=hour, minute=minute)


@pytest.mark.parametrize(
    ("policy", "used", "now", "allowed", "remaining", "reason"),
    [
        # Inside the window, allowance left: the smaller of the two.
        (_policy(), 30, NOW, True, 90, "within_limits"),
        (_policy(), 30, _at(19, 30), True, 30, "within_limits"),
        # Allowance used up, or past it.
        (_policy(), 120, NOW, False, 0, "allowance_used"),
        (_policy(), 150, NOW, False, 0, "allowance_used"),
        # Outside the allowed hours, whatever the allowance.
        (_policy(), 0, _at(6, 59), False, 0, "outside_allowed_hours"),
        (_policy(), 0, _at(20, 0), False, 0, "outside_allowed_hours"),
        # Window ending at midnight in .NET notation, whole-day allowance.
        (_policy("1.00:00:00", "00:00:00", "1.00:00:00"), 600, _at(23, 0), True, 60, "within_limits"),
        # Limits off: allowed until midnight.
        (_policy(enabled=False), 999, _at(22, 0), True, 120, "no_limits"),
        # Unknown usage or schedule.
        (_policy(), None, NOW, None, None, "unknown"),
        (None, 10, NOW, None, None, "unknown"),
        # Zero allowance.
        (_policy("00:00:00"), 0, NOW, False, 0, "allowance_used"),
    ],
)
def test_platform_allowance(policy, used, now, allowed, remaining, reason):
    status = platform_allowance(policy, used, now)
    assert (status["allowed"], status["remaining_minutes"], status["reason"]) == (
        allowed, remaining, reason
    )


def test_allowance_parser_reads_whole_days():
    assert parse_allowance_minutes("1.00:00:00") == 1440
    assert parse_allowance_minutes("02:30:00") == 150
    assert parse_allowance_minutes("PT1H15M") == 75
    assert parse_allowance_minutes("garbage") == 0


def test_an_active_lock_wins_over_everything():
    lock = {"type": "blockUntil", "valid_until": _iso(NOW + timedelta(hours=1))}
    status = platform_allowance(_policy(enabled=False), 0, NOW, lock)
    assert (status["allowed"], status["remaining_minutes"], status["reason"]) == (False, 0, "locked")
    # A lock known only from the blocked platforms (no end) counts too.
    status = platform_allowance(_policy(), 0, NOW, {"type": "blockUntil", "valid_until": None})
    assert status["allowed"] is False


def test_an_expired_lock_is_ignored():
    lock = {"type": "blockUntil", "valid_until": _iso(NOW - timedelta(minutes=1))}
    assert platform_allowance(_policy(), 30, NOW, lock)["reason"] == "within_limits"


def test_extra_time_allows_until_it_ends():
    extra = {"type": "allowUntil", "valid_until": _iso(_at(21, 0) + timedelta(minutes=45))}
    status = platform_allowance(_policy(), 500, _at(21, 0), extra)
    assert (status["allowed"], status["remaining_minutes"], status["reason"]) == (
        True, 45, "extra_time"
    )


def test_xbox_policy_override_shapes():
    until = _iso(NOW + timedelta(hours=1))
    block = {"screenTimeOverride": {"screenTimeOverrideType": "blockUntil", "validUntil": until}}
    assert xbox_policy_override(block)["type"] == "blockUntil"
    assert xbox_policy_override({"data": block})["type"] == "blockUntil"
    extra = {"screenTimeOverride": {"screenTimeOverrideType": "allowUntil", "validUntil": until}}
    assert xbox_policy_override(extra)["type"] == "allowUntil"
    assert xbox_policy_override({"dailyRestrictions": {}}) is None
    assert xbox_policy_override(None) is None


# --- Downtime -----------------------------------------------------------------


def test_downtime_now_spans_the_night():
    # Allowed 07:00-20:00 every day; at 22:00 the downtime runs 20:00 -> 07:00.
    window = downtime_window(_policy(), _at(22, 0))
    assert window["active"] is True
    assert window["start"] == _at(20, 0)
    assert window["end"] == _at(7, 0) + timedelta(days=1)
    # Early morning: it started the evening before.
    window = downtime_window(_policy(), _at(6, 0))
    assert (window["start"], window["end"]) == (_at(20, 0) - timedelta(days=1), _at(7, 0))


def test_next_downtime_during_allowed_hours():
    window = downtime_window(_policy(), NOW)
    assert window["active"] is False
    assert (window["start"], window["end"]) == (_at(20, 0), _at(7, 0) + timedelta(days=1))


def test_no_downtime_with_limits_off_or_unknown():
    assert downtime_window(_policy(enabled=False), NOW) == {"active": False, "start": None, "end": None}
    assert downtime_window(None, NOW)["active"] is None
    # Allowed all day, every day: no downtime ahead.
    assert downtime_window(_policy(begin="00:00", end="24:00"), NOW) == {
        "active": False, "start": None, "end": None
    }


# --- Coordinator and entities -------------------------------------------------


@pytest.fixture
def coordinator(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.update_interval = None
    coord.data = {
        "accounts": {
            CHILD: {
                "first_name": "Alex",
                "devices": ["pc", "box"],
                "blocked_platforms": [],
                "screentime_policy": _policy(),
                "xbox_screentime_policy": _policy("01:00:00"),
                "xbox_today_usage": 20,
            }
        },
        "devices": {
            "pc": {"os_name": "Windows", "today_time_used": 25, "account_id": CHILD},
            "box": {"os_name": "Xbox", "device_class": "Xbox", "today_time_used": 20, "account_id": CHILD},
        },
    }
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
    return coord, entry


async def test_coordinator_feeds_each_platform_its_own_usage(coordinator):
    coord, _ = coordinator
    windows = coord.platform_status(CHILD, "Windows", NOW)
    xbox = coord.platform_status(CHILD, "Xbox", NOW)
    assert (windows["used_minutes"], windows["remaining_minutes"]) == (25, 95)
    assert (xbox["used_minutes"], xbox["remaining_minutes"]) == (20, 40)


async def test_xbox_schedule_from_xbox_family_settings(coordinator):
    """The schedule as the Xbox Family Settings service gives it, once converted."""
    coord, _ = coordinator
    coord.data["accounts"][CHILD]["xbox_screentime_policy"] = schedule_to_policy({
        "isEnabled": True,
        "dailyRestrictions": [
            {
                "dayOfWeek": index,
                "allowance": {"hour": 1, "minute": 30},
                "allowedIntervals": [{
                    "begin": {"days": 0, "hour": 15, "minute": 0},
                    "end": {"days": 1, "hour": 0, "minute": 0},
                }],
            }
            for index in range(7)
        ],
    })
    status = coord.platform_status(CHILD, "Xbox", NOW)
    assert (status["allowed"], status["remaining_minutes"]) == (True, 70)
    status = coord.platform_status(CHILD, "Xbox", _at(14, 0))
    assert status["reason"] == "outside_allowed_hours"


async def test_xbox_lock_in_the_policy_makes_it_zero(coordinator):
    coord, _ = coordinator
    coord.data["accounts"][CHILD]["xbox_policy"] = {
        "screenTimeOverride": {
            "screenTimeOverrideType": "blockUntil",
            "validUntil": _iso(dt_util.utcnow() + timedelta(hours=1)),
        }
    }
    status = coord.platform_status(CHILD, "Xbox")
    assert (status["allowed"], status["remaining_minutes"], status["reason"]) == (False, 0, "locked")


async def test_xbox_extra_time_in_the_policy(coordinator):
    coord, _ = coordinator
    coord.data["accounts"][CHILD]["xbox_today_usage"] = 300
    coord.data["accounts"][CHILD]["xbox_policy"] = {
        "screenTimeOverride": {
            "screenTimeOverrideType": "allowUntil",
            "validUntil": _iso(NOW + timedelta(minutes=30)),
        }
    }
    status = coord.platform_status(CHILD, "Xbox", NOW)
    assert (status["allowed"], status["remaining_minutes"], status["reason"]) == (
        True, 30, "extra_time"
    )


async def test_windows_lock_from_blocked_platforms(coordinator):
    coord, _ = coordinator
    coord.data["accounts"][CHILD]["blocked_platforms"] = ["Windows"]
    status = coord.platform_status(CHILD, "Windows")
    assert (status["allowed"], status["reason"]) == (False, "locked")


async def test_windows_without_a_windows_device_is_unknown(coordinator):
    coord, _ = coordinator
    coord.data["accounts"][CHILD]["devices"] = ["box"]
    assert coord.platform_status(CHILD, "Windows", NOW)["allowed"] is None


async def test_entities_for_both_platforms(hass, coordinator):
    coord, entry = coordinator
    binaries: list = []
    await binary_sensor.async_setup_entry(hass, entry, binaries.extend)
    sensors: list = []
    await sensor.async_setup_entry(hass, entry, sensors.extend)

    assert sorted(e.unique_id.split("_", 2)[-1] for e in binaries) == [
        "windows_allowed_now", "windows_downtime", "xbox_allowed_now", "xbox_downtime"
    ]
    remaining = [e for e in sensors if isinstance(e, sensor.FamilySafetyTimeRemainingSensor)]
    assert sorted(e.unique_id.split("_", 2)[-1] for e in remaining) == [
        "windows_time_remaining", "xbox_time_remaining"
    ]
    xbox_play = [e for e in sensors if isinstance(e, sensor.FamilySafetyXboxScreenTimeSensor)]
    assert len(xbox_play) == 1 and xbox_play[0].native_value == 20

    windows = next(e for e in remaining if e._platform == "Windows")
    attrs = windows.extra_state_attributes
    assert attrs["user_id"] == CHILD
    assert attrs["limits_enabled"] is True
    assert attrs["allowed_ranges"] == [{"begin": "07:00:00", "end": "20:00:00"}]


async def test_mobile_schedule_shape(coordinator):
    """The mobile API's schedule: 48 slots in allowedIntervals, pairs in allottedIntervals."""
    coord, entry = coordinator
    slots = [False] * 14 + [True] * 26 + [False] * 8  # 07:00 - 20:00
    coord.data["accounts"][CHILD]["screentime_policy"] = {
        "isEnabled": True,
        "dailyRestrictions": {
            key: {
                "allowance": "02:00:00",
                "allottedIntervals": [{"begin": "07:00:00", "end": "20:00:00"}],
                "allowedIntervals": slots,
                "timeline": slots,
            }
            for key in DAY_KEYS
        },
    }
    status = coord.platform_status(CHILD, "Windows", NOW)
    assert (status["allowed"], status["remaining_minutes"]) == (True, 95)
    assert coord.platform_status(CHILD, "Windows", _at(20, 30))["reason"] == "outside_allowed_hours"
    remaining = sensor.FamilySafetyTimeRemainingSensor(coord, entry, CHILD)
    assert remaining.extra_state_attributes["allowed_ranges"] == [
        {"begin": "07:00:00", "end": "20:00:00"}
    ]


async def test_no_xbox_entities_without_xbox_data(hass, coordinator):
    coord, entry = coordinator
    account = coord.data["accounts"][CHILD]
    del account["xbox_screentime_policy"]
    account["xbox_today_usage"] = None
    binaries: list = []
    await binary_sensor.async_setup_entry(hass, entry, binaries.extend)
    sensors: list = []
    await sensor.async_setup_entry(hass, entry, sensors.extend)
    assert not [e for e in binaries if e._platform == "Xbox"]
    assert not [
        e for e in sensors
        if isinstance(e, (sensor.FamilySafetyXboxScreenTimeSensor, sensor.FamilySafetyXboxTimeRemainingSensor))
    ]


async def test_downtime_entity_attributes(hass, coordinator):
    coord, entry = coordinator
    entity = binary_sensor.FamilySafetyDowntimeSensor(coord, entry, CHILD, "Windows")
    attrs = entity.extra_state_attributes
    assert attrs["user_id"] == CHILD
    assert attrs["platform"] == "Windows"
    assert dt_util.parse_datetime(attrs["downtime_end"]).tzinfo is not None
    assert entity.is_on in (True, False)


async def test_allowed_now_attributes(hass, coordinator):
    coord, entry = coordinator
    coord.data["accounts"][CHILD]["xbox_policy"] = {
        "screenTimeOverride": {
            "screenTimeOverrideType": "blockUntil",
            "validUntil": _iso(dt_util.utcnow() + timedelta(hours=1)),
        }
    }
    entity = binary_sensor.FamilySafetyAllowedNowSensor(coord, entry, CHILD, "Xbox")
    assert entity.is_on is False
    attrs = entity.extra_state_attributes
    assert (attrs["reason"], attrs["override"]) == ("locked", "blockUntil")
    assert dt_util.parse_datetime(attrs["override_until"]).tzinfo is not None
    assert {"remaining_minutes", "used_minutes"} <= entity._unrecorded_attributes


async def test_minute_tick_writes_only_when_something_changed(hass, coordinator, monkeypatch):
    coord, entry = coordinator
    entity = binary_sensor.FamilySafetyAllowedNowSensor(coord, entry, CHILD, "Windows")
    entity.hass = hass
    entity.entity_id = "binary_sensor.alex_windows_allowed_now"
    writes: list = []
    import homeassistant.helpers.entity as ha_entity

    monkeypatch.setattr(ha_entity.Entity, "async_write_ha_state", lambda self: writes.append(1))
    entity.async_write_ha_state()
    entity._async_minute_tick(dt_util.now())
    entity._async_minute_tick(dt_util.now())
    assert len(writes) == 1
    coord.data["accounts"][CHILD]["blocked_platforms"] = ["Windows"]
    entity._async_minute_tick(dt_util.now())
    assert len(writes) == 2
