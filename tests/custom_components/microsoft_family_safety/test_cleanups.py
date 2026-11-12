"""Small fixes: sensor attributes, dead schedule write, restore-point accessor."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.microsoft_family_safety import sensor
from custom_components.microsoft_family_safety.const import DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)

CHILD = "1234567890123456"


def _coordinator(hass) -> FamilySafetyDataUpdateCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    return FamilySafetyDataUpdateCoordinator(hass, entry)


async def test_screen_time_sensors_carry_no_state_class_attribute(hass) -> None:
    coord = _coordinator(hass)
    coord.data = {
        "accounts": {CHILD: {"first_name": "Alex", "today_screentime_usage": 5}},
        "devices": {"dev1": {"device_name": "PC", "today_time_used": 3}},
    }
    account = sensor.FamilySafetyScreenTimeSensor(coord, coord.entry, CHILD)
    device = sensor.FamilySafetyDeviceScreenTimeSensor(coord, coord.entry, "dev1")

    for entity in (account, device):
        attrs = entity.extra_state_attributes
        # It overrode the entity's real state class in the state attributes.
        assert "state_class" not in attrs
        assert attrs["date"]


async def test_saved_schedule_accessor_normalises_the_id(hass) -> None:
    coord = _coordinator(hass)
    coord._saved_screentime = {CHILD: {"dailyRestrictions": {}}}

    assert coord.has_saved_screentime(int(CHILD))
    assert coord.has_saved_screentime(CHILD)
    assert not coord.has_saved_screentime("42")


async def test_block_and_unblock_keep_the_app_name() -> None:
    """pyfamilysafety sent the app id as displayName, renaming the app."""
    from unittest.mock import AsyncMock

    from pyfamilysafety.application import Application

    from custom_components.microsoft_family_safety._pyfamilysafety_compat import (
        _patch_app_display_name,
    )

    _patch_app_display_name()
    api = AsyncMock()
    app = Application(api, "1234567890123456")
    app.app_id = "x:xboxplt:0123456789abcdef"
    app.name = "Example Game"

    await app.block_app()
    body = api.send_request.await_args.kwargs["body"]
    assert body["displayName"] == "Example Game"
    assert body["blockState"] == "BlockedAlways"
    assert api.send_request.await_args.kwargs["platform"] == "XBOX"
    assert app.blocked is True

    await app.unblock_app()
    body = api.send_request.await_args.kwargs["body"]
    assert body["displayName"] == "Example Game"
    assert body["blockState"] == "NotBlocked"
    assert app.blocked is False

    app.name = None
    await app.block_app()
    assert api.send_request.await_args.kwargs["body"]["displayName"] == app.app_id
