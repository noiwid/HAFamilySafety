"""pause_app / resume_app: block for now, put the prior policy back exactly."""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.microsoft_family_safety import _register_services, sensor
from custom_components.microsoft_family_safety.api_client import FamilySafetyWebAPIError
from custom_components.microsoft_family_safety.app_pause import AppPauser, policy_state
from custom_components.microsoft_family_safety.const import DAY_KEYS, DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)

CHILD = "1234567890123456"

LIMIT_DAY = {
    "allowance": 3600000,
    "intervalsEnabled": True,
    "intervals": [{"start": "07:00:00 AM", "end": "10:00:00 PM"}],
}


def _policy(app_id: str, *, limit: bool, block_state: str = "NotBlocked") -> dict:
    """An app policy as the v3/AppLimits/policies resource answers it."""
    return {
        "appId": app_id,
        "displayName": app_id,
        "enabled": limit,
        "blockState": block_state,
        "appTimeEnforcementPolicy": "Custom",
        "alternateAppIds": [],
        **{day: (dict(LIMIT_DAY) if limit else None) for day in DAY_KEYS},
    }


class FakeWebAPI:
    """The mobile API's app policies: a PATCH without day keys clears them."""

    def __init__(self, policies: dict[str, dict]) -> None:
        self.policies = policies
        self.writes: list[tuple[str, str, dict]] = []
        self.read_error: Exception | None = None
        self.write_error: Exception | None = None

    async def get_app_policy(self, child_id, app_id, platform):
        if self.read_error:
            raise self.read_error
        policy = self.policies.get(app_id)
        return copy.deepcopy(policy) if policy is not None else None

    async def write_app_policy(self, child_id, app_id, platform, body):
        if self.write_error:
            raise self.write_error
        self.writes.append((app_id, platform, copy.deepcopy(body)))
        self.policies[app_id] = {**{day: None for day in DAY_KEYS}, **copy.deepcopy(body)}
        return self.policies[app_id]


class FakeAccount:
    def __init__(self, apps: dict[str, SimpleNamespace]) -> None:
        self.apps = apps

    def get_application(self, app_id):
        if app_id not in self.apps:
            raise IndexError(app_id)
        return self.apps[app_id]


@pytest.fixture
def rig(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.update_interval = None
    coord.async_request_refresh = AsyncMock()
    apps = {
        "x:racing": SimpleNamespace(blocked=False, policy=None),  # has a time limit
        "appx:paint": SimpleNamespace(blocked=False, policy=None),  # no limit
        "win32:chat": SimpleNamespace(blocked=True, policy=None),  # blocked by a parent
    }
    api = FakeWebAPI({
        "x:racing": _policy("x:racing", limit=True),
        "appx:paint": _policy("appx:paint", limit=False),
        "win32:chat": _policy("win32:chat", limit=False, block_state="BlockedAlways"),
    })
    coord.web_api = api
    # The roster keys accounts by int; services pass the id as a str.
    coord._accounts = {int(CHILD): FakeAccount(apps)}
    coord.data = {
        "accounts": {CHILD: {"applications": [
            {"app_id": "x:racing", "app_name": "Racing", "blocked": False},
            {"app_id": "appx:paint", "app_name": "Paint", "blocked": False},
        ]}},
        "devices": {},
    }
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
    _register_services(hass)
    return coord, apps, api, entry


async def _call(hass, name, **data):
    return await hass.services.async_call(
        DOMAIN, name, {"account_id": CHILD, **data}, blocking=True, return_response=True
    )


async def test_a_time_limit_is_kept_while_paused_and_put_back(hass, rig):
    coord, apps, api, _ = rig
    before = copy.deepcopy(api.policies["x:racing"])

    paused = await _call(hass, "pause_app", apps=["x:racing", "win32:chat", "x:nope"])
    assert paused["paused"] == ["x:racing"]
    assert paused["skipped"] == {"win32:chat": "already blocked", "x:nope": "unknown app"}
    assert apps["x:racing"].blocked is True
    app_id, platform, body = api.writes[0]
    assert (app_id, platform, body["blockState"]) == ("x:racing", "xbox", "BlockedAlways")
    # The block carries the limit, so Microsoft keeps it stored.
    assert all(body[day] == LIMIT_DAY for day in DAY_KEYS)

    resumed = await _call(hass, "resume_app")
    assert resumed == {
        "account_id": CHILD, "resumed": ["x:racing"], "released": [], "skipped": {},
    }
    assert apps["x:racing"].blocked is False
    assert api.policies["x:racing"] == before
    assert apps["win32:chat"].blocked is True  # never touched
    assert [w[0] for w in api.writes] == ["x:racing", "x:racing"]
    assert coord.app_pauser.saved(CHILD) == {}


async def test_an_app_without_a_limit_comes_back_without_one(hass, rig):
    _, apps, api, _ = rig
    await _call(hass, "pause_app", apps=["appx:paint"])
    await _call(hass, "resume_app", apps=["appx:paint"])
    _, platform, body = api.writes[-1]
    assert platform == "windows"
    assert body["blockState"] == "NotBlocked"
    assert not any(day in body for day in DAY_KEYS)
    assert all(api.policies["appx:paint"][day] is None for day in DAY_KEYS)
    assert apps["appx:paint"].blocked is False


async def test_a_block_microsoft_reports_is_skipped(hass, rig):
    coord, apps, api, _ = rig
    # The activity report still says unblocked, the policy says blocked.
    api.policies["appx:paint"]["blockState"] = "Blocked"
    result = await coord.async_pause_app(CHILD, ["appx:paint"])
    assert result["skipped"] == {"appx:paint": "already blocked"}
    assert api.writes == []
    assert coord.app_pauser.saved(CHILD) == {}


async def test_repeat_pause_keeps_the_first_state_also_after_a_restart(hass, rig):
    coord, apps, api, entry = rig
    await _call(hass, "pause_app", apps=["x:racing"])
    # The policy now reads as blocked; a second pause must not save that.
    await _call(hass, "pause_app", apps=["x:racing"])
    assert len(api.writes) == 2

    restarted = AppPauser(hass, entry.entry_id)
    await restarted.async_load()
    saved = restarted.saved(CHILD)["x:racing"]
    assert saved["blockState"] == "NotBlocked"
    assert saved["days"] == {day: LIMIT_DAY for day in DAY_KEYS}

    coord.app_pauser = restarted
    await _call(hass, "resume_app", apps=["x:racing"])
    assert api.policies["x:racing"] == _policy("x:racing", limit=True)


async def test_resume_of_an_app_never_paused_is_skipped(hass, rig):
    _, _, api, _ = rig
    result = await _call(hass, "resume_app", apps=["appx:paint"])
    assert result["skipped"] == {"appx:paint": "not paused"}
    assert api.writes == []


async def test_a_failed_restore_keeps_the_saved_state(hass, rig):
    coord, _, api, _ = rig
    await _call(hass, "pause_app", apps=["x:racing"])
    api.write_error = FamilySafetyWebAPIError("API request failed with status 500: x")
    result = await _call(hass, "resume_app")
    assert "x:racing" in result["skipped"]
    assert "x:racing" in coord.app_pauser.saved(CHILD)


async def test_a_refused_block_is_forgotten_a_network_failure_is_kept(hass, rig):
    coord, _, api, _ = rig
    api.write_error = FamilySafetyWebAPIError("API request failed with status 400: bad")
    result = await coord.async_pause_app(CHILD, ["x:racing"])
    assert result["paused"] == [] and "x:racing" in result["skipped"]
    assert coord.app_pauser.saved(CHILD) == {}

    # A network failure may have gone through: that one stays resumable.
    api.write_error = asyncio.TimeoutError()
    await coord.async_pause_app(CHILD, ["x:racing"])
    assert "x:racing" in coord.app_pauser.saved(CHILD)


async def test_an_app_a_parent_unblocked_meanwhile_is_left_alone(hass, rig):
    coord, apps, api, entry = rig
    await coord.async_pause_app(CHILD, ["x:racing", "appx:paint"])
    count = sensor.FamilySafetyApplicationCountSensor(coord, entry, CHILD)
    assert count.extra_state_attributes["paused_apps"] == [
        {"app_id": "appx:paint", "app_name": "Paint", "paused": "ours"},
        {"app_id": "x:racing", "app_name": "Racing", "paused": "ours"},
    ]

    apps["x:racing"].blocked = False  # a parent unblocked it in the app
    by_id = {a["app_id"]: a["paused"] for a in count.extra_state_attributes["paused_apps"]}
    assert by_id == {"appx:paint": "ours", "x:racing": "parent"}

    writes = len(api.writes)
    result = await coord.async_resume_app(CHILD)
    assert result["released"] == ["x:racing"]
    assert result["resumed"] == ["appx:paint"]
    assert [w[0] for w in api.writes[writes:]] == ["appx:paint"]
    assert coord.app_pauser.saved(CHILD) == {}
    assert count.extra_state_attributes["paused_apps"] == []


async def test_the_activity_report_policy_is_the_fallback(hass, rig):
    coord, apps, api, _ = rig
    api.read_error = FamilySafetyWebAPIError("API request failed with status 404: x")
    apps["x:racing"].policy = _policy("x:racing", limit=True)
    result = await coord.async_pause_app(CHILD, ["x:racing", "appx:paint"])
    assert result["paused"] == ["x:racing"]
    # Nothing readable: nothing to put back, so no pause.
    assert result["skipped"] == {"appx:paint": "app policy unreadable"}
    assert list(coord.app_pauser.saved(CHILD)) == ["x:racing"]


async def test_only_windows_and_xbox_apps(hass, rig):
    coord, _, api, _ = rig
    result = await coord.async_pause_app(CHILD, ["a:com.example.game"])
    assert result["skipped"] == {"a:com.example.game": "not a Windows or Xbox app"}
    assert api.writes == []


def test_policy_state_refuses_other_shapes():
    assert policy_state(None) is None
    assert policy_state({"appId": "x:a"}) is None
    assert policy_state({"blockState": "NotBlocked", "monday": "01:00:00"}) is None
    state = policy_state(_policy("x:a", limit=False))
    assert state["days"] == {} and state["blockState"] == "NotBlocked"
