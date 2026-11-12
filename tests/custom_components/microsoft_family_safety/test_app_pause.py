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
    LIMIT_KEYS,
    AppPauser,
    _refused,
    app_platform,
    read_entry,
)
from custom_components.microsoft_family_safety.const import DAY_KEYS, DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)
from custom_components.microsoft_family_safety.switch import FamilySafetyAppBlockSwitch

CHILD = "1234567890123456"
OTHER_CHILD = "6543210987654321"

LIMIT_DAY = {
    "allowance": 3600000,
    "intervalsEnabled": True,
    "intervals": [{"start": "07:00:00 AM", "end": "10:00:00 PM"}],
}


def _limit_policy(app_id: str, name: str, block_state: str = "NotBlocked") -> dict:
    """An app's policy with a limit, as the v4 app activity report gives it."""
    return {
        "appId": app_id,
        "blockState": block_state,
        "enabled": True,
        "displayName": name,
        "appTimeEnforcementPolicy": "Custom",
        "weekend": None,
        "weekday": None,
        "everyday": None,
        **{day: copy.deepcopy(LIMIT_DAY) for day in DAY_KEYS},
        "blocked": False,
        "override": None,
        "alternateIds": ["alt-1"],
        "alternateAppIds": ["alt-1"],
        "relatedWebsites": [],
        "categoryType": None,
        "categorization": None,
    }


def _entry(app_id: str, name: str, *, block_state: str = "NotBlocked", policy=None) -> dict:
    """An app's entry in the v4 app activity report."""
    return {
        "appId": app_id,
        "displayName": name,
        "iconUrl": "",
        "usage": 0,
        "policy": policy,
        "blockState": block_state,
        "isLegacyBlocked": False,
    }


def _error(status: int) -> FamilySafetyWebAPIError:
    return FamilySafetyWebAPIError(f"API request failed with status {status}: x", status=status)


class FakeWebAPI:
    """Microsoft's app policies, read through the activity report.

    A PATCH replaces the limit keys (absent = null) and the enforcement
    policy, as observed: block_app's body wipes a limit.
    """

    def __init__(self, entries: dict[str, dict]) -> None:
        self.entries = entries
        self.reads: list[str] = []
        self.writes: list[tuple[str, str, dict]] = []
        self.read_error: Exception | None = None
        self.write_error: Exception | None = None

    async def get_app_activity(self, child_id, platform):
        self.reads.append(platform)
        if self.read_error:
            raise self.read_error
        return [
            copy.deepcopy(entry) for app_id, entry in self.entries.items()
            if app_platform(app_id) == platform
        ]

    async def write_app_policy(self, child_id, app_id, platform, body):
        if self.write_error:
            raise self.write_error
        self.writes.append((app_id, platform, copy.deepcopy(body)))
        entry = self.entries[app_id]
        policy = dict(entry["policy"] or {"appId": app_id, "override": None})
        policy.update({key: copy.deepcopy(body.get(key)) for key in LIMIT_KEYS})
        for key in ("blockState", "enabled", "displayName", "appTimeEnforcementPolicy"):
            policy[key] = body[key]
        entry["policy"] = policy
        entry["blockState"] = body["blockState"]
        return {}


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
        "x:racing": SimpleNamespace(blocked=False),  # Xbox, has a time limit
        "appx:paint": SimpleNamespace(blocked=False),  # Windows, no limit
        "win32:chat": SimpleNamespace(blocked=True),  # blocked by a parent
    }
    api = FakeWebAPI({
        "x:racing": _entry("x:racing", "Racing", policy=_limit_policy("x:racing", "Racing")),
        "appx:paint": _entry("appx:paint", "Paint"),
        "win32:chat": _entry("win32:chat", "Chat", block_state="BlockedAlways"),
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
    before = copy.deepcopy(api.entries["x:racing"])

    paused = await _call(hass, "pause_app", apps=["x:racing", "win32:chat", "x:nope"])
    assert paused["paused"] == ["x:racing"]
    assert paused["skipped"] == {"win32:chat": "already blocked", "x:nope": "unknown app"}
    assert paused["failed"] == {}
    assert apps["x:racing"].blocked is True
    app_id, platform, body = api.writes[0]
    assert (app_id, platform) == ("x:racing", "xbox")
    # The block carries the limit and its enforcement policy, so Microsoft
    # keeps the limit stored; read-only fields are not sent.
    assert body == {
        "appId": "x:racing", "displayName": "Racing", "blockState": "BlockedAlways",
        "blocked": False, "enabled": True, "appTimeEnforcementPolicy": "Custom",
        "alternateAppIds": ["alt-1"], **{day: LIMIT_DAY for day in DAY_KEYS},
    }
    assert api.entries["x:racing"]["policy"]["sunday"] == LIMIT_DAY

    resumed = await _call(hass, "resume_app")
    assert resumed == {
        "account_id": CHILD, "resumed": ["x:racing"], "released": [],
        "changed_by_parent": [], "skipped": {}, "failed": {},
    }
    assert apps["x:racing"].blocked is False
    assert api.entries["x:racing"] == before
    assert apps["win32:chat"].blocked is True  # never touched
    assert coord.app_pauser.saved(CHILD) == {}


async def test_an_app_without_a_limit_is_blocked_and_unblocked(hass, rig):
    _, apps, api, _ = rig
    await _call(hass, "pause_app", apps=["appx:paint"])
    _, platform, body = api.writes[-1]
    assert platform == "windows"
    assert body == {
        "appId": "appx:paint", "displayName": "Paint", "blockState": "BlockedAlways",
        "blocked": False, "appTimeEnforcementPolicy": "WeekendAndWeekday", "enabled": True,
    }
    await _call(hass, "resume_app", apps=["appx:paint"])
    _, _, body = api.writes[-1]
    assert body["blockState"] == "NotBlocked"
    assert not any(key in body for key in LIMIT_KEYS)
    policy = api.entries["appx:paint"]["policy"]
    assert policy["blockState"] == "NotBlocked"
    assert all(policy[key] is None for key in LIMIT_KEYS)
    assert apps["appx:paint"].blocked is False


async def test_a_block_microsoft_reports_is_skipped(hass, rig):
    coord, apps, api, _ = rig
    # The polled report still says unblocked; the live read says blocked.
    api.entries["appx:paint"]["blockState"] = "Blocked"
    result = await coord.async_pause_app(CHILD, ["appx:paint"])
    assert result["skipped"] == {"appx:paint": "already blocked"}
    api.entries["appx:paint"].update(blockState="NotBlocked", isLegacyBlocked=True)
    result = await coord.async_pause_app(CHILD, ["appx:paint"])
    assert result["skipped"] == {"appx:paint": "already blocked"}
    assert api.writes == []


async def test_repeat_pause_keeps_the_first_state_also_after_a_restart(hass, rig):
    coord, _, api, entry = rig
    await _call(hass, "pause_app", apps=["x:racing"])
    # Still blocked by us: a second pause writes nothing and saves nothing new.
    again = await _call(hass, "pause_app", apps=["x:racing"])
    assert again["paused"] == ["x:racing"]
    assert len(api.writes) == 1

    restarted = AppPauser(hass, entry.entry_id)
    await restarted.async_load()
    saved = restarted.saved(CHILD)["x:racing"]
    assert saved["policy"]["days"] == {day: LIMIT_DAY for day in DAY_KEYS}

    coord.app_pauser = restarted
    await _call(hass, "resume_app", apps=["x:racing"])
    assert api.entries["x:racing"]["policy"] == _limit_policy("x:racing", "Racing")


async def test_a_repeat_pause_after_a_release_blocks_again(hass, rig):
    coord, _, api, _ = rig
    await coord.async_pause_app(CHILD, ["x:racing"])
    # Released in the app.
    api.entries["x:racing"]["blockState"] = "NotBlocked"
    api.entries["x:racing"]["policy"]["blockState"] = "NotBlocked"
    result = await coord.async_pause_app(CHILD, ["x:racing"])
    assert result["paused"] == ["x:racing"]
    assert api.entries["x:racing"]["policy"]["blockState"] == "BlockedAlways"
    assert api.entries["x:racing"]["policy"]["monday"] == LIMIT_DAY
    assert len(api.writes) == 2


async def test_a_repeat_pause_after_a_parent_change_leaves_it(hass, rig):
    coord, _, api, _ = rig
    await coord.async_pause_app(CHILD, ["x:racing"])
    api.entries["x:racing"]["policy"]["monday"] = {**LIMIT_DAY, "allowance": 7200000}
    result = await coord.async_pause_app(CHILD, ["x:racing"])
    assert result["changed_by_parent"] == ["x:racing"] and result["paused"] == []
    assert len(api.writes) == 1
    assert coord.app_pauser.saved(CHILD) == {}


async def test_resume_of_an_app_never_paused_is_skipped(hass, rig):
    _, _, api, _ = rig
    result = await _call(hass, "resume_app", apps=["appx:paint"])
    assert result["skipped"] == {"appx:paint": "not paused"}
    assert api.writes == [] and api.reads == []


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
    del api.entries["appx:paint"]  # gone from the report
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

    # It did go through: resume finds our block and puts the limit back.
    api.write_error = None
    api.entries["x:racing"]["blockState"] = "BlockedAlways"
    api.entries["x:racing"]["policy"]["blockState"] = "BlockedAlways"
    result = await coord.async_resume_app(CHILD)
    assert result["resumed"] == ["x:racing"]
    assert api.entries["x:racing"]["policy"] == _limit_policy("x:racing", "Racing")


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

    # A parent unblocked it in the app (keeping its limit).
    apps["x:racing"].blocked = False
    api.entries["x:racing"]["blockState"] = "NotBlocked"
    api.entries["x:racing"]["policy"]["blockState"] = "NotBlocked"
    by_id = {a["app_id"]: a["paused"] for a in count.extra_state_attributes["paused_apps"]}
    assert by_id == {"appx:paint": "ours", "x:racing": "parent"}

    writes = len(api.writes)
    result = await coord.async_resume_app(CHILD)
    assert result["released"] == ["x:racing"]
    assert result["resumed"] == ["appx:paint"]
    assert [w[0] for w in api.writes[writes:]] == ["appx:paint"]
    assert coord.app_pauser.saved(CHILD) == {}
    assert count.extra_state_attributes["paused_apps"] == []


async def test_a_lagging_poll_does_not_count_as_a_release(hass, rig):
    coord, apps, api, _ = rig
    await coord.async_pause_app(CHILD, ["x:racing"])
    apps["x:racing"].blocked = False  # the polled report lags; Microsoft still blocks
    result = await coord.async_resume_app(CHILD)
    assert result["resumed"] == ["x:racing"] and result["released"] == []
    assert api.entries["x:racing"]["policy"] == _limit_policy("x:racing", "Racing")


@pytest.mark.parametrize(
    ("app_id", "change"),
    [
        ("x:racing", {"saturday": {**LIMIT_DAY, "allowance": 7200000}}),  # limit changed
        ("x:racing", {"blockState": "Blocked"}),  # another block
        ("appx:paint", {"monday": LIMIT_DAY}),  # a limit set while paused
    ],
)
async def test_a_parent_change_while_paused_is_kept(hass, rig, app_id, change):
    coord, _, api, _ = rig
    await coord.async_pause_app(CHILD, [app_id])
    api.entries[app_id]["policy"].update(change)
    parents = copy.deepcopy(api.entries[app_id])
    result = await coord.async_resume_app(CHILD)
    assert result["changed_by_parent"] == [app_id] and result["resumed"] == []
    assert api.entries[app_id] == parents  # nothing written over it
    assert coord.app_pauser.saved(CHILD) == {}


async def test_an_unreadable_report_on_resume_keeps_the_record(hass, rig):
    coord, _, api, _ = rig
    await coord.async_pause_app(CHILD, ["x:racing"])
    api.read_error = _error(503)
    with pytest.raises(HomeAssistantError, match="policy_unreadable"):
        await coord.async_resume_app(CHILD)
    assert "x:racing" in coord.app_pauser.saved(CHILD)


async def test_no_pause_without_a_readable_policy(hass, rig):
    coord, _, api, _ = rig
    api.read_error = _error(400)
    with pytest.raises(HomeAssistantError, match="policy_unreadable"):
        await _call(hass, "pause_app", apps=["x:racing"])
    api.read_error = None
    # A limit key in an unknown shape.
    api.entries["x:racing"]["policy"]["monday"] = "01:00:00"
    # Enabled, but no limit key set: the limit may sit elsewhere.
    policy = _limit_policy("appx:paint", "Paint")
    policy.update({day: None for day in DAY_KEYS})
    api.entries["appx:paint"]["policy"] = policy
    with pytest.raises(HomeAssistantError, match="policy_unreadable"):
        await coord.async_pause_app(CHILD, ["x:racing", "appx:paint"])
    assert api.writes == []
    assert coord.app_pauser.saved(CHILD) == {}


async def test_only_windows_and_xbox_apps(hass, rig):
    coord, _, api, _ = rig
    result = await coord.async_pause_app(CHILD, ["a:com.example.game"])
    assert result["skipped"] == {"a:com.example.game": "not a Windows or Xbox app"}
    assert api.writes == [] and api.reads == []


def test_platform_of_an_app_id():
    assert app_platform("x:9NBLGGH4R315") == "xbox"
    assert app_platform("appx:windowsplt:Example.App_abc") == "windows"
    assert app_platform("x:windowsplt:Example.App_abc") == "windows"
    assert app_platform("win32:example.exe") == "windows"
    assert app_platform("a:com.example") is None


def test_read_entry():
    assert read_entry(None) is None
    assert read_entry({"appId": "x:a"}) is None
    free = read_entry(_entry("x:a", "A"))
    assert (free["block"], free["days"], free["policy"]) == ("NotBlocked", {}, None)
    limited = read_entry(_entry("x:a", "A", policy=_limit_policy("x:a", "A")))
    assert limited["days"] == {day: LIMIT_DAY for day in DAY_KEYS}
    assert read_entry(_entry("x:a", "A", policy="weird")) is None


async def test_load_drops_records_of_an_unknown_shape(hass, rig):
    _, _, _, entry = rig
    pauser = AppPauser(hass, entry.entry_id)
    good = {"platform": "xbox", "name": "A", "policy": None, "written_days": {}}
    await pauser._store.async_save({
        CHILD: {"x:good": good, "x:old": {"blocked": False}},
        OTHER_CHILD: "junk",
    })
    await pauser.async_load()
    assert pauser.saved(CHILD) == {"x:good": good}
    assert pauser.saved(OTHER_CHILD) == {}


def _app_block_switch(hass, coord, entry, app_id, name):
    switch = FamilySafetyAppBlockSwitch(coord, entry, CHILD, "Alex", app_id, name)
    switch.hass = hass
    switch.entity_id = f"switch.alex_{name.lower()}_blocked"
    return switch


def _plain_calls(apps):
    """pyfamilysafety's block_app / unblock_app, whose bodies wipe a limit."""
    for app in apps.values():
        app.block_app = AsyncMock()
        app.unblock_app = AsyncMock()
    return lambda: [
        (app_id, name) for app_id, app in apps.items()
        for name in ("block_app", "unblock_app") if getattr(app, name).await_count
    ]


async def test_the_block_switch_off_resumes_a_paused_app(hass, rig):
    coord, apps, api, entry = rig
    plain = _plain_calls(apps)
    await coord.async_pause_app(CHILD, ["x:racing"])
    switch = _app_block_switch(hass, coord, entry, "x:racing", "Racing")
    await switch.async_turn_off()
    # The saved limit is written back, not wiped, and the pause is consumed.
    assert api.entries["x:racing"]["policy"] == _limit_policy("x:racing", "Racing")
    assert coord.app_pauser.saved(CHILD) == {}
    assert plain() == []
    # Not paused any more: the next unblock is the plain one again.
    await switch.async_turn_off()
    assert plain() == [("x:racing", "unblock_app")]


async def test_the_unblock_app_service_resumes_a_paused_app(hass, rig):
    coord, apps, api, _ = rig
    plain = _plain_calls(apps)
    await coord.async_pause_app(CHILD, ["x:racing", "appx:paint"])
    await hass.services.async_call(
        DOMAIN, "unblock_app", {"account_id": CHILD, "app_id": "x:racing"}, blocking=True
    )
    assert api.entries["x:racing"]["policy"] == _limit_policy("x:racing", "Racing")
    assert list(coord.app_pauser.saved(CHILD)) == ["appx:paint"]  # only that one
    assert plain() == []
    # A later resume_app has nothing to report as a parent change.
    result = await coord.async_resume_app(CHILD)
    assert result["resumed"] == ["appx:paint"] and result["changed_by_parent"] == []


async def test_unblocking_a_paused_app_that_cannot_be_read_fails_and_keeps_it(hass, rig):
    coord, apps, api, _ = rig
    plain = _plain_calls(apps)
    await coord.async_pause_app(CHILD, ["x:racing"])
    api.read_error = _error(503)
    with pytest.raises(HomeAssistantError, match="policy_unreadable"):
        await coord.async_unblock_app(CHILD, "x:racing")
    assert "x:racing" in coord.app_pauser.saved(CHILD)
    assert plain() == []  # no limit-wiping fallback


async def test_unblocking_a_paused_app_a_parent_released_writes_nothing(hass, rig):
    coord, apps, api, _ = rig
    plain = _plain_calls(apps)
    await coord.async_pause_app(CHILD, ["x:racing"])
    api.entries["x:racing"]["blockState"] = "NotBlocked"
    api.entries["x:racing"]["policy"]["blockState"] = "NotBlocked"
    writes = len(api.writes)
    await coord.async_unblock_app(CHILD, "x:racing")
    assert len(api.writes) == writes and plain() == []
    assert coord.app_pauser.saved(CHILD) == {}


async def test_unblocking_a_paused_app_a_parent_reblocked_unblocks_it(hass, rig):
    coord, apps, api, _ = rig
    plain = _plain_calls(apps)
    await coord.async_pause_app(CHILD, ["x:racing"])
    api.entries["x:racing"]["policy"]["monday"] = {**LIMIT_DAY, "allowance": 7200000}
    await coord.async_unblock_app(CHILD, "x:racing")
    # The parent's state is no longer ours to put back: unblocked as usual.
    assert plain() == [("x:racing", "unblock_app")]
    assert coord.app_pauser.saved(CHILD) == {}


def _slow_reads(api):
    """Let other calls run while a read is in flight, as a real request does."""
    read = api.get_app_activity

    async def slow(child_id, platform):
        await asyncio.sleep(0)
        return await read(child_id, platform)

    api.get_app_activity = slow


async def test_two_concurrent_unblocks_of_a_paused_app_keep_its_limit(hass, rig):
    coord, apps, api, _ = rig
    plain = _plain_calls(apps)
    await coord.async_pause_app(CHILD, ["x:racing"])
    _slow_reads(api)
    await asyncio.gather(
        coord.async_unblock_app(CHILD, "x:racing"),
        coord.async_unblock_app(CHILD, "x:racing"),
    )
    # The second one finds it resumed by the first: no limit-wiping write.
    assert plain() == []
    assert api.entries["x:racing"]["policy"] == _limit_policy("x:racing", "Racing")
    assert coord.app_pauser.saved(CHILD) == {}


async def test_an_unblock_during_a_resume_keeps_the_limit(hass, rig):
    coord, apps, api, _ = rig
    plain = _plain_calls(apps)
    await coord.async_pause_app(CHILD, ["x:racing"])
    _slow_reads(api)
    result, _ = await asyncio.gather(
        coord.async_resume_app(CHILD),
        coord.async_unblock_app(CHILD, "x:racing"),
    )
    assert result["resumed"] == ["x:racing"]
    assert plain() == []
    assert api.entries["x:racing"]["policy"] == _limit_policy("x:racing", "Racing")


async def test_blocking_a_paused_app_keeps_its_limit(hass, rig):
    coord, apps, api, entry = rig
    plain = _plain_calls(apps)
    await coord.async_pause_app(CHILD, ["x:racing"])
    switch = _app_block_switch(hass, coord, entry, "x:racing", "Racing")
    await switch.async_turn_on()  # already blocked by the pause: nothing written
    await hass.services.async_call(
        DOMAIN, "block_app", {"account_id": CHILD, "app_id": "x:racing"}, blocking=True
    )
    assert len(api.writes) == 1 and plain() == []
    # Released in the app, then blocked again: the saved limit stays stored.
    api.entries["x:racing"]["blockState"] = "NotBlocked"
    api.entries["x:racing"]["policy"]["blockState"] = "NotBlocked"
    await switch.async_turn_on()
    assert api.entries["x:racing"]["policy"]["blockState"] == "BlockedAlways"
    assert api.entries["x:racing"]["policy"]["monday"] == LIMIT_DAY
    assert plain() == []
    result = await coord.async_resume_app(CHILD)
    assert result["resumed"] == ["x:racing"]
    assert api.entries["x:racing"]["policy"] == _limit_policy("x:racing", "Racing")


async def test_blocking_an_app_never_paused_is_the_plain_block(hass, rig):
    coord, apps, api, _ = rig
    plain = _plain_calls(apps)
    await coord.async_block_app(CHILD, "appx:paint")
    assert plain() == [("appx:paint", "block_app")]
    assert api.reads == [] and api.writes == []


async def test_a_child_missing_from_a_roster_keeps_its_pauses(hass, rig):
    """Pauses are not pruned on a poll: a partial roster must not lose them."""
    coord, _, api, entry = rig
    await coord.async_pause_app(CHILD, ["x:racing"])
    coord.data = {"accounts": {OTHER_CHILD: {"applications": []}}, "devices": {}}
    coord.async_update_listeners()
    await hass.async_block_till_done()
    restarted = AppPauser(hass, entry.entry_id)
    await restarted.async_load()
    assert "x:racing" in restarted.saved(CHILD)
    result = await coord.async_resume_app(CHILD)
    assert result["resumed"] == ["x:racing"]
    assert api.entries["x:racing"]["policy"] == _limit_policy("x:racing", "Racing")


async def test_activity_report_range_stays_under_seven_days() -> None:
    """Microsoft answers 400 BeginEndTimeRangeError for 7 days or more."""
    from datetime import datetime, timedelta
    from unittest.mock import AsyncMock

    from custom_components.microsoft_family_safety.api_client import FamilySafetyWebAPI

    api = FamilySafetyWebAPI(authenticator=None)
    api._request = AsyncMock(return_value={"appActivity": []})
    for days in (None, 7, 30):
        api._request.reset_mock()
        if days is None:
            await api.get_app_activity("1234567890123456", "windows")
        else:
            await api.get_app_activity("1234567890123456", "windows", days=days)
        params = api._request.await_args.kwargs["params"]
        begin = datetime.strptime(params["beginTime"], "%Y-%m-%dT%H:%M:%SZ")
        end = datetime.strptime(params["endTime"], "%Y-%m-%dT%H:%M:%SZ")
        assert timedelta(0) < end - begin < timedelta(days=7)
        assert api._request.await_args.kwargs["extra_headers"] == {"Plat-Info": "Windows"}
