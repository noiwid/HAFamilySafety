"""Account lock, unlock and the limits switch never lose a child's schedule.

Microsoft has no lock endpoint: a lock zeroes the 7 days and the restore point
kept by the coordinator is the only way back. These tests drive the coordinator
with a fake web API that records every write, and check the guards of
issue #23 plus the ones added after the 2026-11 audit.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.microsoft_family_safety.const import DAY_KEYS, DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)

CHILD = "1234567890123456"
SLOTS = [False] * 14 + [True] * 30 + [False] * 4  # 07:00-22:00


def _policy(allowance: str, *, enabled: bool = True) -> dict:
    return {
        "isEnabled": enabled,
        "dailyRestrictions": {
            key: {"allowance": allowance, "timeline": list(SLOTS)} for key in DAY_KEYS
        },
    }


class FakeWebAPI:
    """Records the writes; each one can be told to fail."""

    def __init__(self) -> None:
        self.allowances: list[tuple[int, int, int]] = []
        self.intervals: list[tuple[int, list[bool]]] = []
        self.enabled_calls: list[bool] = []
        self.fail = False
        self.last_web_error_code = None

    async def set_mobile_schedule_enabled(self, child_id, enabled, platform="Windows"):
        self.enabled_calls.append(enabled)
        return True


@pytest.fixture
async def coordinator(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    web = FakeWebAPI()
    coord.web_api = web
    coord._async_save_screentime = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    coord._fetch_screentime_policy = AsyncMock(return_value=_policy("02:00:00"))
    coord.data = {"accounts": {CHILD: {"screentime_policy": _policy("02:00:00")}}}

    async def set_allowance(account_id, day, hours, minutes):
        if web.fail:
            raise RuntimeError("down")
        web.allowances.append((day, hours, minutes))

    async def set_intervals(account_id, day, slots):
        web.intervals.append((day, list(slots)))

    coord._set_screentime_allowance = set_allowance
    coord._set_screentime_intervals = set_intervals
    return coord


async def test_lock_refuses_without_readable_schedule_or_restore_point(coordinator):
    coordinator._fetch_screentime_policy.return_value = None
    with pytest.raises(UpdateFailed):
        await coordinator.async_lock_account(CHILD)
    assert coordinator.web_api.allowances == []


async def test_lock_then_unlock_restores_the_exact_schedule(coordinator):
    await coordinator.async_lock_account(CHILD)
    assert coordinator.web_api.allowances == [(d, 0, 0) for d in range(7)]
    assert CHILD in coordinator._saved_screentime

    # Locking again reads an all-zero schedule: the good restore point stays.
    coordinator._fetch_screentime_policy.return_value = _policy("00:00:00")
    await coordinator.async_lock_account(CHILD)
    assert coordinator._saved_screentime[CHILD] == {**_policy("02:00:00"), "_saved_by": "lock"}

    coordinator.web_api.allowances.clear()
    await coordinator.async_unlock_account(CHILD)
    assert coordinator.web_api.allowances == [(d, 2, 0) for d in range(7)]
    assert coordinator.web_api.intervals[-1] == (6, SLOTS)
    assert CHILD not in coordinator._saved_screentime


async def test_unlock_refuses_to_invent_a_schedule(coordinator):
    with pytest.raises(UpdateFailed):
        await coordinator.async_unlock_account(CHILD)
    broken = _policy("02:00:00")
    broken["dailyRestrictions"]["monday"] = {"allowance": None}
    coordinator._saved_screentime[CHILD] = broken
    with pytest.raises(UpdateFailed):
        await coordinator.async_unlock_account(CHILD)
    assert coordinator.web_api.allowances == []
    assert CHILD in coordinator._saved_screentime


async def test_lock_refuses_a_schedule_it_could_not_restore(coordinator):
    # A day without intervals, or a .NET "1.00:00:00" day: the lock must not
    # zero a week it could not read back; the latter must simply be readable.
    unreadable = _policy("02:00:00")
    del unreadable["dailyRestrictions"]["friday"]["timeline"]
    coordinator._fetch_screentime_policy.return_value = unreadable
    with pytest.raises(UpdateFailed):
        await coordinator.async_lock_account(CHILD)
    assert coordinator.web_api.allowances == []

    coordinator._fetch_screentime_policy.return_value = _policy("1.00:00:00")
    await coordinator.async_lock_account(CHILD)
    coordinator.web_api.allowances.clear()
    await coordinator.async_unlock_account(CHILD)
    assert coordinator.web_api.allowances == [(d, 24, 0) for d in range(7)]


async def test_lock_turns_the_schedule_flag_on_and_unlock_puts_it_back(coordinator):
    coordinator.data["accounts"][CHILD]["screentime_policy"]["isEnabled"] = False
    coordinator._fetch_screentime_policy.return_value = _policy("02:00:00", enabled=False)
    await coordinator.async_lock_account(CHILD)
    assert coordinator.web_api.enabled_calls == [True]
    coordinator.data["accounts"][CHILD]["screentime_policy"]["isEnabled"] = True
    await coordinator.async_unlock_account(CHILD)
    assert coordinator.web_api.enabled_calls == [True, False]


async def test_enable_limits_does_not_unlock_a_locked_child(coordinator):
    """Turning limits on (an automation every morning...) sets the flag, the lock stays."""
    await coordinator.async_lock_account(CHILD)
    coordinator.web_api.allowances.clear()
    await coordinator.async_set_policy_enabled(CHILD, True)
    assert coordinator.web_api.enabled_calls[-1] is True
    assert coordinator.web_api.allowances == []
    assert CHILD in coordinator._saved_screentime
    await coordinator.async_unlock_account(CHILD)
    assert coordinator.web_api.allowances == [(d, 2, 0) for d in range(7)]


async def test_enable_limits_restores_what_the_disable_fallback_saved(coordinator):
    coordinator.web_api.set_mobile_schedule_enabled = AsyncMock(side_effect=RuntimeError)
    await coordinator.async_set_policy_enabled(CHILD, False)
    coordinator.web_api.allowances.clear()
    await coordinator.async_set_policy_enabled(CHILD, True)
    assert coordinator.web_api.allowances == [(d, 2, 0) for d in range(7)]


async def test_a_lock_after_the_disable_fallback_keeps_the_real_schedule(coordinator):
    """Audit 2026-11-13: the 24 h week of "limits off" must never replace the saved schedule."""
    coordinator.web_api.set_mobile_schedule_enabled = AsyncMock(side_effect=RuntimeError)
    await coordinator.async_set_policy_enabled(CHILD, False)
    assert coordinator.web_api.allowances[-1] == (6, 24, 0)
    # The next read sees the 24 h week the fallback wrote.
    coordinator._fetch_screentime_policy.return_value = _policy("1.00:00:00")
    await coordinator.async_lock_account(CHILD)
    await coordinator.async_set_policy_enabled(CHILD, False)  # a second "off" while it still fails
    coordinator.web_api.allowances.clear()
    await coordinator.async_unlock_account(CHILD)
    assert coordinator.web_api.allowances == [(d, 2, 0) for d in range(7)]


async def test_a_day_changed_during_a_lock_is_kept_at_unlock(coordinator):
    await coordinator.async_lock_account(CHILD)
    await coordinator.async_set_screentime_limit(CHILD, 1, 3, 0)
    coordinator.web_api.allowances.clear()
    await coordinator.async_unlock_account(CHILD)
    assert (1, 3, 0) in coordinator.web_api.allowances
    assert (2, 2, 0) in coordinator.web_api.allowances


async def test_disable_fallback_never_saves_a_zeroed_schedule(coordinator):
    coordinator.web_api.set_mobile_schedule_enabled = AsyncMock(side_effect=RuntimeError)
    coordinator._fetch_screentime_policy.return_value = _policy("00:00:00")
    with pytest.raises(UpdateFailed):
        await coordinator.async_set_policy_enabled(CHILD, False)
    assert CHILD not in coordinator._saved_screentime
    assert coordinator.web_api.allowances == []


async def test_lock_and_unlock_do_not_interleave(coordinator):
    started = asyncio.Event()
    release = asyncio.Event()
    web = coordinator.web_api

    async def slow_allowance(account_id, day, hours, minutes):
        web.allowances.append((day, hours, minutes))
        started.set()
        await release.wait()

    coordinator._set_screentime_allowance = slow_allowance
    lock_task = asyncio.create_task(coordinator.async_lock_account(CHILD))
    await started.wait()
    unlock_task = asyncio.create_task(coordinator.async_unlock_account(CHILD))
    await asyncio.sleep(0)
    assert all(h == 0 for _, h, _ in web.allowances)  # unlock has not started
    release.set()
    await lock_task
    await unlock_task
    assert [h for _, h, _ in web.allowances] == [0] * 7 + [2] * 7

