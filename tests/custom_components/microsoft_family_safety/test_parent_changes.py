"""Telling a parent's change in the Family Safety app from our own lock."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.util import dt as dt_util

from custom_components.microsoft_family_safety import switch
from custom_components.microsoft_family_safety.const import DOMAIN
from custom_components.microsoft_family_safety.screen_time import (
    iso_utc,
    parse_utc,
    parse_xbox_override,
)
from custom_components.microsoft_family_safety.xbox_family_api import (
    OVERRIDE_ALLOW,
    OVERRIDE_BLOCK,
)

from .test_platform_lock import CHILD, FakeMobile, FakeXbox, _hours, _make, _xbox


@pytest.fixture
def rig(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    xbox = FakeXbox()
    coord = _make(hass, entry, xbox, FakeMobile())
    return coord, entry, xbox


def _poll(coord, xbox):
    """What a poll keeps: the Xbox override xapprove returned."""
    coord.data["accounts"][CHILD]["xbox_override"] = parse_xbox_override(
        {"overrides": xbox.overrides}
    )


async def test_no_lock_is_none(rig):
    coord, _, xbox = rig
    _poll(coord, xbox)
    assert coord.platform_hold_state(CHILD, "Xbox") == {
        "hold": "none", "override": "none", "override_until": None, "allow_until": None,
    }


async def test_our_lock_is_ours(rig):
    coord, _, xbox = rig
    await coord.async_lock_platform(CHILD, "Xbox", _hours(2))
    _poll(coord, xbox)
    state = coord.platform_hold_state(CHILD, "Xbox")
    assert state["hold"] == "ours"
    assert state["override"] == "blockUntil"
    until = parse_utc(coord.platform_hold(CHILD, "Xbox")["until"])
    assert state["override_until"] == dt_util.as_local(until).isoformat()
    assert state["allow_until"] is None


@pytest.mark.parametrize(
    ("overrides", "override"),
    [
        (lambda: [], "none"),  # released in the app
        (lambda: [_xbox(OVERRIDE_BLOCK, _hours(9))], "blockUntil"),  # another end
        (lambda: [_xbox(OVERRIDE_ALLOW, _hours(1))], "allowUntil"),  # extra time
    ],
)
async def test_a_parent_change_to_our_lock_is_parent(rig, overrides, override):
    coord, _, xbox = rig
    await coord.async_lock_platform(CHILD, "Xbox", _hours(2))
    xbox.overrides = overrides()
    _poll(coord, xbox)
    state = coord.platform_hold_state(CHILD, "Xbox")
    assert (state["hold"], state["override"]) == ("parent", override)
    if override == "allowUntil":
        assert state["allow_until"] == state["override_until"]
        assert dt_util.parse_datetime(state["allow_until"]).tzinfo is not None


async def test_a_parent_lock_without_ours_is_parent(rig):
    coord, _, xbox = rig
    xbox.overrides = [_xbox(OVERRIDE_BLOCK, _hours(1))]
    _poll(coord, xbox)
    assert coord.platform_hold_state(CHILD, "Xbox")["hold"] == "parent"


async def test_an_unread_xbox_with_our_hold_is_ours(rig):
    coord, _, _ = rig
    await coord.async_lock_platform(CHILD, "Xbox", _hours(2))
    coord.data["accounts"][CHILD]["xbox_override"] = None  # no xapprove answer
    state = coord.platform_hold_state(CHILD, "Xbox")
    assert (state["hold"], state["override"]) == ("ours", None)


async def test_windows_uses_the_polled_lock_flag(rig):
    coord, _, _ = rig
    account = coord.data["accounts"][CHILD]
    account["blocked_platforms"] = ["Windows"]
    assert coord.platform_hold_state(CHILD, "Windows")["hold"] == "parent"
    await coord.async_lock_platform(CHILD, "Windows", _hours(2))
    assert coord.platform_hold_state(CHILD, "Windows")["hold"] == "ours"
    account["blocked_platforms"] = []  # cancelled in the app
    assert coord.platform_hold_state(CHILD, "Windows")["hold"] == "parent"


async def test_hold_is_on_the_lock_switch(rig):
    coord, entry, xbox = rig
    await coord.async_lock_platform(CHILD, "Xbox", _hours(2))
    _poll(coord, xbox)
    lock = switch.FamilySafetyPlatformLockSwitch(coord, entry, CHILD, "Alex", "Xbox")
    attrs = lock.extra_state_attributes
    assert attrs["hold"] == "ours"
    assert attrs["override"] == "blockUntil"
    assert {"override_until", "allow_until"} <= set(attrs)


async def test_unlock_left_parent_override_says_what_is_in_force(rig):
    coord, _, xbox = rig
    await coord.async_lock_platform(CHILD, "Xbox", _hours(2))
    extra_until = _hours(1)
    xbox.overrides = [_xbox(OVERRIDE_ALLOW, extra_until)]

    result = await coord.async_unlock_platform(CHILD, "Xbox")

    assert result["applied"] == "left_parent_override"
    current = result["current"]
    assert current["type"] == "allowUntil"
    assert parse_utc(current["valid_until"]) == parse_utc(iso_utc(extra_until))
    assert current["allow_until"] == current["override_until"]


async def test_lock_switch_attributes_for_windows_and_xbox(rig):
    """Every attribute an automation reads to tell who decides the lock."""
    coord, entry, xbox = rig
    # The roster keys children by int; the attribute is still a str.
    coord.data["accounts"] = {int(CHILD): coord.data["accounts"][CHILD]}
    coord.data["accounts"][int(CHILD)]["blocked_platforms"] = ["Windows"]
    xbox.overrides = [_xbox(OVERRIDE_ALLOW, _hours(1))]
    coord.data["accounts"][int(CHILD)]["xbox_override"] = parse_xbox_override(
        {"overrides": xbox.overrides}
    )
    windows = switch.FamilySafetyPlatformLockSwitch(coord, entry, int(CHILD), "Alex", "Windows")
    xbox_lock = switch.FamilySafetyPlatformLockSwitch(coord, entry, int(CHILD), "Alex", "Xbox")

    attrs = windows.extra_state_attributes
    assert attrs["user_id"] == CHILD
    assert attrs["platform"] == "Windows"
    assert (attrs["hold"], attrs["override"]) == ("parent", "blockUntil")
    assert attrs["override_until"] is None  # the mobile flag has no end
    assert attrs["allow_until"] is None

    attrs = xbox_lock.extra_state_attributes
    assert attrs["user_id"] == CHILD
    assert attrs["platform"] == "Xbox"
    assert (attrs["hold"], attrs["override"]) == ("parent", "allowUntil")
    assert attrs["allow_until"] == attrs["override_until"]
    assert dt_util.parse_datetime(attrs["allow_until"]).tzinfo is not None

    # Unreadable: override is None, nothing held, so nobody decides.
    coord.data["accounts"][int(CHILD)]["xbox_override"] = None
    attrs = xbox_lock.extra_state_attributes
    assert (attrs["hold"], attrs["override"], attrs["override_until"]) == ("none", None, None)


async def test_entities_are_told_once_the_hold_is_saved(rig):
    """The write refreshes before the hold is confirmed; a last push follows.

    The push also carries the lock state the write left, not the last poll's.
    """
    coord, _, _ = rig
    seen: list[tuple[bool | None, bool]] = []

    def listener() -> None:
        hold = coord.platform_hold(CHILD, "Xbox")
        blocked = "Xbox" in (coord.data["accounts"][CHILD].get("blocked_platforms") or [])
        seen.append((None if hold is None else bool(hold.get("confirmed")), blocked))

    unsub = coord.async_add_listener(listener)
    try:
        await coord.async_lock_platform(CHILD, "Xbox", _hours(2))
        assert seen and seen[-1] == (True, True)
        seen.clear()
        await coord.async_unlock_platform(CHILD, "Xbox")
        assert seen and seen[-1][1] is False
    finally:
        unsub()
