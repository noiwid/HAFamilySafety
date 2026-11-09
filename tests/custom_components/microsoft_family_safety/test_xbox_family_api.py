"""Xbox settings through the Xbox Family Settings app's API.

The request shapes come from a capture of the Xbox Family Settings iOS app
(2026-11-06) and a read-only run of the same token chain against a real
account, also 40 h after the sign-in.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.util import dt as dt_util

from custom_components.microsoft_family_safety.const import (
    CONF_WEB_COOKIES,
    CONF_XBOX_FEATURES,
    DOMAIN,
)
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)
from custom_components.microsoft_family_safety.xbox_family_api import (
    OVERRIDE_BLOCK,
    XboxFamilyAPI,
    XboxFamilyAuthError,
    overrides_to_policy,
    schedule_to_policy,
    usage_today,
)

CHILD = "914798558090500"
XUID = "2535449443639838"
PARENT = "2535416680601644"
TOKEN_URL = "https://login.live.com/oauth20_token.srf"
XAPPROVE = f"https://xapprove.xboxlive.com/api/v1/user/{XUID}/settings/screentime"


def _day(index: int, hour: int, minute: int = 0) -> dict:
    return {
        "dayOfWeek": index,
        "allowance": {"days": 0, "hour": hour, "minute": minute},
        "allowedIntervals": [
            {"begin": {"days": 0, "hour": 7, "minute": 0}, "end": {"days": 0, "hour": 22, "minute": 0}}
        ],
    }


SCHEDULE = {
    "isEnabled": True,
    "isGlobal": False,
    "dailyRestrictions": [_day(0, 0, 30), _day(1, 0), _day(2, 0, 30), _day(3, 0),
                          _day(4, 4, 30), _day(5, 1), _day(6, 0, 45)],
}


# --- conversions -------------------------------------------------------------
def test_schedule_converts_to_the_entity_shape():
    policy = schedule_to_policy(SCHEDULE)
    assert policy["isEnabled"] is True
    assert policy["dailyRestrictions"]["sunday"]["allowance"] == "00:30:00"
    assert policy["dailyRestrictions"]["thursday"]["allowance"] == "04:30:00"
    assert policy["dailyRestrictions"]["friday"]["allowedIntervals"] == [
        {"begin": "07:00", "end": "22:00"}
    ]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ([{"overrideType": 2, "validUntil": "2026-11-08T18:45:45Z"}], "blockUntil"),
        ([{"overrideType": 1, "validUntil": "2026-11-06T20:00:53Z"}], "allowUntil"),
        ([{"overrideType": 1}, {"overrideType": 2, "validUntil": "x"}], "blockUntil"),
        ([{"overrideType": 7}], "xapprove_7"),
    ],
)
def test_overrides_map_to_the_web_policy_shape(overrides, expected):
    policy = overrides_to_policy({"overrides": overrides})
    assert policy["screenTimeOverride"]["screenTimeOverrideType"] == expected


def test_no_override_reads_as_not_blocked():
    assert overrides_to_policy({"overrides": []}) == {"dailyRestrictions": {}}


def test_usage_today():
    report = {"dailyScreenTimeEvents": [
        {"date": "2026-11-01T00:00:00", "timeUsed": "00:49:00"},
        {"date": "2026-11-08T00:00:00", "timeUsed": "01:05:40"},
    ]}
    assert usage_today(report, "2026-11-08") == 66
    assert usage_today(report, "2026-11-09") == 0
    assert usage_today({}, "2026-11-08") is None


# --- token chain and requests -------------------------------------------------
def _mock_auth(aioclient_mock) -> None:
    aioclient_mock.post(
        TOKEN_URL,
        json={"access_token": "msa-access", "refresh_token": "rotated", "expires_in": 86400},
    )
    aioclient_mock.post(
        "https://user.auth.xboxlive.com/user/authenticate",
        json={"Token": "user-token", "NotAfter": "2099-01-01T00:00:00Z"},
    )
    aioclient_mock.post(
        "https://xsts.auth.xboxlive.com/xsts/authorize",
        json={
            "Token": "xsts-token",
            "NotAfter": "2099-01-01T00:00:00Z",
            "DisplayClaims": {"xui": [{"uhs": "12345", "xid": PARENT}]},
        },
    )


async def _linked_api(hass, hass_storage) -> XboxFamilyAPI:
    hass_storage[f"{DOMAIN}.xbox.entry"] = {
        "version": 1, "key": f"{DOMAIN}.xbox.entry", "data": {"refresh_token": "stored"}
    }
    api = XboxFamilyAPI(hass, "entry")
    await api.async_load()
    return api


async def test_read_uses_xsts_and_rps_ticket(hass, hass_storage, aioclient_mock):
    _mock_auth(aioclient_mock)
    aioclient_mock.get(XAPPROVE, json=SCHEDULE)
    api = await _linked_api(hass, hass_storage)
    assert await api.async_get_schedule(XUID) == SCHEDULE
    method, url, data, headers = aioclient_mock.mock_calls[-1]
    assert headers["Authorization"] == "XBL3.0 x=12345;xsts-token"
    assert headers["x-ms-rps-ticket"] == "msa-access"
    user_auth = next(c for c in aioclient_mock.mock_calls if "user.auth" in str(c[1]))
    assert user_auth[2]["Properties"]["RpsTicket"] == "t=msa-access"
    assert api.state == "ok"
    # The rotated refresh token is kept for the next start.
    assert hass_storage[f"{DOMAIN}.xbox.entry"]["data"]["refresh_token"] == "rotated"


async def test_revoked_sign_in_raises_auth_error(hass, hass_storage, aioclient_mock):
    aioclient_mock.post(TOKEN_URL, status=400, json={"error": "invalid_grant"})
    api = await _linked_api(hass, hass_storage)
    with pytest.raises(XboxFamilyAuthError):
        await api.async_get_schedule(XUID)
    assert api.state == "auth_failed"


async def test_child_matched_by_name(hass, hass_storage, aioclient_mock):
    _mock_auth(aioclient_mock)
    aioclient_mock.get(
        f"https://accounts.xboxlive.com/family/memberXuid({PARENT})",
        json={"familyUsers": [
            {"firstName": "Pat", "lastName": "Doe", "xuid": PARENT, "role": "Admin"},
            {"firstName": "Sam", "lastName": "Doe", "xuid": XUID, "role": "Child"},
        ]},
    )
    api = await _linked_api(hass, hass_storage)
    assert await api.async_child_xuid("sam", "Doe") == XUID
    assert await api.async_child_xuid("Pat", "Doe") is None  # not a child
    family_call = next(c for c in aioclient_mock.mock_calls if "accounts.xboxlive" in str(c[1]))
    assert family_call[3]["x-xbl-contract-version"] == "2"
    assert "x-ms-rps-ticket" not in family_call[3]


async def test_set_day_patches_one_day_keeping_its_windows(hass, hass_storage, aioclient_mock):
    _mock_auth(aioclient_mock)
    aioclient_mock.get(XAPPROVE, json=SCHEDULE)
    aioclient_mock.patch(XAPPROVE, json=[])
    api = await _linked_api(hass, hass_storage)
    await api.async_set_day(XUID, 5, minutes=75)
    body = next(c for c in aioclient_mock.mock_calls if c[0] == "PATCH")[2]
    assert body == {
        "isEnabled": True,
        "dailyRestrictions": [{
            "dayOfWeek": 5,
            "allowance": {"hour": 1, "minute": 15},
            "allowedIntervals": [{
                "begin": {"days": 0, "hour": 7, "minute": 0},
                "end": {"days": 0, "hour": 22, "minute": 0},
            }],
        }],
    }


async def test_create_override_body(hass, hass_storage, aioclient_mock):
    _mock_auth(aioclient_mock)
    aioclient_mock.post(f"{XAPPROVE}/overrides/create", json={"overrides": []})
    api = await _linked_api(hass, hass_storage)
    until = dt_util.parse_datetime("2026-11-08T18:45:45+00:00")
    await api.async_create_override(XUID, OVERRIDE_BLOCK, until)
    body = aioclient_mock.mock_calls[-1][2]
    assert body == {"overrideType": 2, "target": 1, "validUntil": "2026-11-08T18:45:45.000+00:00"}


# --- coordinator --------------------------------------------------------------
@pytest.fixture
async def coordinator(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"refresh_token": "x", CONF_WEB_COOKIES: [{"name": "MSPAuth"}]},
        options={CONF_XBOX_FEATURES: True},
    )
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.xbox_family = MagicMock(linked=True, ready=True, usable=True)
    coord.xbox_family.async_child_xuid = AsyncMock(return_value=XUID)
    coord.xbox_family.async_get_schedule = AsyncMock(return_value=SCHEDULE)
    coord.xbox_family.async_get_overrides = AsyncMock(
        return_value={"overrides": [{"overrideType": 2, "validUntil": "2099-01-01T00:00:00Z"}]}
    )
    coord.xbox_family.async_get_usage = AsyncMock(return_value={"dailyScreenTimeEvents": []})
    coord.xbox_family.async_set_day = AsyncMock()
    coord.xbox_family.async_create_override = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    coord._accounts = {CHILD: SimpleNamespace(user_id=CHILD, first_name="Sam", surname="Doe")}
    coord.web_api = MagicMock()
    return coord


async def test_poll_reads_xbox_from_xapprove(coordinator):
    account_data: dict = {"xbox_screentime_policy": {"source": "mobile"}}
    result = await coordinator._async_fetch_xapprove(coordinator._accounts[CHILD], account_data)
    assert account_data["xbox_screentime_policy"]["source"] == "xapprove"
    assert account_data["xbox_screentime_policy"]["dailyRestrictions"]["friday"]["allowance"] == "01:00:00"
    assert result["usage"] == 0
    assert result["lock_policy"]["screenTimeOverride"]["screenTimeOverrideType"] == "blockUntil"
    coordinator._apply_xbox_lock_state(CHILD, account_data, result["lock_policy"])
    assert "Xbox" in account_data["blocked_platforms"]


async def test_poll_falls_back_when_not_linked(coordinator):
    coordinator.xbox_family.linked = False
    assert await coordinator._async_fetch_xapprove(coordinator._accounts[CHILD], {}) is None


async def test_xbox_limit_writes_go_to_xapprove(coordinator):
    await coordinator.async_set_xbox_screentime_limit(CHILD, 5, 1, 15)
    coordinator.xbox_family.async_set_day.assert_awaited_once_with(XUID, 5, minutes=75)
    coordinator.web_api.set_mobile_xbox_screentime_allowance.assert_not_called()


@pytest.mark.parametrize("passed_id", [CHILD, int(CHILD)])
async def test_writes_find_the_child_whatever_the_id_type(coordinator, passed_id):
    # The roster keys accounts by int user_id; entities and services pass either.
    coordinator._accounts = {int(CHILD): coordinator._accounts[CHILD]}
    await coordinator.async_set_xbox_screentime_limit(passed_id, 0, 0, 30)
    coordinator.xbox_family.async_set_day.assert_awaited_once_with(XUID, 0, minutes=30)


async def test_xbox_lock_through_xapprove(coordinator):
    coordinator.xbox_family.async_create_override.return_value = {
        "overrides": [{"overrideType": 2, "validUntil": "2099-01-01T00:00:00Z"}]
    }
    await coordinator.async_lock_platform(CHILD, "Xbox", dt_util.utcnow() + timedelta(hours=1))
    assert coordinator.xbox_family.async_create_override.await_args.args[1] == OVERRIDE_BLOCK
    assert (CHILD, "xbox") in coordinator._platform_override_until
    coordinator.web_api.set_platform_time_override.assert_not_called()


async def test_xbox_unlock_falls_back_to_web_when_block_remains(coordinator):
    coordinator.xbox_family.async_create_override.return_value = {
        "overrides": [{"overrideType": 2, "validUntil": "2099-01-01T00:00:00Z"}]
    }
    coordinator._has_web_credentials = True
    coordinator.web_api.has_web_cookies = True
    coordinator.web_api.set_platform_time_override = AsyncMock()
    await coordinator.async_unlock_platform(CHILD, "Xbox")
    assert coordinator.web_api.set_platform_time_override.await_args.args[2] == "cancel"


@pytest.mark.parametrize(
    ("state", "usable"), [("ok", True), ("linked", True), ("error", True), ("auth_failed", False)]
)
async def test_only_a_refused_token_makes_xapprove_unusable(hass, state, usable):
    api = XboxFamilyAPI(hass, "entry")
    api._refresh_token = "token"
    api.state = state
    assert api.usable is usable
