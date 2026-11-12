"""lock_platform saves what was in force, unlock_platform puts it back.

The first lock of a platform reads Microsoft's override (a parent's own lock,
extra time, or nothing) and keeps it in a Store with the fact that this
integration holds a lock there. A later lock, after a failed call or a
restart, never takes its own lock for that prior state; unlock restores it
while it has not run out, else cancels, and only undoes a lock that is still
ours.

Xbox is read and written through the Xbox Family Settings service
(xapprove), Windows and Mobile are read from the mobile API. No test here
gives the coordinator a Family web session.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.microsoft_family_safety import (
    _register_services,
    async_remove_entry,
)
from custom_components.microsoft_family_safety.const import CONF_STRICT_MODE, DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    PLATFORM_HOLD_STORAGE_KEY,
    FamilySafetyDataUpdateCoordinator,
)
from custom_components.microsoft_family_safety.screen_time import (
    iso_utc,
    parse_mobile_override,
    parse_utc,
    parse_xbox_override,
)
from custom_components.microsoft_family_safety.switch import (
    FamilySafetyPlatformLockSwitch,
)
from custom_components.microsoft_family_safety.xbox_family_api import (
    OVERRIDE_ALLOW,
    OVERRIDE_BLOCK,
    OVERRIDE_REVOKE,
    XboxFamilyError,
)

CHILD = "1234567890123456"
NONE = {"type": "none", "valid_until": None}
XUID = "2535400000000001"


def _hours(hours: float) -> datetime:
    return dt_util.utcnow() + timedelta(hours=hours)


def _xbox(kind: int, until: datetime) -> dict:
    return {"overrideType": kind, "validUntil": iso_utc(until)}


class FakeXbox:
    """The Xbox Family Settings service: its overrides and the writes it gets."""

    def __init__(self) -> None:
        self.linked = True
        self.ready = True
        self.usable = True
        self.overrides: list[dict] | None = []
        self.writes: list[tuple[int, str]] = []
        self.fail = False
        #: Microsoft applies the write but the call still fails.
        self.apply_then_fail = False

    async def async_child_xuid(self, first_name, surname):
        return XUID

    async def async_get_overrides(self, xuid):
        if self.overrides is None:
            raise XboxFamilyError("read failed")
        return {"overrides": list(self.overrides)}

    async def async_create_override(self, xuid, override_type, valid_until):
        if self.fail and not self.apply_then_fail:
            raise XboxFamilyError("HTTP 403")
        self.writes.append((override_type, iso_utc(valid_until)))
        self.overrides = (
            [] if override_type == OVERRIDE_REVOKE else [_xbox(override_type, valid_until)]
        )
        if self.fail:
            raise XboxFamilyError("HTTP 403")
        return {"overrides": list(self.overrides)}


class FakeMobile:
    """The mobile API's /v4/devicelimits/{id}/overrides for one child."""

    def __init__(self) -> None:
        self.platforms: dict[str, list[dict]] = {"Windows": [], "Mobile": [], "Xbox": []}
        self.writes: list[tuple[str, str]] = []
        self.reads = 0

    def state(self) -> dict:
        return {
            "lockablePlatforms": [
                {"appliesTo": name, "overrides": list(entries), "devices": []}
                for name, entries in self.platforms.items()
            ]
        }

    async def get_mobile_device_overrides(self, child_id):
        assert child_id == CHILD
        self.reads += 1
        return self.state()

    async def set_mobile_device_override(self, child_id, target, override_type, valid_until):
        assert child_id == CHILD
        if override_type == "Cancel":
            before = list(self.platforms[target])
            self.platforms[target] = []
            self.writes.append((target, "Cancel"))
            if getattr(self, "lag_after_cancel", False):
                # Answers the cancelled state; later GETs still list the old one.
                answer = self.state()
                self.platforms[target] = before
                return answer
        else:
            assert override_type == "BlockUntil"
            until = iso_utc(valid_until)
            self.platforms[target] = [{"overrideType": "BlockUntil", "validUntil": until}]
            self.writes.append((target, until))
        return self.state()


def _make(hass, entry, xbox: FakeXbox, mobile: FakeMobile) -> FamilySafetyDataUpdateCoordinator:
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.xbox_family = xbox
    coord.web_api = mobile
    coord._accounts[CHILD] = SimpleNamespace(
        user_id=CHILD, first_name="Alex", surname="Rivera", blocked_platforms=[],
    )
    coord.async_request_refresh = AsyncMock()
    coord.data = {"accounts": {CHILD: {"blocked_platforms": []}}, "devices": {}}
    return coord


@pytest.fixture
def entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def xbox():
    return FakeXbox()


@pytest.fixture
def mobile():
    return FakeMobile()


@pytest.fixture
def coordinator(hass, entry, xbox, mobile):
    return _make(hass, entry, xbox, mobile)


# --- Xbox through xapprove -------------------------------------------------------


async def test_xbox_lock_writes_a_block_and_saves_nothing_in_force(coordinator, xbox):
    result = await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))

    assert [kind for kind, _ in xbox.writes] == [OVERRIDE_BLOCK]
    assert result["prior"]["type"] == "none"
    assert result["applied"]["type"] == "blockUntil"
    assert parse_utc(result["applied"]["valid_until"]) == parse_utc(xbox.writes[0][1])
    hold = coordinator.platform_hold(CHILD, "Xbox")
    assert hold["confirmed"] is True
    assert "Xbox" in coordinator._merge_blocked_platforms(coordinator._accounts[CHILD])


async def test_unlock_restores_the_parent_lock_in_force_before(coordinator, xbox):
    parent_until = _hours(1)
    xbox.overrides = [_xbox(OVERRIDE_BLOCK, parent_until)]

    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(3))
    result = await coordinator.async_unlock_platform(CHILD, "Xbox")

    assert xbox.writes[-1] == (OVERRIDE_BLOCK, iso_utc(parent_until))
    assert result["prior"]["type"] == "blockUntil"
    assert result["applied"]["type"] == "blockUntil"
    assert coordinator.platform_hold(CHILD, "Xbox") is None


async def test_unlock_with_nothing_saved_cancels(coordinator, xbox):
    result = await coordinator.async_unlock_platform(CHILD, "Xbox")

    assert [kind for kind, _ in xbox.writes] == [OVERRIDE_REVOKE]
    assert result == {"prior": None, "applied": {"type": "none", "valid_until": None}}


async def test_unlock_discards_a_prior_lock_that_has_run_out(coordinator, xbox):
    xbox.overrides = [_xbox(OVERRIDE_BLOCK, _hours(1))]
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(3))
    # The parent's lock ended while ours was held.
    key = coordinator._hold_key(CHILD, "Xbox")
    coordinator._platform_holds[key]["prior"]["valid_until"] = iso_utc(_hours(-0.5))

    result = await coordinator.async_unlock_platform(CHILD, "Xbox")

    assert xbox.writes[-1][0] == OVERRIDE_REVOKE
    assert result["applied"]["type"] == "none"


async def test_extra_time_before_the_lock_is_not_written_back(coordinator, xbox):
    xbox.overrides = [_xbox(OVERRIDE_ALLOW, _hours(1))]
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(3))

    result = await coordinator.async_unlock_platform(CHILD, "Xbox")

    assert xbox.writes[-1][0] == OVERRIDE_REVOKE
    assert result["applied"]["not_restored"]["type"] == "allowUntil"


async def test_a_failed_write_keeps_the_saved_prior_for_the_retry(coordinator, xbox):
    xbox.fail = True
    xbox.apply_then_fail = True  # Microsoft stored it anyway
    with pytest.raises(HomeAssistantError):
        await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))
    hold = coordinator.platform_hold(CHILD, "Xbox")
    assert hold["prior"]["type"] == "none"
    assert hold["confirmed"] is False
    # The read-back now shows our own block; the retry must not save it.
    assert parse_xbox_override({"overrides": xbox.overrides})["type"] == "blockUntil"

    xbox.fail = False
    result = await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))

    assert result["prior"]["type"] == "none"
    await coordinator.async_unlock_platform(CHILD, "Xbox")
    assert xbox.writes[-1][0] == OVERRIDE_REVOKE


async def test_a_refused_write_reports_the_failure(coordinator, xbox):
    xbox.fail = True
    with pytest.raises(HomeAssistantError):
        await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))
    assert xbox.writes == []


async def test_repeat_lock_keeps_the_prior_and_the_later_end(coordinator, xbox):
    xbox.overrides = [_xbox(OVERRIDE_BLOCK, _hours(1))]
    first = await coordinator.async_lock_platform(CHILD, "Xbox", _hours(5))
    second = await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))

    assert second["prior"] == first["prior"]
    assert second["prior"]["type"] == "blockUntil"
    assert second["applied"]["valid_until"] == first["applied"]["valid_until"]


async def test_lock_keeps_a_parent_lock_ending_later(coordinator, xbox):
    parent_until = _hours(10)
    xbox.overrides = [_xbox(OVERRIDE_BLOCK, parent_until)]

    result = await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))

    assert parse_utc(result["applied"]["valid_until"]) == parse_utc(iso_utc(parent_until))


async def test_prior_survives_a_restart(hass, entry, coordinator, xbox, mobile, hass_storage):
    xbox.overrides = [_xbox(OVERRIDE_BLOCK, _hours(1))]
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(3))
    assert f"{PLATFORM_HOLD_STORAGE_KEY}.{entry.entry_id}" in hass_storage

    # A new coordinator after a restart; Microsoft now shows our own lock.
    restarted = _make(hass, entry, xbox, mobile)
    await restarted.async_load_saved_screentime()
    result = await restarted.async_lock_platform(CHILD, "Xbox", _hours(3))
    assert result["prior"]["type"] == "blockUntil"
    assert parse_utc(result["prior"]["valid_until"]) < _hours(1.1)

    unlocked = await restarted.async_unlock_platform(CHILD, "Xbox")
    assert unlocked["applied"]["type"] == "blockUntil"


async def test_a_hold_whose_lock_ran_out_starts_over(coordinator, xbox):
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))
    key = coordinator._hold_key(CHILD, "Xbox")
    coordinator._platform_holds[key]["until"] = iso_utc(_hours(-1))
    xbox.overrides = [_xbox(OVERRIDE_BLOCK, _hours(4))]  # a parent lock since

    result = await coordinator.async_lock_platform(CHILD, "Xbox", _hours(1))

    assert result["prior"]["type"] == "blockUntil"


async def test_int_roster_ids_and_str_service_ids_meet(coordinator, xbox):
    # The roster keys accounts by int user_id; services pass a str.
    coordinator._accounts = {int(CHILD): coordinator._accounts.pop(CHILD)}
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))
    assert coordinator.platform_hold(int(CHILD), "Xbox")["confirmed"] is True
    await coordinator.async_unlock_platform(int(CHILD), "Xbox")
    assert coordinator.platform_hold(CHILD, "Xbox") is None


async def test_xbox_without_the_xapprove_link_is_refused_without_the_web(coordinator, xbox):
    """No Xbox Family Settings link: refused, never sent through the web session."""
    xbox.linked = False
    xbox.usable = False
    web = _web_session(coordinator, coordinator.web_api)

    with pytest.raises(HomeAssistantError, match="Xbox Family Settings"):
        await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))
    # The refused lock never went through: nothing to undo.
    assert (await coordinator.async_unlock_platform(CHILD, "Xbox"))["applied"] == "not_locked"
    with pytest.raises(HomeAssistantError, match="Xbox Family Settings"):
        await coordinator.async_unlock_platform(CHILD, "Xbox")

    assert web.method_calls == []
    assert xbox.writes == []


def _web_session(coordinator, mobile: FakeMobile) -> MagicMock:
    """A live Family web session, to prove the lock paths never use it.

    The client keeps its mobile-API overrides calls; any other call is recorded.
    """
    web = MagicMock(has_web_cookies=True, family_context_state="ready")
    web.set_platform_time_override = AsyncMock(return_value=True)
    web.get_mobile_device_overrides = mobile.get_mobile_device_overrides
    web.set_mobile_device_override = mobile.set_mobile_device_override
    coordinator.web_api = web
    coordinator._has_web_credentials = True
    return web


@pytest.mark.parametrize("platform", ["Xbox", "Windows"])
async def test_no_write_goes_through_the_web_session(hass, platform):
    """Lock, restore, unlock, release and a strict-mode re-lock: mobile API or xapprove only."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={"refresh_token": "x"}, options={CONF_STRICT_MODE: True}
    )
    entry.add_to_hass(hass)
    xbox, mobile = FakeXbox(), FakeMobile()
    coord = _make(hass, entry, xbox, mobile)
    web = _web_session(coord, mobile)
    writes = xbox.writes if platform == "Xbox" else mobile.writes
    parent = _hours(1)
    if platform == "Xbox":
        xbox.overrides = [_xbox(OVERRIDE_BLOCK, parent)]
    else:
        mobile.platforms["Windows"] = [
            {"overrideType": "BlockUntil", "validUntil": iso_utc(parent)}
        ]

    await coord.async_lock_platform(CHILD, platform, _hours(3))
    restored = await coord.async_unlock_platform(CHILD, platform)  # the parent lock back
    assert restored["applied"]["type"] == "blockUntil"
    await coord.async_lock_platform(CHILD, platform, _hours(3))
    key = coord._hold_key(CHILD, platform)
    coord._platform_holds[key]["confirmed_at"] = iso_utc(_hours(-1))
    xbox.overrides = []
    mobile.platforms["Windows"] = []
    coord._mobile_writes.clear()  # our write is minutes old
    before = len(writes)
    await coord._async_enforce_holds({CHILD: NONE})  # cancelled elsewhere: placed again
    await hass.async_block_till_done()
    assert len(writes) == before + 1
    await coord.async_unlock_platform(CHILD, platform)
    await coord.async_release_platform(CHILD, platform)

    assert len(writes) == 6
    assert web.method_calls == []


# --- Windows and Mobile: read from the mobile API -------------------------------


async def test_windows_prior_comes_from_the_mobile_api(coordinator, mobile):
    parent_until = _hours(1)
    mobile.platforms["Windows"] = [
        {"overrideType": "BlockUntil", "validUntil": iso_utc(parent_until)}
    ]
    result = await coordinator.async_lock_platform(CHILD, "Windows", _hours(3))
    assert result["prior"]["type"] == "blockUntil"
    assert mobile.writes[-1][0] == "Windows"

    unlocked = await coordinator.async_unlock_platform(CHILD, "Windows")

    assert mobile.writes[-1] == ("Windows", iso_utc(parent_until))
    assert unlocked["applied"]["type"] == "blockUntil"
    assert coordinator.platform_hold(CHILD, "Windows") is None


async def test_windows_listed_as_desktop(coordinator, mobile):
    """Microsoft may list Windows as "Desktop": read and written under that name."""
    parent_until = _hours(1)
    mobile.platforms = {
        "Desktop": [{"overrideType": "BlockUntil", "validUntil": iso_utc(parent_until)}],
        "Xbox": [],
    }
    result = await coordinator.async_lock_platform(CHILD, "Windows", _hours(3))
    assert result["prior"]["type"] == "blockUntil"
    assert mobile.writes[-1][0] == "Desktop"
    assert coordinator._accounts[CHILD].blocked_platforms == ["Windows"]

    await coordinator.async_unlock_platform(CHILD, "Windows")
    assert mobile.writes[-1] == ("Desktop", iso_utc(parent_until))


async def test_reads_never_touch_the_web_session(coordinator, mobile):
    web = _web_session(coordinator, mobile)
    await coordinator.async_lock_platform(CHILD, "Mobile", _hours(2))
    await coordinator.async_unlock_platform(CHILD, "Mobile")
    # Each: the hold's read and the write's target read; the POST answer decides.
    assert mobile.reads == 4
    assert [name for name, _ in mobile.writes] == ["Mobile", "Mobile"]
    assert web.method_calls == []


def test_override_parsers():
    now = dt_util.utcnow()
    later = iso_utc(now + timedelta(hours=1))
    earlier = iso_utc(now - timedelta(minutes=1))
    mobile = {"lockablePlatforms": [
        {"appliesTo": "Xbox", "overrides": [{"overrideType": "BlockUntil", "validUntil": later}]},
        {"appliesTo": "Windows", "overrides": []},
    ]}
    assert parse_mobile_override(mobile, "Windows")["type"] == "none"
    assert parse_mobile_override(mobile, "Xbox")["type"] == "blockUntil"
    assert parse_mobile_override(
        {"lockablePlatforms": [{"appliesTo": "Windows", "overrides": [{"overrideType": "Mystery"}]}]},
        "Windows",
    ) is None
    assert parse_mobile_override({"something": 1}, "Windows") is None

    assert parse_xbox_override({"overrides": []})["type"] == "none"
    assert parse_xbox_override({})["type"] == "none"
    assert parse_xbox_override({"overrides": [{"overrideType": 2, "validUntil": later}]})["type"] == "blockUntil"
    assert parse_xbox_override({"overrides": [{"overrideType": 1, "validUntil": later}]})["type"] == "allowUntil"
    # A release, an expired lock, an unknown type, no end.
    assert parse_xbox_override({"overrides": [{"overrideType": 100, "validUntil": later}]})["type"] == "none"
    assert parse_xbox_override({"overrides": [{"overrideType": 2, "validUntil": earlier}]})["type"] == "none"
    assert parse_xbox_override({"overrides": [{"overrideType": 7, "validUntil": later}]}) is None
    assert parse_xbox_override({"overrides": [{"overrideType": 2}]}) is None
    assert parse_xbox_override(None) is None
    # Two of a kind: the one ending last.
    latest = iso_utc(now + timedelta(hours=5))
    assert parse_xbox_override({"overrides": [
        {"overrideType": 2, "validUntil": later}, {"overrideType": 2, "validUntil": latest},
    ]})["valid_until"] == latest
    assert parse_mobile_override({"lockablePlatforms": [{"appliesTo": "Windows", "overrides": [
        {"overrideType": "BlockUntil", "validUntil": latest},
        {"overrideType": "BlockUntil", "validUntil": later},
    ]}]}, "Windows")["valid_until"] == latest


# --- unlock only touches a lock that is still ours ------------------------------


async def test_unlock_after_our_hold_ran_out_writes_nothing(coordinator, xbox):
    xbox.overrides = [_xbox(OVERRIDE_BLOCK, _hours(1))]  # a parent lock before ours
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(3))
    key = coordinator._hold_key(CHILD, "Xbox")
    coordinator._platform_holds[key]["until"] = iso_utc(_hours(-0.1))
    xbox.writes.clear()

    result = await coordinator.async_unlock_platform(CHILD, "Xbox")

    assert result["applied"] == "hold_expired"
    assert xbox.writes == []
    assert coordinator.platform_hold(CHILD, "Xbox") is None


@pytest.mark.parametrize(
    ("parent", "kind"),
    [
        (lambda: [_xbox(OVERRIDE_BLOCK, _hours(8))], "blockUntil"),  # a longer lock
        (lambda: [_xbox(OVERRIDE_ALLOW, _hours(1))], "allowUntil"),  # extra time
    ],
)
async def test_unlock_leaves_a_parent_change_alone(coordinator, xbox, parent, kind):
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(3))
    xbox.overrides = parent()
    xbox.writes.clear()

    result = await coordinator.async_unlock_platform(CHILD, "Xbox")

    assert result["applied"] == "left_parent_override"
    assert result["current"]["type"] == kind
    assert xbox.writes == []
    assert coordinator.platform_hold(CHILD, "Xbox") is None


async def test_unlock_when_the_state_cannot_be_read_still_unlocks(coordinator, xbox):
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(3))
    xbox.overrides = None  # the read fails
    xbox.writes.clear()

    result = await coordinator.async_unlock_platform(CHILD, "Xbox")

    assert xbox.writes[-1][0] == OVERRIDE_REVOKE
    assert result["applied"]["type"] == "none"
    assert result["state_unknown"] is True


@pytest.mark.parametrize("platform", ["Xbox", "Windows"])
async def test_unlock_cancels_when_the_read_shows_nothing(coordinator, xbox, mobile, platform):
    """Released in the app, or a read that misses our lock: cancel anyway."""
    await coordinator.async_lock_platform(CHILD, platform, _hours(3))
    xbox.overrides = []
    mobile.platforms[platform] = []
    xbox.writes.clear()
    mobile.writes.clear()

    result = await coordinator.async_unlock_platform(CHILD, platform)

    assert result["applied"]["type"] == "none"
    if platform == "Xbox":
        assert [kind for kind, _ in xbox.writes] == [OVERRIDE_REVOKE]
    else:
        assert mobile.writes == [("Windows", "Cancel")]
    assert coordinator.platform_hold(CHILD, platform) is None


async def test_a_parent_extension_is_not_taken_over_by_a_repeat_lock(coordinator, xbox):
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))
    parent_until = _hours(8)
    xbox.overrides = [_xbox(OVERRIDE_BLOCK, parent_until)]  # extended in the app
    xbox.writes.clear()

    again = await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))

    assert again["applied"] == "parent_lock_kept"
    assert parse_utc(again["current"]["valid_until"]) == parse_utc(iso_utc(parent_until))
    hold = coordinator.platform_hold(CHILD, "Xbox")
    assert hold["superseded"] is True
    assert parse_utc(hold["until"]) < _hours(2.1)
    unlocked = await coordinator.async_unlock_platform(CHILD, "Xbox")
    assert unlocked["applied"] == "left_parent_override"
    assert xbox.writes == []  # the parent's extension stays


async def test_an_unreadable_prior_still_locks_and_unlock_only_cancels_its_own(
    coordinator, xbox
):
    xbox.overrides = None  # both reads fail
    result = await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))
    assert result["prior"]["type"] == "unknown"
    assert result["applied"]["type"] == "blockUntil"

    # A parent's other lock is in force now: left alone.
    xbox.overrides = [_xbox(OVERRIDE_BLOCK, _hours(6))]
    xbox.writes.clear()
    left = await coordinator.async_unlock_platform(CHILD, "Xbox")
    assert left["applied"] == "left_parent_override"
    assert left["prior_unknown"] is True
    assert xbox.writes == []

    # Our own lock is identifiable: cancelled.
    xbox.overrides = None
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))
    xbox.writes.clear()
    ours = await coordinator.async_unlock_platform(CHILD, "Xbox")
    assert ours["applied"]["type"] == "none"
    assert ours["prior_unknown"] is True
    assert [kind for kind, _ in xbox.writes] == [OVERRIDE_REVOKE]


async def test_the_prior_is_read_a_second_time_when_the_first_read_fails(coordinator, xbox):
    reads = []
    original = xbox.async_get_overrides

    async def flaky(xuid):
        reads.append(xuid)
        if len(reads) == 1:
            raise XboxFamilyError("timeout")
        return await original(xuid)

    xbox.async_get_overrides = flaky
    xbox.overrides = [_xbox(OVERRIDE_BLOCK, _hours(1))]
    result = await coordinator.async_lock_platform(CHILD, "Xbox", _hours(3))
    assert len(reads) == 2
    assert result["prior"]["type"] == "blockUntil"


async def test_expired_holds_are_pruned_a_day_after_their_end(coordinator):
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))
    await coordinator.async_lock_platform(CHILD, "Windows", _hours(2))
    holds = coordinator._platform_holds
    holds[coordinator._hold_key(CHILD, "Xbox")]["until"] = iso_utc(_hours(-25))
    holds[coordinator._hold_key(CHILD, "Windows")]["until"] = iso_utc(_hours(-1))

    await coordinator._async_prune_platform_holds()

    assert coordinator.platform_hold(CHILD, "Xbox") is None
    # Ended an hour ago: kept, so an unlock still knows it ran out.
    assert coordinator.platform_hold(CHILD, "Windows") is not None


async def test_removing_the_entry_deletes_the_holds(hass, entry, coordinator, hass_storage):
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(2))
    key = f"{PLATFORM_HOLD_STORAGE_KEY}.{entry.entry_id}"
    assert key in hass_storage
    await async_remove_entry(hass, entry)
    assert key not in hass_storage


async def test_the_lock_switch_off_cancels_without_restoring(coordinator, entry, xbox):
    xbox.overrides = [_xbox(OVERRIDE_BLOCK, _hours(1))]
    await coordinator.async_lock_platform(CHILD, "Xbox", _hours(3))
    switch = FamilySafetyPlatformLockSwitch(coordinator, entry, CHILD, "Alex", "Xbox")
    switch.hass = coordinator.hass

    await switch.async_turn_off()

    assert xbox.writes[-1][0] == OVERRIDE_REVOKE
    assert coordinator.platform_hold(CHILD, "Xbox") is None


# --- the service: until, duration_hours, response -------------------------------


@pytest.fixture
def service(hass):
    coord = MagicMock()
    coord.async_lock_platform = AsyncMock(
        return_value={"prior": {"type": "none", "valid_until": None},
                      "applied": {"type": "blockUntil", "valid_until": "x"}}
    )
    coord.async_unlock_platform = AsyncMock(return_value={"prior": None, "applied": {}})
    with patch(
        "custom_components.microsoft_family_safety._get_coordinator", return_value=coord
    ):
        _register_services(hass)
        yield coord


async def _call(hass, name="lock_platform", response=False, **data):
    return await hass.services.async_call(
        DOMAIN, name, {"account_id": CHILD, "platform": "Xbox", **data},
        blocking=True, return_response=response,
    )


async def test_service_defaults_to_24_hours(hass, service):
    before = dt_util.now()
    await _call(hass)
    until = service.async_lock_platform.await_args.args[2]
    assert timedelta(hours=23, minutes=59) < until - before < timedelta(hours=24, minutes=1)


async def test_service_takes_until_instead_of_hours(hass, service):
    wanted = dt_util.now().replace(microsecond=0) + timedelta(hours=3, minutes=15)
    await _call(hass, until=wanted.isoformat())
    assert service.async_lock_platform.await_args.args[2] == wanted


async def test_service_until_without_offset_is_local_time(hass, service):
    wanted = (dt_util.now() + timedelta(hours=2)).replace(microsecond=0, tzinfo=None)
    await _call(hass, until=wanted.isoformat())
    until = service.async_lock_platform.await_args.args[2]
    assert until.tzinfo is not None
    assert until.replace(tzinfo=None) == wanted


async def test_service_refuses_both_and_the_past(hass, service):
    with pytest.raises(ServiceValidationError, match="not both"):
        await _call(hass, duration_hours=2, until=(dt_util.now() + timedelta(hours=1)).isoformat())
    with pytest.raises(ServiceValidationError, match="future"):
        await _call(hass, until=(dt_util.now() - timedelta(hours=1)).isoformat())
    with pytest.raises(ServiceValidationError, match="168 hours"):
        await _call(hass, until=(dt_util.now() + timedelta(hours=169)).isoformat())
    service.async_lock_platform.assert_not_awaited()


async def test_service_answers_with_prior_and_applied(hass, service):
    response = await _call(hass, response=True, duration_hours=2)
    assert response["prior"]["type"] == "none"
    assert response["applied"]["type"] == "blockUntil"
    assert await _call(hass, name="unlock_platform", response=True) == {
        "prior": None, "applied": {}
    }
    # Without asking for it, as existing callers do, nothing changes.
    assert await _call(hass, duration_hours=2) is None


async def test_a_lagging_read_after_our_write_is_not_a_parent_change(coordinator, mobile):
    """Microsoft's GET lags behind its writes; our write's answer stands for 2 minutes."""
    parent_until = _hours(1)
    parent = [{"overrideType": "BlockUntil", "validUntil": iso_utc(parent_until)}]
    mobile.platforms["Windows"] = list(parent)
    await coordinator.async_lock_platform(CHILD, "Windows", _hours(3))

    # The GET still lists the parent's lock it had before ours.
    mobile.platforms["Windows"] = list(parent)
    current = await coordinator._async_read_platform_override(CHILD, "Windows")
    assert current["type"] == "blockUntil"
    assert parse_utc(current["valid_until"]) != parse_utc(parent[0]["validUntil"])

    # So unlock knows the lock as ours and puts the parent's lock back...
    result = await coordinator.async_unlock_platform(CHILD, "Windows")
    assert result["applied"]["type"] == "blockUntil"
    assert mobile.writes[-1] == ("Windows", iso_utc(parent_until))

    # ...and a poll that still shows our (longer) lock does not count either.
    stale = SimpleNamespace(user_id=CHILD, blocked_platforms=["Windows"])
    assert coordinator._merge_blocked_platforms(stale) == ["Windows"]  # the parent's lock


async def test_a_lagging_read_after_a_cancel_is_not_an_error(coordinator, mobile):
    await coordinator.async_lock_platform(CHILD, "Windows", _hours(2))
    mobile.lag_after_cancel = True
    result = await coordinator.async_unlock_platform(CHILD, "Windows")
    assert result["applied"]["type"] == "none"
    assert coordinator.platform_hold(CHILD, "Windows") is None
    # The poll still lists the lock: not believed, the switch stays off.
    stale = SimpleNamespace(user_id=CHILD, blocked_platforms=["Windows"])
    coordinator.data["accounts"][CHILD]["blocked_platforms"] = (
        coordinator._merge_blocked_platforms(stale)
    )
    assert coordinator.data["accounts"][CHILD]["blocked_platforms"] == []
    assert coordinator.platform_hold_state(CHILD, "Windows")["hold"] == "none"
