"""Strict mode puts back a lock of ours that Microsoft reports as cancelled."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from homeassistant.util import dt as dt_util

from custom_components.microsoft_family_safety.const import (
    CONF_STRICT_MODE,
    DOMAIN,
    EVENT_LOCK_REASSERTED,
)
from custom_components.microsoft_family_safety.screen_time import iso_utc
from custom_components.microsoft_family_safety.xbox_family_api import OVERRIDE_BLOCK

from .test_platform_lock import CHILD, FakeMobile, FakeXbox, _make

NONE = {"type": "none", "valid_until": None}


@pytest.fixture
def xbox():
    return FakeXbox()


@pytest.fixture
def mobile():
    return FakeMobile()


def _coordinator(hass, xbox, mobile, strict: bool):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"refresh_token": "x"}, options={CONF_STRICT_MODE: strict}
    )
    entry.add_to_hass(hass)
    return _make(hass, entry, xbox, mobile)


async def _locked(coord, xbox, mobile, platform="Xbox", age=timedelta(minutes=11)):
    """A lock of ours, placed ``age`` ago (past the cooldown by default)."""
    await coord.async_lock_platform(CHILD, platform, dt_util.utcnow() + timedelta(hours=2))
    hold = coord._platform_holds[coord._hold_key(CHILD, platform)]
    hold["confirmed_at"] = iso_utc(dt_util.utcnow() - age)
    # Our mobile write's answer is that old too (see _MOBILE_WRITE_GRACE).
    for key, (written, entry) in list(coord._mobile_writes.items()):
        coord._mobile_writes[key] = (written - age, entry)
    xbox.writes.clear()
    mobile.writes.clear()


async def test_cancelled_lock_is_placed_again_with_an_event(hass, xbox, mobile):
    coord = _coordinator(hass, xbox, mobile, strict=True)
    await _locked(coord, xbox, mobile)
    events = async_capture_events(hass, EVENT_LOCK_REASSERTED)
    until = coord.platform_hold(CHILD, "Xbox")["until"]

    await coord._async_enforce_holds({CHILD: NONE})
    await hass.async_block_till_done()

    assert xbox.writes == [(OVERRIDE_BLOCK, until)]
    assert len(events) == 1
    assert events[0].data["account_id"] == CHILD
    assert events[0].data["platform"] == "Xbox"
    assert events[0].data["until"] == until

    # Within the cooldown nothing is sent again.
    await coord._async_enforce_holds({CHILD: NONE})
    await hass.async_block_till_done()
    assert len(xbox.writes) == 1


async def test_strict_mode_off_changes_nothing(hass, xbox, mobile):
    coord = _coordinator(hass, xbox, mobile, strict=False)
    await _locked(coord, xbox, mobile)
    await coord._async_enforce_holds({CHILD: NONE})
    await hass.async_block_till_done()
    assert xbox.writes == []


@pytest.mark.parametrize("state", [None, {"type": "blockUntil", "valid_until": "x"}])
async def test_unknown_or_still_blocked_is_left_alone(hass, xbox, mobile, state):
    coord = _coordinator(hass, xbox, mobile, strict=True)
    await _locked(coord, xbox, mobile)
    await coord._async_enforce_holds({CHILD: state} if state else {})
    await hass.async_block_till_done()
    assert xbox.writes == []


async def test_unconfirmed_or_expired_holds_are_left_alone(hass, xbox, mobile):
    coord = _coordinator(hass, xbox, mobile, strict=True)
    await _locked(coord, xbox, mobile)
    key = coord._hold_key(CHILD, "Xbox")
    coord._platform_holds[key]["confirmed"] = False
    await coord._async_enforce_holds({CHILD: NONE})
    coord._platform_holds[key]["confirmed"] = True
    coord._platform_holds[key]["until"] = iso_utc(dt_util.utcnow() - timedelta(minutes=1))
    await coord._async_enforce_holds({CHILD: NONE})
    await hass.async_block_till_done()
    assert xbox.writes == []


async def test_unlocked_lock_is_not_placed_again(hass, xbox, mobile):
    coord = _coordinator(hass, xbox, mobile, strict=True)
    await _locked(coord, xbox, mobile)
    await coord.async_unlock_platform(CHILD, "Xbox")
    xbox.writes.clear()
    await coord._async_enforce_holds({CHILD: NONE})
    await hass.async_block_till_done()
    assert xbox.writes == []


async def test_windows_lock_is_read_back_from_the_mobile_api(hass, xbox, mobile):
    coord = _coordinator(hass, xbox, mobile, strict=True)
    await _locked(coord, xbox, mobile, "Windows")
    mobile.platforms["Windows"] = []  # cancelled elsewhere

    await coord._async_enforce_holds({})
    await hass.async_block_till_done()

    assert [name for name, _ in mobile.writes] == ["Windows"]


async def test_a_read_taken_before_the_lock_does_not_count(hass, xbox, mobile):
    coord = _coordinator(hass, xbox, mobile, strict=True)
    await _locked(coord, xbox, mobile)
    confirmed = dt_util.parse_datetime(coord.platform_hold(CHILD, "Xbox")["confirmed_at"])
    read_at = confirmed - timedelta(seconds=5)  # the poll read xapprove before the lock

    await coord._async_enforce_holds({CHILD: NONE}, {CHILD: read_at})
    await hass.async_block_till_done()
    assert xbox.writes == []

    # A read taken after the lock is believed.
    await coord._async_enforce_holds({CHILD: NONE}, {CHILD: dt_util.utcnow()})
    await hass.async_block_till_done()
    assert [kind for kind, _ in xbox.writes] == [OVERRIDE_BLOCK]


async def test_the_poll_hands_the_xapprove_override_over(hass, xbox, mobile):
    """The Xbox read-back is the xapprove answer, a release reading as "none"."""
    coord = _coordinator(hass, xbox, mobile, strict=True)
    xbox.async_get_schedule = _answer({"isEnabled": True, "dailyRestrictions": []})
    xbox.async_get_usage = _answer({"dailyScreenTimeEvents": []})
    xbox.overrides = [{"overrideType": 100, "validUntil": iso_utc(dt_util.utcnow() + timedelta(minutes=5))}]
    account = SimpleNamespace(user_id=CHILD, first_name="Alex", surname="Rivera")

    result = await coord._async_fetch_xapprove(account, {})

    assert result["override"] == NONE


def _answer(value):
    async def answer(*args, **kwargs):
        return value
    return answer


async def test_no_relock_right_after_our_own_write(hass, xbox, mobile):
    """A read that does not show a fresh lock yet waits for the cooldown."""
    coord = _coordinator(hass, xbox, mobile, strict=True)
    await _locked(coord, xbox, mobile, "Windows", age=timedelta(minutes=2))
    mobile.platforms["Windows"] = []  # the read misses our lock

    await coord._async_enforce_holds({})
    await hass.async_block_till_done()
    assert mobile.writes == []

    hold = coord._platform_holds[coord._hold_key(CHILD, "Windows")]
    hold["confirmed_at"] = iso_utc(dt_util.utcnow() - timedelta(minutes=11))
    await coord._async_enforce_holds({})
    await hass.async_block_till_done()
    assert [name for name, _ in mobile.writes] == ["Windows"]


async def test_a_superseded_hold_is_not_placed_again(hass, xbox, mobile):
    coord = _coordinator(hass, xbox, mobile, strict=True)
    await _locked(coord, xbox, mobile)
    coord._platform_holds[coord._hold_key(CHILD, "Xbox")]["superseded"] = True
    await coord._async_enforce_holds({CHILD: NONE})
    await hass.async_block_till_done()
    assert xbox.writes == []
