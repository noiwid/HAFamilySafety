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

CHILD = "1234567890123456"
XUID = "2535400000000001"
PARENT = "2535400000000002"
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


# --- review follow-ups (#69) -------------------------------------------------------
async def test_refused_token_on_the_first_lookup_is_recorded(hass, hass_storage, aioclient_mock):
    # After a restart the first call is the family lookup, outside _request.
    aioclient_mock.post(TOKEN_URL, status=400, json={"error": "invalid_grant"})
    api = await _linked_api(hass, hass_storage)
    with pytest.raises(XboxFamilyAuthError):
        await api.async_child_xuid("Sam", "Doe")
    assert api.state == "auth_failed"
    assert api.usable is False


async def test_relink_forgets_the_previous_account(hass, hass_storage, aioclient_mock):
    aioclient_mock.post(
        TOKEN_URL, json={"access_token": "new", "refresh_token": "new-rt", "expires_in": 86400}
    )
    api = await _linked_api(hass, hass_storage)
    api._parent_xuid = "old-parent"
    api._family = [{"xuid": "old-child"}]
    api._xsts = ("uhs", "old", dt_util.utcnow() + timedelta(hours=1))
    api.state = "auth_failed"
    await api.async_link_from_redirect(
        "https://login.live.com/oauth20_desktop.srf?code=abc", "verifier", "manual"
    )
    assert (api._parent_xuid, api._family, api._xsts) == (None, None, None)
    assert api.state == "linked"


async def test_allow_until_override_reads_as_unlocked(coordinator):
    account_data: dict = {}
    policy = overrides_to_policy({"overrides": [{"overrideType": 1, "validUntil": "2099-01-01T00:00:00Z"}]})
    coordinator._platform_override_until[(CHILD, "xbox")] = dt_util.utcnow() + timedelta(hours=1)
    coordinator._apply_xbox_lock_state(CHILD, account_data, policy)
    assert "Xbox" not in account_data["blocked_platforms"]
    assert account_data["xbox_lock_known"] is True


@pytest.mark.parametrize(
    ("xapprove", "expected"),
    [
        (None, (True, True)),
        ({"usage": 12, "lock_policy": {}}, (False, False)),
        ({"usage": None, "lock_policy": {}}, (False, True)),  # only usage falls back
    ],
)
async def test_web_gates(coordinator, xapprove, expected):
    coordinator.web_api = SimpleNamespace(
        has_web_cookies=True, family_context_state="ready", _family_web_backoff_until=0.0
    )
    assert coordinator._xbox_web_gates(xapprove) == expected


async def test_child_match_is_counted_and_not_logged_by_name(coordinator, caplog):
    caplog.set_level("INFO")
    await coordinator._async_xbox_xuid(coordinator._accounts[CHILD])
    coordinator.xbox_family.async_child_xuid = AsyncMock(return_value=None)
    await coordinator._async_xbox_xuid(SimpleNamespace(user_id=42, first_name="Kim", surname="Doe"))
    info = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert info and not any("Sam" in m or XUID in m for m in info)
    assert len(coordinator._xbox_xuids) == 1
    assert coordinator._xbox_unmatched == {"42"}


async def test_manual_link_is_used_without_a_reload(hass, coordinator):
    coordinator.xbox_family.async_link_from_redirect = AsyncMock()
    coordinator._xbox_xuids = {"stale": "x"}
    coordinator.async_request_refresh = AsyncMock()
    hass.data.setdefault(DOMAIN, {})[coordinator.entry.entry_id] = coordinator
    result = await hass.config_entries.options.async_init(coordinator.entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"update_interval": 300, "platforms": ["Windows", "Xbox"], "xbox_link": True},
    )
    # No usable Home Assistant URL for the sign-in proxy here: paste the redirect
    assert result["step_id"] == "xbox_link_manual"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"redirect_url": "https://login.live.com/oauth20_desktop.srf?code=abc"},
    )
    await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    coordinator.xbox_family.async_link_from_redirect.assert_awaited_once()
    assert coordinator._xbox_xuids == {}
    coordinator.async_request_refresh.assert_awaited()


async def test_proxy_link_is_used_without_a_reload(hass, coordinator):
    """The whole options flow: the proxy captures the code, the flow finishes."""
    from unittest.mock import patch

    coordinator.xbox_family.async_link_from_redirect = AsyncMock()
    coordinator.async_request_refresh = AsyncMock()
    hass.data.setdefault(DOMAIN, {})[coordinator.entry.entry_id] = coordinator
    flow_module = "custom_components.microsoft_family_safety.config_flow"
    proxy_module = "custom_components.microsoft_family_safety.auth.native_proxy"
    with patch(f"{flow_module}._options_browser_hass_url", return_value="https://ha.example"), patch(
        f"{proxy_module}.create_async_httpx_client", return_value=MagicMock()
    ), patch(f"{proxy_module}.register_native_proxy"), patch(
        f"{proxy_module}.unregister_native_proxy", new=AsyncMock()
    ) as close:
        result = await hass.config_entries.options.async_init(coordinator.entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"update_interval": 300, "platforms": ["Windows", "Xbox"], "xbox_link": True},
        )
        assert result["type"] == "progress"
        assert result["step_id"] == "xbox_link"
        flow = hass.config_entries.options._progress[result["flow_id"]]
        proxy = flow._xbox_proxy
        proxy._oauth_redirect_url = "https://login.live.com/oauth20_desktop.srf?code=abc"
        proxy._complete = True
        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_configure(result["flow_id"])
        await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    coordinator.xbox_family.async_link_from_redirect.assert_awaited_once()
    assert coordinator.xbox_family.async_link_from_redirect.await_args.args[2] == "proxy"
    coordinator.async_request_refresh.assert_awaited()
    close.assert_awaited_once()


async def test_removing_the_entry_deletes_the_xbox_token(hass, hass_storage):
    from custom_components.microsoft_family_safety import async_remove_entry

    entry = MockConfigEntry(domain=DOMAIN, data={})
    hass_storage[f"{DOMAIN}.xbox.{entry.entry_id}"] = {
        "version": 1, "key": f"{DOMAIN}.xbox.{entry.entry_id}", "data": {"refresh_token": "rt"}
    }
    await async_remove_entry(hass, entry)
    await hass.async_block_till_done()
    assert f"{DOMAIN}.xbox.{entry.entry_id}" not in hass_storage
