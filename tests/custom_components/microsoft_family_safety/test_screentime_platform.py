from __future__ import annotations

import copy
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser
import voluptuous as vol
import yaml

from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError, Unauthorized

import custom_components.microsoft_family_safety as integration
from custom_components.microsoft_family_safety.api_client import (
    FamilySafetyWebAPI,
    FamilySafetyWebAPIError,
)
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)
from custom_components.microsoft_family_safety.const import (
    CONF_ALLOWED_USERS,
    DAY_KEYS,
    DOMAIN,
)

CHILD = "synthetic-child"
OTHER = "other-child"


def policy(platform, allowance):
    return {
        "appliesTo": platform,
        "enabled": True,
        "dailyRestrictions": {
            day: {
                "allowance": allowance + index * 60000,
                "allottedIntervals": [{"begin": "08:30:00", "end": "19:00:00"}],
            }
            for index, day in enumerate(DAY_KEYS)
        },
    }


class Provider:
    def __init__(self):
        self.state = {
            child: {
                "mode": "PerDeviceType",
                "schedules": [policy("Windows", 3600000), policy("Xbox", 7200000)],
            }
            for child in [CHILD, OTHER]
        }
        self.calls = []
        self.fail = None

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, copy.deepcopy(kwargs)))
        if method == self.fail:
            raise FamilySafetyWebAPIError("synthetic provider failure")
        child = path.rsplit("/", 1)[1]
        if method == "GET":
            return copy.deepcopy(self.state[child])
        assert method == "PATCH"
        body = kwargs["json_data"]
        schedule = next(
            s
            for s in self.state[child]["schedules"]
            if s["appliesTo"] == body["appliesTo"]
        )
        schedule["dailyRestrictions"].update(copy.deepcopy(body["dailyRestrictions"]))
        return {}

    @property
    def writes(self):
        return [c[2]["json_data"] for c in self.calls if c[0] == "PATCH"]


@pytest.fixture
def rig(hass):
    provider = Provider()
    api = FamilySafetyWebAPI(Mock())
    api._request = AsyncMock(side_effect=provider.request)
    api.set_screentime_allowance = AsyncMock(return_value=True)
    api.set_screentime_intervals = AsyncMock(return_value=True)
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "synthetic-token"})
    entry.add_to_hass(hass)
    coordinator = FamilySafetyDataUpdateCoordinator(hass, entry)
    coordinator.web_api = api
    coordinator._has_web_credentials = True
    coordinator._mobile_only = False
    coordinator._addon_client = Mock()
    coordinator._addon_client.set_screentime_allowance = AsyncMock(return_value=True)
    coordinator._addon_client.set_screentime_intervals = AsyncMock(return_value=True)
    coordinator.data = {
        "accounts": {
            CHILD: {
                "screentime_policy": api._convert_mobile_schedule(
                    provider.state[CHILD]["schedules"][0]
                ),
                "xbox_screentime_policy": api._convert_mobile_schedule(
                    provider.state[CHILD]["schedules"][1]
                ),
            },
            OTHER: {
                "screentime_policy": {"sentinel": "other"},
                "xbox_screentime_policy": {"sentinel": "other"},
            },
        }
    }
    coordinator.async_request_refresh = AsyncMock()
    coordinator.async_update_listeners = Mock()
    return provider, api, coordinator


@pytest.fixture
async def service_rig(hass, rig):
    provider, api, coordinator = rig
    hass.data[DOMAIN] = {"synthetic-entry": coordinator}
    integration._register_services(hass)
    yield hass, provider, api, coordinator


async def call(hass, kind, platform="Xbox", day=4, **values):
    data = {"account_id": CHILD, "day_of_week": day}
    if kind == "limit":
        data.update(hours=24, minutes=0)
    else:
        data.update(start_hour=18, start_minute=0, end_hour=20, end_minute=30)
    data.update(values)
    if platform != "__missing__":
        data["platform"] = platform
    await hass.services.async_call(
        DOMAIN, f"set_screentime_{kind}", data, blocking=True
    )


@pytest.mark.parametrize(
    "day,day_key",
    enumerate(
        ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")
    ),
)
async def test_xbox_service_writes_only_selected_xbox_day(service_rig, day, day_key):
    hass, provider, api, coordinator = service_rig
    before = copy.deepcopy(provider.state)
    windows_cache = copy.deepcopy(
        coordinator.data["accounts"][CHILD]["screentime_policy"]
    )
    await call(hass, "limit", day=day)
    await call(hass, "intervals", day=day)
    assert len(provider.writes) == 2
    assert [c[0] for c in provider.calls] == ["GET", "PATCH", "GET", "PATCH"]
    for body in provider.writes:
        assert body["appliesTo"] == "Xbox"
        assert body["mode"] == "PerDeviceType"
        assert list(body["dailyRestrictions"]) == [day_key]
    xbox = provider.state[CHILD]["schedules"][1]
    assert xbox["dailyRestrictions"][day_key] == {
        "allowance": 86400000,
        "allottedIntervals": [{"begin": "18:00:00", "end": "20:30:00"}],
    }
    assert (
        provider.writes[0]["dailyRestrictions"][day_key]["allottedIntervals"]
        == before[CHILD]["schedules"][1]["dailyRestrictions"][day_key][
            "allottedIntervals"
        ]
    )
    assert provider.state[CHILD]["schedules"][0] == before[CHILD]["schedules"][0]
    assert provider.state[OTHER] == before[OTHER]
    for key in DAY_KEYS:
        if key != day_key:
            assert (
                xbox["dailyRestrictions"][key]
                == before[CHILD]["schedules"][1]["dailyRestrictions"][key]
            )
    assert xbox["enabled"] is True
    assert coordinator.data["accounts"][CHILD]["screentime_policy"] == windows_cache
    assert coordinator.data["accounts"][OTHER] == {
        "screentime_policy": {"sentinel": "other"},
        "xbox_screentime_policy": {"sentinel": "other"},
    }
    cached = coordinator.data["accounts"][CHILD]["xbox_screentime_policy"][
        "dailyRestrictions"
    ][day_key]
    assert cached["allowance"] == "24:00:00"
    assert cached["timeline"] == [18 <= i / 2 < 20.5 for i in range(48)]
    api.set_screentime_allowance.assert_not_called()
    api.set_screentime_intervals.assert_not_called()
    coordinator._addon_client.set_screentime_allowance.assert_not_called()
    coordinator._addon_client.set_screentime_intervals.assert_not_called()


@pytest.mark.parametrize("platform", ["Windows", "__missing__"])
@pytest.mark.parametrize("kind", ["limit", "intervals"])
async def test_windows_service_compatibility(service_rig, platform, kind):
    hass, provider, api, coordinator = service_rig
    before = copy.deepcopy(provider.state[CHILD]["schedules"][1])
    await call(hass, kind, platform=platform)
    assert provider.writes[0]["appliesTo"] == "Windows"
    assert provider.state[CHILD]["schedules"][1] == before


@pytest.mark.parametrize(
    "platform", [None, "", "xbox", "windows", "Mobile", "PlayStation", 0, []]
)
@pytest.mark.parametrize("kind", ["limit", "intervals"])
async def test_invalid_platform_rejected_before_network(service_rig, platform, kind):
    hass, provider, api, coordinator = service_rig
    with pytest.raises(vol.Invalid):
        await call(hass, kind, platform=platform)
    assert provider.calls == []


@pytest.mark.parametrize("failure", ["GET", "PATCH"])
@pytest.mark.parametrize("kind", ["limit", "intervals"])
async def test_xbox_failure_never_falls_back_or_updates_success_cache(
    service_rig, failure, kind
):
    hass, provider, api, coordinator = service_rig
    before = copy.deepcopy(coordinator.data)
    provider.fail = failure
    with pytest.raises(HomeAssistantError):
        await call(hass, kind)
    assert coordinator.data == before
    assert all(body["appliesTo"] == "Xbox" for body in provider.writes)
    api.set_screentime_allowance.assert_not_called()
    api.set_screentime_intervals.assert_not_called()
    coordinator._addon_client.set_screentime_allowance.assert_not_called()
    coordinator._addon_client.set_screentime_intervals.assert_not_called()
    coordinator.async_request_refresh.assert_not_called()


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("kind", ["limit", "intervals"])
async def test_windows_existing_fallback_still_works(service_rig, native, kind):
    hass, provider, api, coordinator = service_rig
    provider.fail = "GET"
    coordinator._has_web_credentials = native
    await call(hass, kind, platform="__missing__")
    method = (
        "set_screentime_allowance" if kind == "limit" else "set_screentime_intervals"
    )
    expected = api if native else coordinator._addon_client
    getattr(expected, method).assert_awaited_once()


@pytest.mark.parametrize("kind", ["limit", "intervals"])
@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "duplicate",
        "shared",
        "mode_missing",
        "day_missing",
        "allowance_missing",
        "allowance_bad",
        "intervals_missing",
        "intervals_bad",
    ],
)
async def test_unsafe_xbox_provider_response_stops_without_write(
    service_rig, kind, damage
):
    hass, provider, api, coordinator = service_rig
    payload = provider.state[CHILD]
    xbox = payload["schedules"][1]
    day = xbox["dailyRestrictions"][DAY_KEYS[4]]
    if damage == "missing":
        payload["schedules"].pop()
    elif damage == "duplicate":
        payload["schedules"].append(copy.deepcopy(xbox))
    elif damage == "shared":
        payload["mode"] = "Shared"
    elif damage == "mode_missing":
        payload.pop("mode")
    elif damage == "day_missing":
        xbox["dailyRestrictions"].pop(DAY_KEYS[4])
    elif damage == "allowance_missing":
        day.pop("allowance")
    elif damage == "allowance_bad":
        day["allowance"] = "unknown"
    elif damage == "intervals_missing":
        day.pop("allottedIntervals")
    else:
        day["allottedIntervals"] = "unknown"
    before = copy.deepcopy(coordinator.data)
    with pytest.raises(HomeAssistantError):
        await call(hass, kind)
    assert provider.writes == []
    assert coordinator.data == before
    api.set_screentime_allowance.assert_not_called()
    api.set_screentime_intervals.assert_not_called()


@pytest.mark.parametrize(
    "start,end,expected",
    [
        ((0, 0), (24, 0), [{"begin": "00:00:00", "end": "1.00:00:00"}]),
        ((18, 30), (21, 30), [{"begin": "18:30:00", "end": "21:30:00"}]),
        ((23, 30), (24, 0), [{"begin": "23:30:00", "end": "1.00:00:00"}]),
    ],
)
async def test_xbox_interval_boundaries_preserve_allowance(
    service_rig, start, end, expected
):
    hass, provider, api, coordinator = service_rig
    original_allowance = provider.state[CHILD]["schedules"][1]["dailyRestrictions"][
        DAY_KEYS[4]
    ]["allowance"]
    await call(
        hass,
        "intervals",
        start_hour=start[0],
        start_minute=start[1],
        end_hour=end[0],
        end_minute=end[1],
    )
    assert provider.writes[0]["dailyRestrictions"][DAY_KEYS[4]] == {
        "allowance": original_allowance,
        "allottedIntervals": expected,
    }


@pytest.mark.parametrize("hours,minutes", [(0, 0), (1, 15), (23, 59), (24, 0)])
async def test_xbox_allowance_units(service_rig, hours, minutes):
    hass, provider, api, coordinator = service_rig
    await call(hass, "limit", hours=hours, minutes=minutes)
    assert (
        provider.writes[0]["dailyRestrictions"][DAY_KEYS[4]]["allowance"]
        == (hours * 60 + minutes) * 60000
    )


@pytest.mark.parametrize(
    "kind,values",
    [
        ("limit", {"day_of_week": -1}),
        ("limit", {"day_of_week": 7}),
        ("limit", {"hours": 24, "minutes": 1}),
        ("limit", {"hours": -1}),
        ("intervals", {"start_hour": 21, "end_hour": 20}),
        (
            "intervals",
            {"start_hour": 20, "start_minute": 30, "end_hour": 20, "end_minute": 30},
        ),
        ("intervals", {"end_hour": 24, "end_minute": 30}),
        ("intervals", {"start_minute": 15}),
    ],
)
async def test_invalid_day_limits_and_windows_never_write(service_rig, kind, values):
    hass, provider, api, coordinator = service_rig
    with pytest.raises((vol.Invalid, HomeAssistantError)):
        await call(hass, kind, **values)
    assert provider.calls == []


@pytest.mark.parametrize("kind", ["limit", "intervals"])
async def test_missing_api_is_not_windows_fallback(service_rig, kind):
    hass, provider, api, coordinator = service_rig
    coordinator.web_api = None
    with pytest.raises(HomeAssistantError):
        await call(hass, kind)
    assert provider.calls == []
    coordinator._addon_client.set_screentime_allowance.assert_not_called()
    coordinator._addon_client.set_screentime_intervals.assert_not_called()


async def test_internal_invalid_platform_rejected(rig):
    provider, api, coordinator = rig
    with pytest.raises(ValueError):
        await coordinator.async_set_screentime_limit(CHILD, 4, 1, 0, platform="Mobile")
    with pytest.raises(ValueError):
        await coordinator.async_set_screentime_intervals(
            CHILD, 4, 18, 0, 20, 0, platform="Mobile"
        )
    assert provider.calls == []


def test_service_metadata_exposes_same_platform_contract():
    metadata = yaml.safe_load(
        (Path(integration.__file__).parent / "services.yaml").read_text()
    )
    for name in ["set_screentime_limit", "set_screentime_intervals"]:
        field = metadata[name]["fields"]["platform"]
        assert field["default"] == "Windows"
        assert field["selector"]["select"]["options"] == ["Windows", "Xbox"]


@pytest.mark.parametrize("slots", [[False] * 47, [False] * 49, [0] * 48])
async def test_xbox_api_refuses_invalid_slots_before_read(rig, slots):
    provider, api, coordinator = rig
    with pytest.raises(ValueError):
        await api.set_mobile_xbox_screentime_intervals(CHILD, 4, slots)
    assert provider.calls == []


async def test_xbox_api_preserves_multiple_intervals_and_midnight(rig):
    provider, api, coordinator = rig
    slots = [i in {1, 2, 46, 47} for i in range(48)]
    await api.set_mobile_xbox_screentime_intervals(CHILD, 0, slots)
    assert provider.writes[0]["dailyRestrictions"][DAY_KEYS[0]][
        "allottedIntervals"
    ] == [
        {"begin": "00:30:00", "end": "01:30:00"},
        {"begin": "23:00:00", "end": "1.00:00:00"},
    ]


async def test_xbox_write_uses_fresh_provider_day_not_cached_schedule(service_rig):
    import time

    hass, provider, api, coordinator = service_rig
    stale = copy.deepcopy(provider.state[CHILD])
    api._mobile_schedule_cache[CHILD] = (time.monotonic(), stale)
    provider.state[CHILD]["schedules"][1]["dailyRestrictions"][DAY_KEYS[4]][
        "allowance"
    ] = 13500000
    await call(hass, "intervals")
    assert provider.calls[0][0] == "GET"
    assert provider.writes[0]["dailyRestrictions"][DAY_KEYS[4]]["allowance"] == 13500000


@pytest.mark.parametrize("kind", ["limit", "intervals"])
async def test_xbox_service_respects_allowed_users(service_rig, kind):
    hass, provider, api, coordinator = service_rig
    allowed_user = MockUser().add_to_hass(hass)
    blocked_user = MockUser().add_to_hass(hass)
    blocked_user.mock_policy({"entities": True})
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    hass.config_entries.async_update_entry(
        entry, options={CONF_ALLOWED_USERS: [allowed_user.id]}
    )
    data = {"account_id": CHILD, "day_of_week": 1, "platform": "Xbox"}
    if kind == "limit":
        data.update(hours=1)
    else:
        data.update(start_hour=18, end_hour=20)
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            f"set_screentime_{kind}",
            data,
            blocking=True,
            context=Context(user_id=blocked_user.id),
        )
    assert provider.calls == []
