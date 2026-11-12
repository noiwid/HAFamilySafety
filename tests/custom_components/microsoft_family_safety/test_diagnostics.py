"""Diagnostics never carry tokens, cookies, names, e-mail addresses, browsing data or ids."""

from __future__ import annotations

import json

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.microsoft_family_safety.const import DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)
from custom_components.microsoft_family_safety.diagnostics import (
    async_get_config_entry_diagnostics,
)

CHILD = "1234567890123456"
DEVICE = "abcdef-0123"
XUID = "2533274800000001"
PARENT = "6543210987654321"
SECRETS = [
    "refresh-secret", "cookie-secret", "family-token-secret", "api-key-secret",
    "xbox-refresh-secret", CHILD, DEVICE, XUID, PARENT, "Alex", "Rivera",
    "alex@example.com", "https://pics.example/alex.png", "blocked.example",
    "visited.example", "how to beat level 3", "Alex's laptop", "12.34",
]


async def test_diagnostics_are_redacted(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "refresh_token": "refresh-secret",
            "web_cookies": [{"name": "MSPAuth", "value": "cookie-secret"}],
            "web_family_token": "family-token-secret",
            "auth_user_id": PARENT,
        },
        options={"api_key": "api-key-secret", "update_interval": 300},
    )
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.xbox_family._refresh_token = "xbox-refresh-secret"
    coord._xbox_xuids = {CHILD: XUID}
    coord.data = {
        "accounts": {int(CHILD): {
            "user_id": int(CHILD),
            "first_name": "Alex",
            "surname": "Rivera",
            "profile_picture": "https://pics.example/alex.png",
            "account_balance": 12.34,
            "devices": [DEVICE],
            "applications": [{"app_id": "appx:x", "app_name": "X"}],
            "web_browsing": {"enabled": True, "exceptions": [{"website": "https://blocked.example"}]},
            "web_activity": {
                "domains": [{"domain": "visited.example", "visits": 3}],
                "searches": [{"search": "how to beat level 3"}],
            },
            "xbox_screentime_policy": {"xuid": XUID, "endpoint": f"/users/{PARENT}/{XUID}"},
            "note": "contact alex@example.com",
        }},
        "devices": {DEVICE: {"device_name": "Alex's laptop", "account_id": int(CHILD)}},
        "pending_requests": [{"puid": CHILD}],
    }
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord

    result = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(result)

    for secret in SECRETS:
        assert secret not in text, secret
    account = result["data"]["accounts"]["account_1"]
    assert account["application_count"] == 1
    assert account["user_id"] == "account_1"
    assert account["devices"] == ["device_1"]
    assert account["xbox_screentime_policy"]["endpoint"] == "/users/parent/xbox_user_1"
    assert result["data"]["devices"]["device_1"]["account_id"] == "account_1"
    assert result["data"]["pending_request_count"] == 1
    assert result["entry"]["options"]["update_interval"] == 300
    assert "state" in result["connection"]


async def test_diagnostics_without_a_running_coordinator(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"refresh_token": "refresh-secret", "auth_user_id": PARENT}
    )
    entry.add_to_hass(hass)

    result = await async_get_config_entry_diagnostics(hass, entry)

    text = json.dumps(result)
    assert "refresh-secret" not in text and PARENT not in text
    assert "connection" not in result


PARENT_XUID = "2533274800000099"


async def test_parent_xuid_in_an_xbox_error_is_aliased(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "refresh-secret"})
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.xbox_family._parent_xuid = PARENT_XUID
    coord.xbox_family.last_error = f"GET /family/memberXuid({PARENT_XUID}) answered 403"
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert PARENT_XUID not in json.dumps(result)
    assert result["connection"]["xbox_family_last_error"] == (
        "GET /family/memberXuid(xbox_parent) answered 403"
    )


async def test_ids_are_aliased_before_the_first_poll_succeeds(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "refresh-secret"})
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.data = None
    coord._accounts = {int(CHILD): object()}
    coord._xbox_xuids = {"6543210987650000": XUID}
    real_state = coord.connection_state

    def connection_state():
        return {
            **real_state(),
            "web_api_last_endpoint": f"/api/v1/users/{CHILD}/screentime",
            "roster_errors": {"6543210987650000": ["/xbox/policy"]},
        }

    coord.connection_state = connection_state
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord

    result = await async_get_config_entry_diagnostics(hass, entry)

    text = json.dumps(result)
    assert CHILD not in text and "6543210987650000" not in text
    assert result["connection"]["web_api_last_endpoint"] == "/api/v1/users/account_1/screentime"
    assert result["connection"]["roster_errors"] == {"account_2": ["/xbox/policy"]}
