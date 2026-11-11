"""pause_app / resume_app: block for now, put the prior policy back exactly."""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.exceptions import HomeAssistantError

from custom_components.microsoft_family_safety import _register_services, sensor
from custom_components.microsoft_family_safety.api_client import FamilySafetyWebAPIError
from custom_components.microsoft_family_safety.app_pause import (
    AppPauser,
    _refused,
    policy_state,
)
from custom_components.microsoft_family_safety.const import DAY_KEYS, DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)

CHILD = "1234567890123456"
OTHER_CHILD = "6543210987654321"

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


def _error(status: int) -> FamilySafetyWebAPIError:
    return FamilySafetyWebAPIError(f"API request failed with status {status}: x", status=status)


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
        return copy.deepcopy(self.policies[app_id])


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
    assert paused["failed"] == {}
    assert apps["x:racing"].blocked is True
    app_id, platform, body = api.writes[0]
    assert (app_id, platform, body["blockState"]) == ("x:racing", "xbox", "BlockedAlways")
    # The block carries the limit, so Microsoft keeps it stored.
    assert all(body[day] == LIMIT_DAY for day in DAY_KEYS)

    resumed = await _call(hass, "resume_app")
    assert resumed == {
        "account_id": CHILD, "resumed": ["x:racing"], "released": [],
        "changed_by_parent": [], "skipped": {}, "failed": {},
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
    # Still blocked by us: a second pause writes nothing and saves nothing new.
    again = await _call(hass, "pause_app", apps=["x:racing"])
    assert again["paused"] == ["x:racing"]
    assert len(api.writes) == 1

    restarted = AppPauser(hass, entry.entry_id)
    await restarted.async_load()
    saved = restarted.saved(CHILD)["x:racing"]
    assert saved["prior"]["blockState"] == "NotBlocked"
    assert saved["prior"]["days"] == {day: LIMIT_DAY for day in DAY_KEYS}

    coord.app_pauser = restarted
    await _call(hass, "resume_app", apps=["x:racing"])
    assert api.policies["x:racing"] == _policy("x:racing", limit=True)


async def test_a_repeat_pause_after_a_release_blocks_again(hass, rig):
    coord, apps, api, _ = rig
    await coord.async_pause_app(CHILD, ["x:racing"])
    api.policies["x:racing"]["blockState"] = "NotBlocked"  # released in the app
    result = await coord.async_pause_app(CHILD, ["x:racing"])
    assert result["paused"] == ["x:racing"]
    assert api.policies["x:racing"]["blockState"] == "BlockedAlways"
    assert coord.app_pauser.saved(CHILD)["x:racing"]["prior"]["blockState"] == "NotBlocked"


async def test_a_repeat_pause_after_a_parent_change_leaves_it(hass, rig):
    coord, apps, api, _ = rig
    await coord.async_pause_app(CHILD, ["x:racing"])
    api.policies["x:racing"]["monday"] = {**LIMIT_DAY, "allowance": 7200000}
    result = await coord.async_pause_app(CHILD, ["x:racing"])
    assert result["changed_by_parent"] == ["x:racing"] and result["paused"] == []
    assert len(api.writes) == 1
    assert coord.app_pauser.saved(CHILD) == {}


async def test_resume_of_an_app_never_paused_is_skipped(hass, rig):
    _, _, api, _ = rig
    result = await _call(hass, "resume_app", apps=["appx:paint"])
    assert result["skipped"] == {"appx:paint": "not paused"}
    assert api.writes == []


async def test_a_failed_restore_keeps_the_saved_state_and_fails_the_call(hass, rig):
    coord, _, api, _ = rig
    await _call(hass, "pause_app", apps=["x:racing"])
    api.write_error = _error(500)
    with pytest.raises(HomeAssistantError, match="x:racing"):
        await _call(hass, "resume_app")
    assert "x:racing" in coord.app_pauser.saved(CHILD)


async def test_a_partial_failure_answers_per_app(hass, rig):
    coord, _, api, _ = rig
    await coord.async_pause_app(CHILD, ["x:racing", "appx:paint"])
    del api.policies["appx:paint"]  # no longer readable
    result = await _call(hass, "resume_app")
    assert result["resumed"] == ["x:racing"]
    assert result["failed"] == {"appx:paint": "policy_unreadable"}
    assert list(coord.app_pauser.saved(CHILD)) == ["appx:paint"]


async def test_a_refused_block_is_forgotten_a_network_failure_is_kept(hass, rig):
    coord, _, api, _ = rig
    api.write_error = _error(400)
    with pytest.raises(HomeAssistantError):
        await coord.async_pause_app(CHILD, ["x:racing"])
    assert coord.app_pauser.saved(CHILD) == {}

    # A network failure may have gone through: that one stays resumable.
    api.write_error = asyncio.TimeoutError()
    with pytest.raises(HomeAssistantError):
        await coord.async_pause_app(CHILD, ["x:racing"])
    assert "x:racing" in coord.app_pauser.saved(CHILD)

    # It did go through: resume still finds our block and puts the limit back.
    api.write_error = None
    api.policies["x:racing"]["blockState"] = "BlockedAlways"
    result = await coord.async_resume_app(CHILD)
    assert result["resumed"] == ["x:racing"]
    assert api.policies["x:racing"] == _policy("x:racing", limit=True)


def test_refused_reads_the_http_status():
    assert _refused(_error(400)) and _refused(_error(404))
    assert not _refused(_error(500))
    assert not _refused(FamilySafetyWebAPIError("Mobile API request failed: status 400"))
    assert not _refused(asyncio.TimeoutError())


async def test_an_app_a_parent_unblocked_meanwhile_is_left_alone(hass, rig):
    coord, apps, api, entry = rig
    await coord.async_pause_app(CHILD, ["x:racing", "appx:paint"])
    count = sensor.FamilySafetyApplicationCountSensor(coord, entry, CHILD)
    assert count.extra_state_attributes["paused_apps"] == [
        {"app_id": "appx:paint", "app_name": "Paint", "paused": "ours"},
        {"app_id": "x:racing", "app_name": "Racing", "paused": "ours"},
    ]

    # A parent unblocked it in the app.
    apps["x:racing"].blocked = False
    api.policies["x:racing"]["blockState"] = "NotBlocked"
    by_id = {a["app_id"]: a["paused"] for a in count.extra_state_attributes["paused_apps"]}
    assert by_id == {"appx:paint": "ours", "x:racing": "parent"}

    writes = len(api.writes)
    result = await coord.async_resume_app(CHILD)
    assert result["released"] == ["x:racing"]
    assert result["resumed"] == ["appx:paint"]
    assert [w[0] for w in api.writes[writes:]] == ["appx:paint"]
    assert coord.app_pauser.saved(CHILD) == {}
    assert count.extra_state_attributes["paused_apps"] == []


async def test_a_lagging_report_does_not_count_as_a_release(hass, rig):
    coord, apps, api, _ = rig
    await coord.async_pause_app(CHILD, ["x:racing"])
    apps["x:racing"].blocked = False  # the polled report lags; Microsoft still blocks
    result = await coord.async_resume_app(CHILD)
    assert result["resumed"] == ["x:racing"] and result["released"] == []
    assert api.policies["x:racing"] == _policy("x:racing", limit=True)


async def test_a_limit_a_parent_changed_while_paused_is_kept(hass, rig):
    coord, apps, api, _ = rig
    await coord.async_pause_app(CHILD, ["x:racing"])
    api.policies["x:racing"]["saturday"] = {**LIMIT_DAY, "allowance": 7200000}
    parents = copy.deepcopy(api.policies["x:racing"])
    result = await coord.async_resume_app(CHILD)
    assert result["changed_by_parent"] == ["x:racing"] and result["resumed"] == []
    assert api.policies["x:racing"] == parents  # nothing written over it
    assert coord.app_pauser.saved(CHILD) == {}


async def test_an_unreadable_live_policy_on_resume_keeps_the_record(hass, rig):
    coord, _, api, _ = rig
    await coord.async_pause_app(CHILD, ["x:racing"])
    api.read_error = _error(503)
    with pytest.raises(HomeAssistantError, match="policy_unreadable"):
        await coord.async_resume_app(CHILD)
    assert "x:racing" in coord.app_pauser.saved(CHILD)


async def test_no_pause_without_a_readable_policy(hass, rig):
    coord, apps, api, _ = rig
    api.read_error = _error(404)
    # The activity report's policy is not used: its shape is unknown.
    apps["x:racing"].policy = _policy("x:racing", limit=True)
    with pytest.raises(HomeAssistantError, match="policy_unreadable"):
        await _call(hass, "pause_app", apps=["x:racing"])
    assert api.writes == []
    assert coord.app_pauser.saved(CHILD) == {}


async def test_no_pause_when_a_limit_may_sit_outside_the_day_keys(hass, rig):
    coord, _, api, _ = rig
    # enabled with no day keys: a limit in some other shape.
    api.policies["x:racing"] = {"appId": "x:racing", "enabled": True, "blockState": "NotBlocked"}
    # A limit under another key.
    api.policies["appx:paint"] = {
        **_policy("appx:paint", limit=False), "dailyAllowance": 3600000,
    }
    with pytest.raises(HomeAssistantError):
        await coord.async_pause_app(CHILD, ["x:racing", "appx:paint"])
    assert api.writes == []


async def test_only_windows_and_xbox_apps(hass, rig):
    coord, _, api, _ = rig
    result = await coord.async_pause_app(CHILD, ["a:com.example.game"])
    assert result["skipped"] == {"a:com.example.game": "not a Windows or Xbox app"}
    assert api.writes == []


def test_policy_state_refuses_other_shapes():
    assert policy_state(None) is None
    assert policy_state({"appId": "x:a"}) is None
    assert policy_state({"blockState": "NotBlocked", "monday": "01:00:00"}) is None
    assert policy_state({"blockState": "NotBlocked"}) is None  # enabled unknown
    state = policy_state(_policy("x:a", limit=False))
    assert state["days"] == {} and state["blockState"] == "NotBlocked"


async def test_load_drops_records_of_an_unknown_shape(hass, rig):
    _, _, _, entry = rig
    pauser = AppPauser(hass, entry.entry_id)
    good = {
        "prior": {"blockState": "NotBlocked", "days": {}},
        "written": {"blockState": "BlockedAlways", "days": {}},
        "platform": "xbox",
    }
    await pauser._store.async_save({
        CHILD: {"x:good": good, "x:old": {"blocked": False}},
        OTHER_CHILD: "junk",
    })
    await pauser.async_load()
    assert pauser.saved(CHILD) == {"x:good": good}
    assert pauser.saved(OTHER_CHILD) == {}


async def test_children_gone_from_the_family_are_pruned(hass, rig):
    coord, _, _, _ = rig
    await coord.async_pause_app(CHILD, ["x:racing"])
    pauser = coord.app_pauser
    assert pauser.stale_accounts({CHILD}) == set()
    assert pauser.stale_accounts({OTHER_CHILD}) == {CHILD}
    await pauser.async_prune({OTHER_CHILD})
    assert pauser.saved(CHILD) == {}
