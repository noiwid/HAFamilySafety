"""lock_platform remembers the lock or extra time in force; unlock_platform puts it back.

The first lock of a platform reads the override Microsoft has in force (a
parent's lock, extra time, or nothing) and keeps it in a Store; a second lock
keeps what the first one saved. unlock_platform puts a saved lock or extra
time back while it has not run out, and cancels otherwise. Windows and Mobile
go through the mobile v4 overrides only, the Xbox through xapprove.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from custom_components.microsoft_family_safety import async_remove_entry
from custom_components.microsoft_family_safety.const import CONF_XBOX_FEATURES, DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    HELD_OVERRIDES_STORAGE_KEY,
    FamilySafetyDataUpdateCoordinator,
)
from custom_components.microsoft_family_safety.xbox_family_api import (
    OVERRIDE_ALLOW,
    OVERRIDE_BLOCK,
    OVERRIDE_REVOKE,
)

CHILD = "1234567890123456"
XUID = "2535400000000001"


def _hours(hours: float) -> datetime:
    return (dt_util.utcnow() + timedelta(hours=hours)).replace(microsecond=0)


#: Fixed ends, so the times compared below are the same instants. The test
#: run takes seconds, so they stay in the future.
PARENT, LOCK, LATER = _hours(1), _hours(3), _hours(5)


def _iso(when: datetime) -> str:
    return dt_util.as_utc(when).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FakeXbox:
    """The Xbox Family Settings service: one override in force, as Microsoft keeps it."""

    linked = ready = usable = True

    def __init__(self) -> None:
        self.overrides: list[dict] = []
        self.writes: list[tuple[int, str]] = []

    async def async_child_xuid(self, first_name, surname):
        return XUID

    async def async_get_overrides(self, xuid):
        return {"overrides": list(self.overrides)}

    async def async_create_override(self, xuid, override_type, valid_until):
        self.writes.append((override_type, _iso(valid_until)))
        self.overrides = (
            []
            if override_type == OVERRIDE_REVOKE
            else [{"overrideType": override_type, "validUntil": _iso(valid_until)}]
        )
        return {"overrides": list(self.overrides)}


class FakeMobile:
    """/v4/devicelimits/{child}/overrides: one override per platform, as Microsoft keeps it."""

    has_web_cookies = False

    def __init__(self) -> None:
        self.platforms: dict[str, list[dict]] = {"Windows": [], "Mobile": [], "Xbox": []}
        self.writes: list[tuple[str, str, str | None]] = []

    def state(self) -> dict:
        return {
            "lockablePlatforms": [
                {"appliesTo": name, "overrides": list(entries), "devices": []}
                for name, entries in self.platforms.items()
            ]
        }

    def put(self, platform: str, override_type: str, until: datetime) -> None:
        self.platforms[platform] = [{"overrideType": override_type, "validUntil": _iso(until)}]

    async def get_mobile_device_overrides(self, child_id):
        assert child_id == CHILD
        return self.state()

    async def set_mobile_device_override(self, child_id, target, override_type, valid_until):
        assert child_id == CHILD
        if override_type == "Cancel":
            self.platforms[target] = []
            self.writes.append((target, "Cancel", None))
        else:
            self.put(target, override_type, valid_until)
            self.writes.append((target, override_type, _iso(valid_until)))
        return self.state()


def _make(hass, entry, xbox: FakeXbox, mobile: FakeMobile) -> FamilySafetyDataUpdateCoordinator:
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.xbox_family = xbox
    coord.web_api = mobile
    coord._accounts[int(CHILD)] = SimpleNamespace(
        user_id=int(CHILD), first_name="Alex", surname="Rivera", blocked_platforms=[],
    )
    coord.async_request_refresh = AsyncMock()
    return coord


@pytest.fixture
def entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"refresh_token": "x"}, options={CONF_XBOX_FEATURES: True}
    )
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


def _saved(hass_storage, entry) -> dict:
    return hass_storage[f"{HELD_OVERRIDES_STORAGE_KEY}.{entry.entry_id}"]["data"]


def _prior(hass_storage, entry, platform: str) -> dict | None:
    return _saved(hass_storage, entry)[f"{CHILD}|{platform.lower()}"]["prior"]


# --- lock: what is saved --------------------------------------------------------------


@pytest.mark.parametrize(
    ("in_force", "kind"), [("BlockUntil", "blockUntil"), ("AllowUntil", "allowUntil")]
)
@pytest.mark.parametrize("platform", ["Windows", "Mobile"])
async def test_lock_saves_the_lock_or_extra_time_in_force(
    hass_storage, entry, coordinator, mobile, platform, in_force, kind
):
    mobile.put(platform, in_force, PARENT)
    await coordinator.async_lock_platform(CHILD, platform, LOCK)

    prior = _prior(hass_storage, entry, platform)
    assert prior["type"] == kind
    assert dt_util.parse_datetime(prior["valid_until"]) == PARENT
    assert mobile.writes == [(platform, "BlockUntil", _iso(LOCK))]


@pytest.mark.parametrize(("in_force", "kind"), [(OVERRIDE_BLOCK, "blockUntil"), (OVERRIDE_ALLOW, "allowUntil")])
async def test_xbox_lock_saves_the_lock_or_extra_time_in_force(
    hass_storage, entry, coordinator, xbox, in_force, kind
):
    xbox.overrides = [{"overrideType": in_force, "validUntil": _iso(PARENT)}]
    await coordinator.async_lock_platform(CHILD, "Xbox", LOCK)

    assert _prior(hass_storage, entry, "Xbox")["type"] == kind
    assert xbox.writes == [(OVERRIDE_BLOCK, _iso(LOCK))]


async def test_lock_with_nothing_in_force_saves_nothing(hass_storage, entry, coordinator, mobile):
    await coordinator.async_lock_platform(CHILD, "Windows", LOCK)
    assert _prior(hass_storage, entry, "Windows") is None


async def test_lock_reads_the_overrides_once(coordinator, mobile):
    mobile.get_mobile_device_overrides = AsyncMock(side_effect=mobile.get_mobile_device_overrides)
    await coordinator.async_lock_platform(CHILD, "Windows", LOCK)
    assert mobile.get_mobile_device_overrides.await_count == 1


async def test_a_second_lock_keeps_the_first_saved_state(hass_storage, entry, coordinator, mobile):
    mobile.put("Windows", "BlockUntil", PARENT)
    await coordinator.async_lock_platform(CHILD, "Windows", LOCK)
    # Microsoft now shows our own lock; the second lock must not save that.
    await coordinator.async_lock_platform(CHILD, "Windows", LATER)

    prior = _prior(hass_storage, entry, "Windows")
    assert prior["type"] == "blockUntil"
    assert dt_util.parse_datetime(prior["valid_until"]) == PARENT
    assert mobile.writes[-1] == ("Windows", "BlockUntil", _iso(LATER))


async def test_a_lock_that_ran_out_on_its_own_starts_over(hass_storage, entry, coordinator, mobile):
    await coordinator.async_lock_platform(CHILD, "Windows", PARENT)
    coordinator._held_overrides[f"{CHILD}|windows"]["lock_until"] = _iso(_hours(-1))
    coordinator._mobile_writes.clear()  # that write is an hour old
    mobile.put("Windows", "AllowUntil", _hours(2))  # extra time given since

    await coordinator.async_lock_platform(CHILD, "Windows", LOCK)

    assert _prior(hass_storage, entry, "Windows")["type"] == "allowUntil"


async def test_a_failed_lock_saves_nothing(hass_storage, entry, coordinator, mobile):
    mobile.put("Windows", "AllowUntil", PARENT)
    mobile.set_mobile_device_override = AsyncMock(return_value=mobile.state())  # not applied
    coordinator._MOBILE_READBACK_DELAYS = (0.0,)
    with pytest.raises(HomeAssistantError):
        await coordinator.async_lock_platform(CHILD, "Windows", LOCK)
    assert f"{HELD_OVERRIDES_STORAGE_KEY}.{entry.entry_id}" not in hass_storage


# --- unlock: put back or cancel -------------------------------------------------------


@pytest.mark.parametrize("platform", ["Windows", "Mobile"])
async def test_unlock_puts_back_a_lock_still_in_force(
    hass_storage, entry, coordinator, mobile, platform
):
    mobile.put(platform, "BlockUntil", PARENT)
    await coordinator.async_lock_platform(CHILD, platform, LOCK)
    await coordinator.async_unlock_platform(CHILD, platform)

    assert mobile.writes[-1] == (platform, "BlockUntil", _iso(PARENT))
    assert _saved(hass_storage, entry) == {}


@pytest.mark.parametrize("platform", ["Windows", "Mobile"])
async def test_unlock_puts_back_extra_time_still_in_force(
    hass_storage, entry, coordinator, mobile, platform
):
    mobile.put(platform, "AllowUntil", PARENT)
    await coordinator.async_lock_platform(CHILD, platform, LOCK)
    await coordinator.async_unlock_platform(CHILD, platform)

    assert mobile.writes[-1] == (platform, "AllowUntil", _iso(PARENT))
    assert mobile.platforms[platform] == [
        {"overrideType": "AllowUntil", "validUntil": _iso(PARENT)}
    ]
    assert _saved(hass_storage, entry) == {}


@pytest.mark.parametrize("in_force", [OVERRIDE_BLOCK, OVERRIDE_ALLOW])
async def test_xbox_unlock_puts_back_a_lock_or_extra_time(coordinator, xbox, in_force):
    xbox.overrides = [{"overrideType": in_force, "validUntil": _iso(PARENT)}]
    await coordinator.async_lock_platform(CHILD, "Xbox", LOCK)
    await coordinator.async_unlock_platform(CHILD, "Xbox")

    assert xbox.writes[-1] == (in_force, _iso(PARENT))
    assert xbox.overrides == [{"overrideType": in_force, "validUntil": _iso(PARENT)}]


@pytest.mark.parametrize("in_force", ["BlockUntil", "AllowUntil"])
async def test_unlock_cancels_when_the_saved_one_has_run_out(
    hass_storage, entry, coordinator, mobile, in_force
):
    mobile.put("Windows", in_force, PARENT)
    await coordinator.async_lock_platform(CHILD, "Windows", LOCK)
    coordinator._held_overrides[f"{CHILD}|windows"]["prior"]["valid_until"] = _iso(_hours(-0.5))

    await coordinator.async_unlock_platform(CHILD, "Windows")

    assert mobile.writes[-1] == ("Windows", "Cancel", None)
    assert mobile.platforms["Windows"] == []
    assert _saved(hass_storage, entry) == {}


async def test_unlock_cancels_when_nothing_was_in_force(coordinator, mobile, xbox):
    await coordinator.async_lock_platform(CHILD, "Windows", LOCK)
    await coordinator.async_unlock_platform(CHILD, "Windows")
    assert mobile.writes[-1] == ("Windows", "Cancel", None)

    await coordinator.async_lock_platform(CHILD, "Xbox", LOCK)
    await coordinator.async_unlock_platform(CHILD, "Xbox")
    assert xbox.writes[-1][0] == OVERRIDE_REVOKE


async def test_unlock_without_a_lock_from_here_cancels(coordinator, mobile):
    mobile.put("Windows", "BlockUntil", PARENT)
    await coordinator.async_unlock_platform(CHILD, "Windows")
    assert mobile.writes == [("Windows", "Cancel", None)]


async def test_a_failed_restore_keeps_the_saved_state_for_a_retry(
    hass_storage, entry, coordinator, mobile
):
    mobile.put("Windows", "AllowUntil", PARENT)
    await coordinator.async_lock_platform(CHILD, "Windows", LOCK)
    write = mobile.set_mobile_device_override
    mobile.set_mobile_device_override = AsyncMock(return_value=mobile.state())  # not applied
    coordinator._MOBILE_READBACK_DELAYS = (0.0,)
    with pytest.raises(HomeAssistantError, match="did not confirm"):
        await coordinator.async_unlock_platform(CHILD, "Windows")
    assert _prior(hass_storage, entry, "Windows")["type"] == "allowUntil"

    mobile.set_mobile_device_override = write
    coordinator._mobile_writes.clear()
    await coordinator.async_unlock_platform(CHILD, "Windows")
    assert mobile.writes[-1] == ("Windows", "AllowUntil", _iso(PARENT))


async def test_the_saved_state_survives_a_restart(hass, hass_storage, entry, coordinator, xbox, mobile):
    mobile.put("Windows", "BlockUntil", PARENT)
    await coordinator.async_lock_platform(CHILD, "Windows", LOCK)

    restarted = _make(hass, entry, xbox, mobile)
    await restarted.async_load_saved_screentime()
    await restarted.async_unlock_platform(CHILD, "Windows")

    assert mobile.writes[-1] == ("Windows", "BlockUntil", _iso(PARENT))


async def test_removing_the_entry_deletes_the_saved_states(hass, hass_storage, entry, coordinator):
    await coordinator.async_lock_platform(CHILD, "Windows", LOCK)
    key = f"{HELD_OVERRIDES_STORAGE_KEY}.{entry.entry_id}"
    assert key in hass_storage
    await async_remove_entry(hass, entry)
    assert key not in hass_storage


# --- Windows and Mobile: the v4 overrides only -----------------------------------------


@pytest.mark.parametrize("platform", ["Windows", "Mobile"])
@pytest.mark.parametrize("in_force", [None, "BlockUntil", "AllowUntil"])
async def test_windows_and_mobile_never_use_the_web_session(
    hass, entry, xbox, mobile, platform, in_force
):
    coord = _make(hass, entry, xbox, mobile)
    web = MagicMock(has_web_cookies=True, family_context_state="ready")
    web.get_mobile_device_overrides = mobile.get_mobile_device_overrides
    web.set_mobile_device_override = mobile.set_mobile_device_override
    coord.web_api = web
    coord._has_web_credentials = True
    assert coord._native_web_auth
    if in_force:
        mobile.put(platform, in_force, PARENT)

    await coord.async_lock_platform(CHILD, platform, LOCK)
    await coord.async_unlock_platform(CHILD, platform)

    assert web.method_calls == []
    assert [w[1] for w in mobile.writes] == ["BlockUntil", in_force or "Cancel"]
