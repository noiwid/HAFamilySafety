"""Mobile schedule writes (Windows and Xbox) run one at a time."""
from __future__ import annotations

import asyncio
import copy
from unittest.mock import AsyncMock, Mock

from custom_components.microsoft_family_safety.api_client import FamilySafetyWebAPI
from custom_components.microsoft_family_safety.const import DAY_KEYS

CHILD = "synthetic-child"


def _schedule(platform: str) -> dict:
    return {
        "appliesTo": platform,
        "enabled": True,
        "dailyRestrictions": {
            day: {"allowance": 3_600_000, "allottedIntervals": [{"begin": "08:00:00", "end": "20:00:00"}]}
            for day in DAY_KEYS
        },
    }


class SlowProvider:
    """Fake mobile endpoint that yields on every call and counts overlap."""

    def __init__(self) -> None:
        self.state = {"mode": "PerDeviceType", "schedules": [_schedule("Windows"), _schedule("Xbox")]}
        self.in_flight = 0
        self.max_in_flight = 0
        self.methods: list[str] = []

    async def request(self, method, path, **kwargs):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.methods.append(method)
        try:
            await asyncio.sleep(0.01)
            if method == "GET":
                return copy.deepcopy(self.state)
            body = kwargs["json_data"]
            schedule = next(s for s in self.state["schedules"] if s["appliesTo"] == body["appliesTo"])
            if "dailyRestrictions" in body:
                schedule["dailyRestrictions"].update(copy.deepcopy(body["dailyRestrictions"]))
            if "enabled" in body:
                schedule["enabled"] = body["enabled"]
            return {}
        finally:
            self.in_flight -= 1


async def test_concurrent_schedule_writes_run_one_at_a_time() -> None:
    provider = SlowProvider()
    api = FamilySafetyWebAPI(Mock())
    api._request = AsyncMock(side_effect=provider.request)

    slots = [False] * 16 + [True] * 24 + [False] * 8  # 08:00-20:00
    await asyncio.gather(
        *(api.set_mobile_xbox_screentime_allowance(CHILD, day, 2, day) for day in range(7)),
        api.set_mobile_screentime_allowance(CHILD, 1, 1, 30),
        api.set_mobile_screentime_intervals(CHILD, 2, slots),
        api.set_mobile_schedule_enabled(CHILD, True, platform="Xbox"),
    )

    # Never two requests at once, and every PATCH right after its own GET.
    assert provider.max_in_flight == 1
    assert provider.methods == ["GET", "PATCH"] * 10

    windows, xbox = provider.state["schedules"]
    for day in range(7):
        assert xbox["dailyRestrictions"][DAY_KEYS[day]]["allowance"] == (120 + day) * 60_000
    assert windows["dailyRestrictions"][DAY_KEYS[1]]["allowance"] == 90 * 60_000
    assert windows["dailyRestrictions"][DAY_KEYS[2]]["allottedIntervals"] == [
        {"begin": "08:00:00", "end": "20:00:00"}
    ]


async def test_failed_write_releases_the_lock() -> None:
    provider = SlowProvider()
    api = FamilySafetyWebAPI(Mock())
    api._request = AsyncMock(side_effect=RuntimeError("synthetic failure"))

    try:
        await api.set_mobile_xbox_screentime_allowance(CHILD, 0, 1, 0)
    except RuntimeError:
        pass
    assert not api._mobile_schedule_write_lock.locked()

    api._request = AsyncMock(side_effect=provider.request)
    await asyncio.wait_for(api.set_mobile_xbox_screentime_allowance(CHILD, 0, 1, 0), 1)
    assert provider.methods == ["GET", "PATCH"]
