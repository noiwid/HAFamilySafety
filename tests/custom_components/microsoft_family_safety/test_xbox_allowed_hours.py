"""Xbox daily limit and allowed hours through the screen-time services.

``set_screentime_limit`` and ``set_screentime_intervals`` take an optional
``platform`` (Windows by default). With Xbox they write the day through the
Xbox Family Settings service (xapprove), the schedule the console enforces,
and never fall back to the Xbox schedule of the Family Safety mobile API.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.exceptions import HomeAssistantError

from custom_components.microsoft_family_safety import _register_services
from custom_components.microsoft_family_safety.const import (
    CONF_WEB_COOKIES,
    CONF_XBOX_FEATURES,
    DOMAIN,
)
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)
from custom_components.microsoft_family_safety.number import (
    FamilySafetyXboxDailyLimitNumber,
)
from custom_components.microsoft_family_safety.sensor import (
    FamilySafetyScreenTimePolicySensor,
)
from custom_components.microsoft_family_safety.xbox_family_api import (
    XboxFamilyAPI,
    XboxFamilyAuthError,
    XboxFamilyError,
    schedule_to_policy,
)

CHILD = "1234567890123456"
XUID = "2535400000000001"


def _day(index: int, minutes: int, begin: int = 7 * 60, end: int = 22 * 60) -> dict:
    def hm(total: int) -> dict:
        return {"days": total // 1440, "hour": total // 60 % 24, "minute": total % 60}

    return {
        "dayOfWeek": index,
        "allowance": {"days": 0, "hour": minutes // 60, "minute": minutes % 60},
        "allowedIntervals": [{"begin": hm(begin), "end": hm(end)}],
    }


SCHEDULE = {
    "isEnabled": True,
    "isGlobal": False,
    "dailyRestrictions": [_day(i, 60 + 15 * i) for i in range(7)],
}


# --- XboxFamilyAPI.async_set_day --------------------------------------------------
@pytest.fixture
def api(hass):
    """An API whose requests are recorded; GET answers the current schedule."""
    api = XboxFamilyAPI(hass, "entry")
    api.schedule = deepcopy(SCHEDULE)
    api.calls = []

    async def _request(method, url, *, json_body=None, **kwargs):
        api.calls.append((method, json_body))
        await asyncio.sleep(0)
        if method == "GET":
            return deepcopy(api.schedule)
        # Apply the PATCH like xapprove does: the sent day replaces the stored one.
        for sent in json_body["dailyRestrictions"]:
            api.schedule["dailyRestrictions"][sent["dayOfWeek"]] = deepcopy(sent)
        api.schedule["isEnabled"] = json_body["isEnabled"]
        return []

    api._request = _request
    return api


def _patch_bodies(api) -> list[dict]:
    return [body for method, body in api.calls if method == "PATCH"]


async def test_intervals_write_keeps_the_allowance(api):
    await api.async_set_day(XUID, 5, intervals=[(16 * 60, 21 * 60 + 30)])
    assert _patch_bodies(api) == [{
        "isEnabled": True,
        "dailyRestrictions": [{
            "dayOfWeek": 5,
            "allowance": {"hour": 2, "minute": 15},
            "allowedIntervals": [{
                "begin": {"days": 0, "hour": 16, "minute": 0},
                "end": {"days": 0, "hour": 21, "minute": 30},
            }],
        }],
    }]


async def test_allowance_write_keeps_the_intervals(api):
    api.schedule["dailyRestrictions"][2] = _day(2, 30, 15 * 60, 19 * 60 + 30)
    await api.async_set_day(XUID, 2, minutes=90)
    (body,) = _patch_bodies(api)
    assert body["dailyRestrictions"][0]["allowance"] == {"hour": 1, "minute": 30}
    assert body["dailyRestrictions"][0]["allowedIntervals"] == [{
        "begin": {"days": 0, "hour": 15, "minute": 0},
        "end": {"days": 0, "hour": 19, "minute": 30},
    }]


async def test_midnight_end_is_hour_24_as_microsoft_writes_it(api):
    await api.async_set_day(XUID, 0, intervals=[(7 * 60, 1440)])
    (body,) = _patch_bodies(api)
    assert body["dailyRestrictions"][0]["allowedIntervals"] == [{
        "begin": {"days": 0, "hour": 7, "minute": 0},
        "end": {"days": 0, "hour": 24, "minute": 0},
    }]
    # And it reads back as 24:00.
    policy = schedule_to_policy(api.schedule)
    assert policy["dailyRestrictions"]["sunday"]["allowedIntervals"] == [
        {"begin": "07:00", "end": "24:00"}
    ]


async def test_a_whole_day_window_is_sent_as_microsoft_reads_it(api):
    await api.async_set_day(XUID, 0, intervals=[(0, 1440)])
    (body,) = _patch_bodies(api)
    assert body["dailyRestrictions"][0]["allowedIntervals"] == [{
        "begin": {"days": 0, "hour": 0, "minute": 0},
        "end": {"days": 0, "hour": 24, "minute": 0},
    }]
    # And it reads back as 24:00.
    policy = schedule_to_policy(api.schedule)
    assert policy["dailyRestrictions"]["sunday"]["allowedIntervals"] == [
        {"begin": "00:00", "end": "24:00"}
    ]


async def test_intervals_write_keeps_a_disabled_schedule_disabled(api):
    api.schedule["isEnabled"] = False
    await api.async_set_day(XUID, 1, intervals=[(8 * 60, 20 * 60)])
    assert _patch_bodies(api)[0]["isEnabled"] is False


@pytest.mark.parametrize("window", [(600, 600), (700, 600), (-30, 600), (600, 1470)])
async def test_invalid_window_is_refused_before_any_request(api, window):
    with pytest.raises(ValueError):
        await api.async_set_day(XUID, 1, intervals=[window])
    assert api.calls == []


async def test_concurrent_day_writes_do_not_clobber_each_other(api):
    # A limit and allowed hours for the same day, sent at once: each write
    # must read the day after the other one wrote it.
    await asyncio.gather(
        api.async_set_day(XUID, 3, minutes=45),
        api.async_set_day(XUID, 3, intervals=[(9 * 60, 12 * 60)]),
    )
    assert [method for method, _ in api.calls] == ["GET", "PATCH", "GET", "PATCH"]
    stored = api.schedule["dailyRestrictions"][3]
    assert stored["allowance"] == {"hour": 0, "minute": 45}
    assert stored["allowedIntervals"] == [{
        "begin": {"days": 0, "hour": 9, "minute": 0},
        "end": {"days": 0, "hour": 12, "minute": 0},
    }]


async def test_missing_day_is_an_error(api):
    api.schedule["dailyRestrictions"] = api.schedule["dailyRestrictions"][:3]
    with pytest.raises(XboxFamilyError):
        await api.async_set_day(XUID, 5, intervals=[(600, 900)])
    assert _patch_bodies(api) == []


# --- coordinator --------------------------------------------------------------
@pytest.fixture
async def coordinator(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"refresh_token": "x", CONF_WEB_COOKIES: [{"name": "MSPAuth"}]},
        options={CONF_XBOX_FEATURES: True},
    )
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.xbox_family = MagicMock(linked=True, ready=True, usable=True)
    coord.xbox_family.async_child_xuid = AsyncMock(return_value=XUID)
    coord.xbox_family.async_set_day = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    coord._accounts = {int(CHILD): SimpleNamespace(user_id=int(CHILD), first_name="Sam", surname="Doe")}
    coord.web_api = MagicMock()
    coord.web_api.set_mobile_xbox_screentime_allowance = AsyncMock()
    coord._set_screentime_allowance = AsyncMock()
    coord._set_screentime_intervals = AsyncMock()
    policy = schedule_to_policy(SCHEDULE)
    coord.data = {"accounts": {CHILD: {"xbox_screentime_policy": policy}}}
    return coord


async def test_xbox_allowed_hours_go_to_xapprove(coordinator):
    await coordinator.async_set_screentime_intervals(CHILD, 5, 16, 0, 21, 30, "Xbox")
    coordinator.xbox_family.async_set_day.assert_awaited_once_with(
        XUID, 5, intervals=[(960, 1290)]
    )
    coordinator._set_screentime_intervals.assert_not_called()
    friday = coordinator.data["accounts"][CHILD]["xbox_screentime_policy"]["dailyRestrictions"]["friday"]
    assert friday["allowedIntervals"] == [{"begin": "16:00", "end": "21:30"}]
    assert friday["allowance"] == "02:15:00"  # untouched
    coordinator.async_request_refresh.assert_awaited()


async def test_xbox_allowed_hours_until_midnight(coordinator):
    await coordinator.async_set_screentime_intervals(CHILD, 0, 18, 0, 24, 0, "Xbox")
    coordinator.xbox_family.async_set_day.assert_awaited_once_with(
        XUID, 0, intervals=[(1080, 1440)]
    )
    sunday = coordinator.data["accounts"][CHILD]["xbox_screentime_policy"]["dailyRestrictions"]["sunday"]
    assert sunday["allowedIntervals"] == [{"begin": "18:00", "end": "24:00"}]


async def test_xbox_limit_goes_to_xapprove(coordinator):
    await coordinator.async_set_screentime_limit(CHILD, 6, 3, 0, "Xbox")
    coordinator.xbox_family.async_set_day.assert_awaited_once_with(XUID, 6, minutes=180)
    coordinator._set_screentime_allowance.assert_not_called()
    coordinator.web_api.set_mobile_xbox_screentime_allowance.assert_not_called()
    saturday = coordinator.data["accounts"][CHILD]["xbox_screentime_policy"]["dailyRestrictions"]["saturday"]
    assert saturday["allowance"] == "03:00:00"


async def test_windows_is_the_default_and_unchanged(coordinator):
    await coordinator.async_set_screentime_limit(CHILD, 1, 2, 0)
    await coordinator.async_set_screentime_intervals(CHILD, 1, 8, 0, 20, 30)
    coordinator._set_screentime_allowance.assert_awaited_once_with(CHILD, 1, 2, 0)
    slots = coordinator._set_screentime_intervals.await_args.args[2]
    assert slots.index(True) == 16 and len(slots) - slots[::-1].index(True) == 41
    coordinator.xbox_family.async_set_day.assert_not_called()


async def test_unknown_platform_is_refused(coordinator):
    with pytest.raises(ValueError):
        await coordinator.async_set_screentime_intervals(CHILD, 1, 8, 0, 20, 0, "Mobile")
    with pytest.raises(ValueError):
        await coordinator.async_set_screentime_limit(CHILD, 1, 2, 0, "Mobile")
    coordinator.xbox_family.async_set_day.assert_not_called()


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ("xbox_off", "tick Xbox"),
        ("not_linked", "not linked"),
        ("refused", "refused"),
        ("lookup_refused", "refused"),
        ("lookup_failed", "Could not look up"),
        ("unmatched", "could not be matched"),
        ("unknown_child", "No child account"),
    ],
)
@pytest.mark.parametrize("write", ["limit", "intervals"])
async def test_xbox_writes_refuse_without_xapprove(coordinator, setup, message, write):
    child = CHILD
    if setup == "not_linked":
        coordinator.xbox_family.linked = False
    elif setup == "refused":
        coordinator.xbox_family.usable = False
    elif setup == "lookup_refused":
        coordinator.xbox_family.async_child_xuid.side_effect = XboxFamilyAuthError("invalid_grant")
    elif setup == "lookup_failed":
        coordinator.xbox_family.async_child_xuid.side_effect = XboxFamilyError("503")
    elif setup == "unmatched":
        coordinator.xbox_family.async_child_xuid.return_value = None
    elif setup == "unknown_child":
        child = "999"

    async def _call():
        if write == "limit":
            await coordinator.async_set_screentime_limit(child, 1, 1, 0, "Xbox")
        else:
            await coordinator.async_set_screentime_intervals(child, 1, 8, 0, 20, 0, "Xbox")

    if setup == "xbox_off":
        with patch.object(
            FamilySafetyDataUpdateCoordinator, "xbox_features", new=property(lambda self: False)
        ), pytest.raises(HomeAssistantError, match=message):
            await _call()
    else:
        with pytest.raises(HomeAssistantError, match=message):
            await _call()
    coordinator.xbox_family.async_set_day.assert_not_called()
    # Never the mobile Xbox schedule, which the console does not enforce.
    coordinator.web_api.set_mobile_xbox_screentime_allowance.assert_not_called()
    coordinator._set_screentime_allowance.assert_not_called()
    coordinator._set_screentime_intervals.assert_not_called()


@pytest.mark.parametrize(
    "args",
    [
        (1, 20, 0, 8, 0),  # end before start
        (1, 8, 0, 8, 0),  # empty window
        (1, 8, 15, 20, 0),  # not on the half hour
        (1, 8, 0, 24, 30),  # past midnight
        (7, 8, 0, 20, 0),  # no such day
    ],
)
async def test_invalid_xbox_allowed_hours_are_refused(coordinator, args):
    with pytest.raises(ValueError):
        await coordinator.async_set_screentime_intervals(CHILD, *args, "Xbox")
    coordinator.xbox_family.async_set_day.assert_not_called()


async def test_xapprove_failure_is_reported_and_cache_unchanged(coordinator):
    coordinator.xbox_family.async_set_day.side_effect = XboxFamilyError("PATCH answered 500")
    with pytest.raises(HomeAssistantError, match="Xbox allowed hours"):
        await coordinator.async_set_screentime_intervals(CHILD, 5, 16, 0, 21, 0, "Xbox")
    friday = coordinator.data["accounts"][CHILD]["xbox_screentime_policy"]["dailyRestrictions"]["friday"]
    assert friday["allowedIntervals"] == [{"begin": "07:00", "end": "22:00"}]


async def test_xbox_limit_entity_still_falls_back_when_not_linked(coordinator):
    # The number entity keeps its behaviour: without xapprove it writes the
    # mobile Xbox schedule as before. Only the services refuse.
    coordinator.xbox_family.linked = False
    await coordinator.async_set_xbox_screentime_limit(CHILD, 1, 1, 0)
    coordinator.web_api.set_mobile_xbox_screentime_allowance.assert_awaited_once_with(CHILD, 1, 1, 0)


# --- services -------------------------------------------------------------------
@pytest.fixture
def service_coordinator(hass):
    coord = SimpleNamespace(
        async_set_screentime_limit=AsyncMock(), async_set_screentime_intervals=AsyncMock(),
        async_unlock_platform=AsyncMock(), async_remove_app_time_limit=AsyncMock(),
    )
    with patch(
        "custom_components.microsoft_family_safety._get_coordinator", return_value=coord
    ):
        _register_services(hass)
        yield coord


async def test_services_default_to_windows(hass, service_coordinator):
    await hass.services.async_call(
        DOMAIN, "set_screentime_limit",
        {"account_id": CHILD, "day_of_week": 1, "hours": 2}, blocking=True,
    )
    await hass.services.async_call(
        DOMAIN, "set_screentime_intervals",
        {"account_id": CHILD, "day_of_week": 1, "start_hour": 8, "end_hour": 20},
        blocking=True,
    )
    service_coordinator.async_set_screentime_limit.assert_awaited_once_with(CHILD, 1, 2, 0, "Windows")
    service_coordinator.async_set_screentime_intervals.assert_awaited_once_with(
        CHILD, 1, 8, 0, 20, 0, "Windows"
    )


async def test_services_pass_xbox(hass, service_coordinator):
    await hass.services.async_call(
        DOMAIN, "set_screentime_intervals",
        {"account_id": CHILD, "platform": "Xbox", "day_of_week": 5,
         "start_hour": 16, "end_hour": 21, "end_minute": 30},
        blocking=True,
    )
    service_coordinator.async_set_screentime_intervals.assert_awaited_once_with(
        CHILD, 5, 16, 0, 21, 30, "Xbox"
    )


@pytest.mark.parametrize("given", ["xbox", "XBOX", " Xbox "])
async def test_services_take_the_platform_in_any_case(hass, service_coordinator, given):
    await hass.services.async_call(
        DOMAIN, "set_screentime_limit",
        {"account_id": CHILD, "platform": given, "day_of_week": 1, "hours": 2},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN, "unlock_platform", {"account_id": CHILD, "platform": given}, blocking=True,
    )
    await hass.services.async_call(
        DOMAIN, "remove_app_time_limit",
        {"account_id": CHILD, "platform": given, "app_id": "a", "app_name": "A"},
        blocking=True,
    )
    # Each service passes on its own canonical spelling.
    service_coordinator.async_set_screentime_limit.assert_awaited_once_with(CHILD, 1, 2, 0, "Xbox")
    service_coordinator.async_unlock_platform.assert_awaited_once_with(CHILD, "Xbox")
    service_coordinator.async_remove_app_time_limit.assert_awaited_once_with(CHILD, "a", "A", "xbox")


async def test_services_reject_other_platforms(hass, service_coordinator):
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN, "set_screentime_limit",
            {"account_id": CHILD, "platform": "Mobile", "day_of_week": 1, "hours": 2},
            blocking=True,
        )
    service_coordinator.async_set_screentime_limit.assert_not_called()


# --- read-only exposure -----------------------------------------------------------
def _entity_data(coordinator, source="xapprove"):
    policy = schedule_to_policy(SCHEDULE)
    policy["source"] = source
    coordinator.data = {"accounts": {CHILD: {"first_name": "Sam", "xbox_screentime_policy": policy}}}


async def test_xbox_allowed_hours_are_shown(coordinator):
    _entity_data(coordinator)
    number = FamilySafetyXboxDailyLimitNumber(
        coordinator, coordinator.entry, CHILD, 5, "friday", "Friday"
    )
    assert number.extra_state_attributes["allowed_intervals"] == [{"begin": "07:00", "end": "22:00"}]
    sensor = FamilySafetyScreenTimePolicySensor(coordinator, coordinator.entry, CHILD)
    attrs = sensor.extra_state_attributes
    assert attrs["xbox_friday_allowed_intervals"] == [{"begin": "07:00", "end": "22:00"}]
    assert "xbox_monday_allowed_intervals" in attrs


async def test_mobile_xbox_schedule_hours_are_not_shown(coordinator):
    # The mobile Xbox schedule is not what the console enforces.
    _entity_data(coordinator, source="mobile")
    number = FamilySafetyXboxDailyLimitNumber(
        coordinator, coordinator.entry, CHILD, 5, "friday", "Friday"
    )
    assert "allowed_intervals" not in number.extra_state_attributes
    sensor = FamilySafetyScreenTimePolicySensor(coordinator, coordinator.entry, CHILD)
    assert not any(k.startswith("xbox_") for k in sensor.extra_state_attributes)
