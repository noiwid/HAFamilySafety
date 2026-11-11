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
