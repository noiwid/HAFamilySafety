"""The screen time cards find a device's entities from a single entity of the device."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.microsoft_family_safety import card

ENTRY = "entry1"
CHILD = 1234  # the roster gives int ids
PC = "g:pc1"
XBOX = "g:xbox1"
PHONE = "g:phone1"
DAYS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]


def _registry():
    def entry(entity_id, suffix, platform="microsoft_family_safety", config_entry_id=ENTRY):
        return SimpleNamespace(
            entity_id=entity_id, unique_id=f"{config_entry_id}_{suffix}",
            platform=platform, config_entry_id=config_entry_id,
        )

    entries = [
        entry("sensor.pc_screen_time", f"{PC}_screentime"),
        entry("sensor.pc_info", f"{PC}_info"),
        entry("sensor.xbox_screen_time", f"{XBOX}_screentime"),
        entry("sensor.phone_screen_time", f"{PHONE}_screentime"),
        entry("sensor.alex_screen_time", f"{CHILD}_screentime"),
        entry("sensor.alex_xbox_screen_time", f"{CHILD}_xbox_screentime"),
        entry("sensor.alex_windows_time_remaining", f"{CHILD}_windows_time_remaining"),
        entry("sensor.alex_xbox_time_remaining", f"{CHILD}_xbox_time_remaining"),
        entry("switch.alex_windows_lock", f"{CHILD}_platform_windows"),
        entry("switch.alex_xbox_lock", f"{CHILD}_platform_xbox"),
        entry("switch.alex_lock", f"{CHILD}_account_lock"),
        entry("button.alex_approve", f"{CHILD}_approve_request"),
        entry("sensor.connection", "connection"),
        entry("switch.not_ours", f"{PC}_screentime", platform="other"),
        *(entry(f"number.alex_{d}_limit", f"{CHILD}_limit_{d}") for d in DAYS),
        *(entry(f"number.alex_xbox_{d}_limit", f"{CHILD}_xbox_limit_{d}") for d in DAYS),
    ]
    by_id = {e.entity_id: e for e in entries}
    by_uid = {(e.entity_id.split(".")[0], e.platform, e.unique_id): e.entity_id for e in entries}
    registry = MagicMock()
    registry.async_get = by_id.get
    registry.async_get_entity_id = lambda domain, platform, uid: by_uid.get((domain, platform, uid))
    return registry


def _hass():
    devices = {
        PC: {"account_id": CHILD, "os_name": "Windows", "device_class": "PC"},
        XBOX: {"account_id": CHILD, "os_name": "Xbox", "device_class": "Console"},
        PHONE: {"account_id": CHILD, "os_name": "Android", "device_class": "Mobile"},
    }
    coordinator = SimpleNamespace(data={"accounts": {CHILD: {}}, "devices": devices})
    return SimpleNamespace(data={"microsoft_family_safety": {ENTRY: coordinator}})


def _cards(entity_id):
    with patch.object(card.er, "async_get", return_value=_registry()):
        return card.card_entities(_hass(), entity_id)


def test_a_pc_gets_its_time_and_the_childs_windows_roles():
    assert _cards("sensor.pc_info") == {
        "device": {
            "daily_screen_time": "sensor.pc_screen_time",
            "screen_time_remaining": "sensor.alex_windows_time_remaining",
            "lock": "switch.alex_windows_lock",
        },
        "child": {
            "daily_limit_1": "number.alex_monday_limit",
            "daily_limit_2": "number.alex_tuesday_limit",
            "daily_limit_3": "number.alex_wednesday_limit",
            "daily_limit_4": "number.alex_thursday_limit",
            "daily_limit_5": "number.alex_friday_limit",
            "daily_limit_6": "number.alex_saturday_limit",
            "daily_limit_7": "number.alex_sunday_limit",
        },
        "locked_when": "on",
    }


def test_an_xbox_gets_the_xbox_roles():
    result = _cards("sensor.xbox_screen_time")
    assert result["device"] == {
        "daily_screen_time": "sensor.alex_xbox_screen_time",
        "screen_time_remaining": "sensor.alex_xbox_time_remaining",
        "lock": "switch.alex_xbox_lock",
    }
    assert result["child"]["daily_limit_7"] == "number.alex_xbox_sunday_limit"


def test_the_child_device_is_refused_with_a_reason():
    # its time is the total of the PC and the Xbox: taking it with them would count it twice
    with pytest.raises(card.ChildAccountDevice):
        _cards("button.alex_approve")
    connection = MagicMock()
    with patch.object(card.er, "async_get", return_value=_registry()):
        card.ws_card_entities(_hass(), connection, {"id": 8, "entity_id": "sensor.alex_screen_time"})
    assert connection.send_error.call_args.args == (8, "not_found", card.CHILD_ACCOUNT_MESSAGE)
    connection.send_result.assert_not_called()


def test_another_device_only_has_its_time():
    assert _cards("sensor.phone_screen_time") == {
        "device": {"daily_screen_time": "sensor.phone_screen_time"}, "child": {}, "locked_when": "on",
    }


def test_unknown_foreign_or_timeless_entity():
    assert _cards("light.kitchen") is None
    assert _cards("switch.not_ours") is None
    assert _cards("sensor.connection") is None


def test_websocket_refuses_a_foreign_entity():
    connection = MagicMock()
    with patch.object(card.er, "async_get", return_value=_registry()):
        card.ws_card_entities(_hass(), connection, {"id": 7, "entity_id": "switch.not_ours"})
    connection.send_error.assert_called_once()
    assert connection.send_error.call_args.args[:2] == (7, "not_found")
    connection.send_result.assert_not_called()


async def test_registered_once():
    hass = MagicMock()
    hass.data = {}
    hass.http.async_register_static_paths = AsyncMock()
    with (
        patch.object(card, "async_get_integration", AsyncMock(return_value=SimpleNamespace(version="9.9.9"))),
        patch.object(card, "add_extra_js_url") as add_js,
        patch.object(card.websocket_api, "async_register_command") as add_ws,
    ):
        await card.async_register_card(hass)
        await card.async_register_card(hass)
    assert [c.args[1] for c in add_js.call_args_list] == [
        "/familysafety_card/parental-screen-time.js?v=9.9.9",
        "/familysafety_card/familysafety-card.js?v=9.9.9",
    ]
    add_ws.assert_called_once()
    hass.http.async_register_static_paths.assert_awaited_once()
