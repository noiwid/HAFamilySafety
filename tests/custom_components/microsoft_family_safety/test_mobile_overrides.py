"""Windows/Mobile lock and unlock on the /v4 mobile device-limits overrides.

A write is judged by the overrides document the POST answers with: a GET
right after it can lag (still listing an override the Cancel removed). Only
when the POST fails (Microsoft has answered HTTP 500 to overrides it applied)
or answers no document do retried reads decide; a cancel that did not apply
is never reported as an unlock.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from custom_components.microsoft_family_safety import _pyfamilysafety_compat as compat
from custom_components.microsoft_family_safety.api_client import (
    FamilySafetyWebAPI,
    FamilySafetyWebAPIError,
)
from custom_components.microsoft_family_safety.const import DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)
from custom_components.microsoft_family_safety.mobile_overrides import (
    mobile_blocked_platforms,
)

CHILD = "1234567890123456"
ERROR_500 = 'API request failed with status 500: "Internal Server Error"'


def _iso(when: datetime) -> str:
    return dt_util.as_utc(when).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


class FakeMobileOverrides:
    """The /v4 overrides as Microsoft keeps them; the POST can fail or not apply."""

    has_web_cookies = False

    def __init__(self, applies_to: str = "Windows") -> None:
        self.applies_to = applies_to
        self.overrides: list[dict] = []
        self.posts: list[tuple[str, str, datetime]] = []
        self.reads = 0
        self.post_error: Exception | None = None
        self.applies = True
        #: A GET lists what was there before the last write for this many reads.
        self.lag_reads = 0
        self._stale: list[dict] | None = None
        #: The POST answers no body (None) instead of the document.
        self.empty_answer = False

    def state(self, overrides: list[dict] | None = None) -> dict:
        overrides = self.overrides if overrides is None else overrides
        return {
            "lockablePlatforms": [
                {"appliesTo": self.applies_to, "overrides": list(overrides), "devices": []},
                {"appliesTo": "Xbox", "overrides": [], "devices": []},
            ]
        }

    async def get_mobile_device_overrides(self, child_id: str) -> dict:
        assert child_id == CHILD
        self.reads += 1
        if self._stale is not None and self.lag_reads > 0:
            self.lag_reads -= 1
            return self.state(self._stale)
        return self.state()

    async def set_mobile_device_override(self, child_id, target, override_type, until):
        assert child_id == CHILD
        self.posts.append((target, override_type, until))
        self._stale = list(self.overrides)
        if self.applies:
            if override_type == "BlockUntil":
                self.overrides = [{"overrideType": "BlockUntil", "validUntil": _iso(until)}]
            elif override_type == "Cancel":
                self.overrides = []
        if self.post_error is not None:
            raise self.post_error
        return None if self.empty_answer else self.state()


@pytest.fixture
async def coordinator(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.web_api = FakeMobileOverrides()
    coord._MOBILE_READBACK_DELAYS = (0.0, 0.0, 0.0)
    coord.async_request_refresh = AsyncMock()
    coord._accounts[CHILD] = MagicMock(blocked_platforms=[])
    return coord


async def test_lock_sends_block_until_and_reads_it_back(coordinator):
    web = coordinator.web_api
    until = dt_util.utcnow() + timedelta(minutes=3)
    await coordinator.async_lock_platform(CHILD, "Windows", until)
    assert web.posts == [("Windows", "BlockUntil", until)]
    assert web.reads == 1  # the target name; the POST answer decides
    assert coordinator._accounts[CHILD].blocked_platforms == ["Windows"]


async def test_target_name_comes_from_applies_to(coordinator):
    web = coordinator.web_api
    web.applies_to = "Desktop"
    await coordinator.async_lock_platform(CHILD, "Windows")
    assert web.posts[-1][:2] == ("Desktop", "BlockUntil")
    assert coordinator._accounts[CHILD].blocked_platforms == ["Windows"]
    await coordinator.async_unlock_platform(CHILD, "Windows")
    assert web.posts[-1][:2] == ("Desktop", "Cancel")
    assert coordinator._accounts[CHILD].blocked_platforms == []


async def test_unlock_sends_cancel(coordinator):
    web = coordinator.web_api
    web.overrides = [{"overrideType": "BlockUntil", "validUntil": _iso(dt_util.utcnow() + timedelta(hours=1))}]
    await coordinator.async_unlock_platform(CHILD, "Windows")
    assert [p[:2] for p in web.posts] == [("Windows", "Cancel")]
    assert web.overrides == []
    assert coordinator._accounts[CHILD].blocked_platforms == []


async def test_cancel_answered_500_but_applied_is_an_unlock(coordinator):
    web = coordinator.web_api
    web.overrides = [{"overrideType": "BlockUntil", "validUntil": _iso(dt_util.utcnow() + timedelta(hours=1))}]
    web.post_error = FamilySafetyWebAPIError(ERROR_500)
    await coordinator.async_unlock_platform(CHILD, "Windows")
    assert coordinator._accounts[CHILD].blocked_platforms == []


async def test_cancel_answered_500_and_not_applied_raises(coordinator):
    web = coordinator.web_api
    web.overrides = [{"overrideType": "BlockUntil", "validUntil": _iso(dt_util.utcnow() + timedelta(hours=1))}]
    web.post_error = FamilySafetyWebAPIError(ERROR_500)
    web.applies = False
    coordinator._platform_override_until[(CHILD, "windows")] = dt_util.utcnow() + timedelta(hours=1)
    with pytest.raises(HomeAssistantError, match="still lists a lock"):
        await coordinator.async_unlock_platform(CHILD, "Windows")
    assert web.reads == 1 + 3  # the target, then three reads that never settle
    assert coordinator._accounts[CHILD].blocked_platforms == ["Windows"]
    assert (CHILD, "windows") in coordinator._platform_override_until


async def test_lock_accepted_but_not_applied_raises(coordinator):
    coordinator.web_api.applies = False
    with pytest.raises(HomeAssistantError, match="lists no lock"):
        await coordinator.async_lock_platform(CHILD, "Windows")


async def test_platform_not_listed_sends_nothing(coordinator):
    web = coordinator.web_api
    web.applies_to = "Mobile"
    with pytest.raises(HomeAssistantError, match="no Windows device limits"):
        await coordinator.async_lock_platform(CHILD, "Windows")
    assert web.posts == []


async def test_target_read_timeout_gives_a_reason_and_sends_nothing(coordinator):
    web = coordinator.web_api
    web.get_mobile_device_overrides = AsyncMock(side_effect=asyncio.TimeoutError())
    with pytest.raises(HomeAssistantError, match="nothing was sent: no answer in time"):
        await coordinator.async_lock_platform(CHILD, "Windows")
    assert web.posts == []


async def test_failed_read_back_after_a_500_raises(coordinator):
    web = coordinator.web_api
    web.post_error = FamilySafetyWebAPIError(ERROR_500)
    reads = web.get_mobile_device_overrides

    async def second_read_fails(child_id):
        if web.reads >= 1:
            raise FamilySafetyWebAPIError("down")
        return await reads(child_id)

    web.get_mobile_device_overrides = second_read_fails
    with pytest.raises(HomeAssistantError, match="could not be read back"):
        await coordinator.async_unlock_platform(CHILD, "Windows")


def test_blocked_platforms_ignore_extra_time_cancel_and_expired():
    now = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
    later = "2026-01-10T13:00:00.000+0000"
    earlier = "2026-01-10T11:00:00Z"
    state = {
        "lockablePlatforms": [
            {"appliesTo": "Desktop", "overrides": [{"overrideType": "AllowUntil", "validUntil": later}]},
            {"appliesTo": "Mobile", "overrides": [{"overrideType": "BlockUntil", "validUntil": earlier}]},
            {"appliesTo": "Xbox", "overrides": [{"overrideType": "Cancel", "validUntil": later}]},
        ]
    }
    assert mobile_blocked_platforms(state, now) == []
    state["lockablePlatforms"][0]["overrides"].append(
        {"overrideType": "BlockUntil", "validUntil": later}
    )
    state["lockablePlatforms"][1]["overrides"].append({"overrideType": "SomethingNew"})
    assert mobile_blocked_platforms(state, now) == ["Windows", "Mobile"]
    assert mobile_blocked_platforms(None, now) == []


async def test_api_client_sends_the_v4_requests():
    api = FamilySafetyWebAPI(MagicMock())
    api._request = AsyncMock(return_value={"lockablePlatforms": []})
    await api.get_mobile_device_overrides(CHILD)
    api._request.assert_awaited_with(
        "GET",
        f"/v4/devicelimits/{CHILD}/overrides",
        extra_headers={"Plat-Info": "ALL"},
    )
    until = datetime(2026, 1, 10, 13, 4, 5, tzinfo=timezone(timedelta(hours=1)))
    await api.set_mobile_device_override(CHILD, "Desktop", "BlockUntil", until)
    api._request.assert_awaited_with(
        "POST",
        f"/v4/devicelimits/{CHILD}/overrides",
        json_data={
            "target": "Desktop",
            "overrideType": "BlockUntil",
            "validUntil": "2026-01-10T12:04:05.000+0000",
            "culture": "en-US",
        },
        extra_headers={"Plat-Info": "ALL"},
    )


async def test_poll_reads_the_v4_overrides():
    from pyfamilysafety import const as pfs_const
    from pyfamilysafety.account import Account

    compat._patch_v4_overrides_read()
    endpoint = compat._V4_OVERRIDES_ENDPOINT
    assert pfs_const.ENDPOINTS[endpoint]["url"].endswith("/v4/devicelimits/{USER_ID}/overrides")
    later = _iso(dt_util.utcnow() + timedelta(hours=1))
    device = MagicMock(device_id="abc")
    account = MagicMock(user_id=CHILD, devices=[device])
    account._api.send_request = AsyncMock(
        return_value={
            "json": {
                "lockablePlatforms": [
                    {
                        "appliesTo": "Desktop",
                        "overrides": [{"overrideType": "BlockUntil", "validUntil": later}],
                        "devices": [{"deviceId": "g:abc"}, {"deviceId": "g:gone"}],
                    },
                    {"appliesTo": "Xbox", "overrides": [], "devices": []},
                ]
            }
        }
    )
    await Account._get_overrides(account)
    account._api.send_request.assert_awaited_once_with(
        endpoint=endpoint, platform="ALL", USER_ID=CHILD
    )
    assert account.blocked_platforms == ["Windows"]
    device.update_blocked_status.assert_called_once_with(True)


async def test_post_answer_decides_while_the_get_lags(coordinator):
    """The Cancel's answer lists nothing; a GET still lists the lock: unlocked."""
    web = coordinator.web_api
    await coordinator.async_lock_platform(CHILD, "Windows")
    web.lag_reads = 5
    await coordinator.async_unlock_platform(CHILD, "Windows")
    assert web.overrides == []
    assert coordinator._accounts[CHILD].blocked_platforms == []
    assert web.reads == 2  # one target read per action, no read-back


async def test_cancel_500_is_judged_by_retried_reads(coordinator):
    """After a 500 the GET may lag too: read again until it settles."""
    web = coordinator.web_api
    web.overrides = [{"overrideType": "BlockUntil", "validUntil": _iso(dt_util.utcnow() + timedelta(hours=1))}]
    web.post_error = FamilySafetyWebAPIError(ERROR_500)
    web.lag_reads = 2
    await coordinator.async_unlock_platform(CHILD, "Windows")
    assert web.reads == 1 + 3  # target, two lagging reads, the settled one
    assert coordinator._accounts[CHILD].blocked_platforms == []


async def test_no_answer_body_falls_back_to_reads(coordinator):
    web = coordinator.web_api
    web.empty_answer = True
    await coordinator.async_lock_platform(CHILD, "Windows")
    assert web.reads == 2
    assert coordinator._accounts[CHILD].blocked_platforms == ["Windows"]


async def test_lagging_poll_does_not_undo_our_write(coordinator):
    """For two minutes after our Cancel, a poll still listing the lock is not believed."""
    web = coordinator.web_api
    await coordinator.async_lock_platform(CHILD, "Windows")
    await coordinator.async_unlock_platform(CHILD, "Windows")
    stale = MagicMock(user_id=CHILD, blocked_platforms=["Windows"])
    assert coordinator._merge_blocked_platforms(stale) == []
    # The next lock's target read sees our Cancel, not the lagging lock.
    web.lag_reads = 5
    entry_read = await coordinator._async_read_mobile_overrides(CHILD)
    assert entry_read["lockablePlatforms"][0]["overrides"] == []
    # After the grace period the poll is believed again.
    key = (CHILD, "Windows")
    written, entry = coordinator._mobile_writes[key]
    coordinator._mobile_writes[key] = (written - timedelta(minutes=3), entry)
    assert coordinator._merge_blocked_platforms(stale) == ["Windows"]
    assert key not in coordinator._mobile_writes


async def test_post_timeout_is_judged_by_reads(coordinator):
    """A POST that times out may still have been applied: read back."""
    web = coordinator.web_api
    web.post_error = asyncio.TimeoutError()
    await coordinator.async_lock_platform(CHILD, "Windows")
    assert web.reads == 2  # the target, then a read that shows the lock
    assert coordinator._accounts[CHILD].blocked_platforms == ["Windows"]


async def test_post_timeout_not_applied_raises(coordinator):
    web = coordinator.web_api
    web.post_error = asyncio.TimeoutError()
    web.applies = False
    with pytest.raises(HomeAssistantError, match="no answer in time"):
        await coordinator.async_lock_platform(CHILD, "Windows")
    assert web.reads == 1 + 3


async def test_no_answer_body_and_no_read_raises(coordinator):
    """No document from the POST and three failed reads: not a success."""
    web = coordinator.web_api
    web.empty_answer = True
    reads = web.get_mobile_device_overrides

    async def later_reads_fail(child_id):
        if web.reads >= 1:
            web.reads += 1
            raise FamilySafetyWebAPIError("down")
        return await reads(child_id)

    web.get_mobile_device_overrides = later_reads_fail
    with pytest.raises(HomeAssistantError, match="without saying what changed"):
        await coordinator.async_lock_platform(CHILD, "Windows")
    assert web.reads == 1 + 3
    coordinator.async_request_refresh.assert_awaited()


def _read_backs_answer(web, answer):
    """The target read gives the document; every read after it gives ``answer``."""
    reads = web.get_mobile_device_overrides

    async def later_reads(child_id):
        if web.reads >= 1:
            web.reads += 1
            return answer
        return await reads(child_id)

    web.get_mobile_device_overrides = later_reads


@pytest.mark.parametrize("answer", [{}, {"error": "x"}])
async def test_unlock_read_back_without_the_document_is_not_a_success(coordinator, answer):
    """{} lists no lock, but it is not Microsoft saying the Cancel applied."""
    web = coordinator.web_api
    web.overrides = [{"overrideType": "BlockUntil", "validUntil": _iso(dt_util.utcnow() + timedelta(hours=1))}]
    web.post_error = FamilySafetyWebAPIError(ERROR_500)
    web.applies = False
    coordinator._accounts[CHILD].blocked_platforms = ["Windows"]
    _read_backs_answer(web, answer)
    with pytest.raises(HomeAssistantError, match="could not be read back"):
        await coordinator.async_unlock_platform(CHILD, "Windows")
    assert web.reads == 1 + 3
    assert coordinator._accounts[CHILD].blocked_platforms == ["Windows"]


@pytest.mark.parametrize("answer", [{}, {"error": "x"}])
async def test_lock_read_back_without_the_document_is_not_read(coordinator, answer):
    web = coordinator.web_api
    web.post_error = asyncio.TimeoutError()
    _read_backs_answer(web, answer)
    with pytest.raises(HomeAssistantError, match="could not be read back"):
        await coordinator.async_lock_platform(CHILD, "Windows")
    assert web.reads == 1 + 3


@pytest.mark.parametrize("answer", [None, "<html></html>", {}, {"error": "x"}, []])
async def test_poll_without_the_document_keeps_the_last_locks(answer):
    from pyfamilysafety.account import Account

    compat._patch_v4_overrides_read()
    device = MagicMock(device_id="abc")
    account = MagicMock(user_id=CHILD, devices=[device], blocked_platforms=["Windows"])
    account._api.send_request = AsyncMock(return_value={"json": answer})
    await Account._get_overrides(account)
    assert account.blocked_platforms == ["Windows"]
    device.update_blocked_status.assert_not_called()


OTHER_CHILD = "6543210987654321"


def _roster_account(user_id, answer):
    """A pyfamilysafety Account whose overrides read answers ``answer``."""
    from pyfamilysafety.account import Account

    account = Account(MagicMock())
    account.user_id = user_id
    account.get_screentime_usage = AsyncMock()
    account._get_devices = AsyncMock()
    account._get_applications = AsyncMock()
    account._get_account_balance = AsyncMock()
    account._api.send_request = AsyncMock(return_value={"json": answer})
    return account


@pytest.mark.parametrize("answer", [None, {}])
async def test_first_poll_without_the_document_affects_only_that_child(answer):
    """A 204 (no body) or {} on the first poll: that child [] + a roster error."""
    compat._patch_v4_overrides_read()
    compat._patch_account_roster_tolerance()
    later = _iso(dt_util.utcnow() + timedelta(hours=1))
    locked = {
        "lockablePlatforms": [
            {
                "appliesTo": "Windows",
                "overrides": [{"overrideType": "BlockUntil", "validUntil": later}],
                "devices": [],
            }
        ]
    }
    child = _roster_account(CHILD, locked)
    other = _roster_account(OTHER_CHILD, answer)

    await asyncio.gather(child.update(), other.update())

    assert child.blocked_platforms == ["Windows"]
    assert child.roster_errors == {}
    assert other.blocked_platforms == []
    assert set(other.roster_errors) == {"overrides"}

    # The same answer on every later poll still never fails the update.
    for _ in range(2):
        await asyncio.gather(child.update(), other.update())
        assert other.blocked_platforms == []
        assert set(other.roster_errors) == {"overrides"}
        assert child.roster_errors == {}

    # Once the document comes, the child's locks are read and the error clears.
    other._api.send_request.return_value = {"json": locked}
    await other.update()
    assert other.blocked_platforms == ["Windows"]
    assert other.roster_errors == {}

    # From then on a missing document keeps the last locks, as before.
    other._api.send_request.return_value = {"json": answer}
    await other.update()
    assert other.blocked_platforms == ["Windows"]
    assert other.roster_errors == {}
