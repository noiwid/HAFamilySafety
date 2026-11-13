"""Each app's limit for today in the Applications sensor."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.util import dt as dt_util

from custom_components.microsoft_family_safety.app_pause import app_limit_today
from custom_components.microsoft_family_safety.const import DAY_KEYS, DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)
from custom_components.microsoft_family_safety.sensor import (
    FamilySafetyApplicationCountSensor,
)

CHILD = "1234567890123456"
TZ = dt_util.get_default_time_zone()
WEDNESDAY = datetime(2026, 11, 4, 16, 10, tzinfo=TZ)
SATURDAY = datetime(2026, 11, 7, 9, 0, tzinfo=TZ)


def _day(minutes=60, start="07:00:00 AM", end="10:00:00 PM", windowed=True):
    return {
        "allowance": minutes * 60000,
        "intervalsEnabled": windowed,
        "intervals": [{"start": start, "end": end}],
    }


def _policy(enabled=True, **days):
    keys = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "weekday", "weekend", "everyday")
    return {
        "appId": "x:racing",
        "blockState": "NotBlocked",
        "enabled": enabled,
        **{key: None for key in keys},
        **days,
    }


@pytest.mark.parametrize(
    ("policy", "now", "expected"),
    [
        # No policy: no limit of its own.
        (None, WEDNESDAY, {"limit_minutes": None}),
        ("junk", WEDNESDAY, {"limit_minutes": None}),
        # A limit on today's day key, allowance in ms, 12-hour window.
        (
            _policy(wednesday=_day(90)),
            WEDNESDAY,
            {"limit_minutes": 90, "limit_start": "07:00", "limit_end": "22:00",
             "limit_enabled": True},
        ),
        # Midnight and noon in 12-hour notation.
        (
            _policy(wednesday=_day(30, "12:00:00 AM", "12:30:00 PM")),
            WEDNESDAY,
            {"limit_minutes": 30, "limit_start": "00:00", "limit_end": "12:30",
             "limit_enabled": True},
        ),
        (
            _policy(wednesday=_day(30, "12:15:00 PM", "11:45:00 PM")),
            WEDNESDAY,
            {"limit_minutes": 30, "limit_start": "12:15", "limit_end": "23:45",
             "limit_enabled": True},
        ),
        # Whole day (no window): minutes only.
        (
            _policy(wednesday=_day(120, "12:00:00 AM", "12:00:00 AM", windowed=False)),
            WEDNESDAY,
            {"limit_minutes": 120, "limit_enabled": True},
        ),
        # Other days limited, today's key null: no limit today.
        (_policy(monday=_day(60)), WEDNESDAY, {"limit_minutes": None, "limit_enabled": True}),
        # Grouped keys: weekday / weekend, then everyday.
        (_policy(weekday=_day(45), weekend=_day(180)), WEDNESDAY,
         {"limit_minutes": 45, "limit_start": "07:00", "limit_end": "22:00",
          "limit_enabled": True}),
        (_policy(weekday=_day(45), weekend=_day(180)), SATURDAY,
         {"limit_minutes": 180, "limit_start": "07:00", "limit_end": "22:00",
          "limit_enabled": True}),
        (_policy(everyday=_day(15, windowed=False)), SATURDAY,
         {"limit_minutes": 15, "limit_enabled": True}),
        # A zero allowance is a limit, not "no limit".
        (_policy(wednesday=_day(0, windowed=False)), WEDNESDAY,
         {"limit_minutes": 0, "limit_enabled": True}),
        # Disabled policy: the stored limit, flagged off.
        (_policy(enabled=False, wednesday=_day(60, windowed=False)), WEDNESDAY,
         {"limit_minutes": 60, "limit_enabled": False}),
        # Unreadable window: minutes kept, no times.
        (_policy(wednesday=_day(60, "7 o'clock", "10:00:00 PM")), WEDNESDAY,
         {"limit_minutes": 60, "limit_enabled": True}),
    ],
)
def test_app_limit_today(policy, now, expected):
    assert app_limit_today(policy, now) == expected


async def test_applications_attribute_carries_the_limits(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.update_interval = None
    today = DAY_KEYS[(dt_util.now().weekday() + 1) % 7]
    apps = [
        SimpleNamespace(app_id="x:racing", name="Racing", blocked=False, icon=None,
                        usage=12.0, policy=_policy(**{today: _day(60)})),
        SimpleNamespace(app_id="appx:paint", name="Paint", blocked=False, icon=None,
                        usage=0, policy=None),
        # pyfamilysafety objects without a policy attribute at all.
        SimpleNamespace(app_id="win32:chat", name="Chat", blocked=True, icon=None, usage=3.0),
    ]
    account = SimpleNamespace(
        user_id=CHILD, first_name="Kid", surname="Example", profile_picture=None,
        today_screentime_usage=0, average_screentime_usage=0, account_balance=0,
        account_currency="", blocked_platforms=[], applications=apps,
    )
    account_id, data = coord._transform_account_data(account)
    coord.data = {"accounts": {account_id: data}, "devices": {}, "pending_requests": []}

    sensor = FamilySafetyApplicationCountSensor(coord, entry, CHILD)
    by_id = {app["app_id"]: app for app in sensor.extra_state_attributes["applications"]}
    assert by_id["x:racing"] == {
        "app_id": "x:racing", "app_name": "Racing", "blocked": False, "icon": None,
        "usage_minutes": 12.0, "limit_minutes": 60, "limit_start": "07:00",
        "limit_end": "22:00", "limit_enabled": True,
    }
    assert by_id["appx:paint"]["limit_minutes"] is None
    assert "limit_start" not in by_id["appx:paint"]
    assert by_id["win32:chat"] == {
        "app_id": "win32:chat", "app_name": "Chat", "blocked": True, "icon": None,
        "usage_minutes": 3.0, "limit_minutes": None,
    }
    # The per-app list stays out of the recorder; the count is the state.
    assert "applications" in sensor._unrecorded_attributes
    assert sensor.native_value == 3


async def test_app_ids_are_encoded_in_the_policy_path():
    """Audit 2026-11-13: a win32 id comes from an executable's name; '#', '?' or '/' must stay in it."""
    from unittest.mock import AsyncMock

    from custom_components.microsoft_family_safety.api_client import FamilySafetyWebAPI

    api = FamilySafetyWebAPI(authenticator=object())
    api._request = AsyncMock(return_value={})
    await api.remove_app_time_limit("123", "win32:game#2/../x?y", "Game", "Windows")
    path = api._request.await_args.args[1]
    assert path == "/v3/AppLimits/policies/123/win32:game%232%2F..%2Fx%3Fy"
