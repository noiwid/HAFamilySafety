"""Web filter (Microsoft Edge): on/off switch, site lists, several sites per call."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.microsoft_family_safety import _register_services, sensor, switch
from custom_components.microsoft_family_safety.api_client import (
    FamilySafetyWebAPI,
    FamilySafetyWebAPIError,
)
from custom_components.microsoft_family_safety.const import DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)
from custom_components.microsoft_family_safety.site_sync import site_lists

CHILD = "1234567890123456"


class FakeWebAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    async def block_website(self, child_id, website):
        self.calls.append(("block", child_id, website))

    async def allow_website(self, child_id, website):
        self.calls.append(("allow", child_id, website))

    async def remove_website(self, child_id, website):
        self.calls.append(("remove", child_id, website))

    async def toggle_web_filter(self, child_id, enabled):
        self.calls.append(("toggle", child_id, enabled))


@pytest.fixture
def rig(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.update_interval = None
    coord.web_api = FakeWebAPI()
    coord.async_request_refresh = AsyncMock()
    coord.data = {
        "accounts": {CHILD: {
            "first_name": "Alex",
            "web_browsing": {
                "enabled": True,
                "useAllowedListOnly": False,
                "exceptions": [
                    {"website": "https://www.Example.com/", "allowed": False},
                    {"website": "https://school.example.org", "allowed": True},
                ],
            },
        }},
        "devices": {},
    }
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
    return coord, entry


async def test_sensor_counts_blocked_and_allowed_sites(rig):
    coord, entry = rig
    attrs = sensor.FamilySafetyWebFilterSensor(coord, entry, CHILD).extra_state_attributes
    assert (attrs["blocked_site_count"], attrs["allowed_site_count"]) == (1, 1)


def test_site_lists_are_host_names():
    assert site_lists({
        "exceptions": [
            {"website": "https://www.Example.com/path", "allowed": False},
            {"website": "https://school.example.org", "allowed": True},
        ],
    }) == (["www.example.com"], ["school.example.org"])
    assert site_lists({"blockedSites": ["https://a.example/x"], "allowedSites": []}) == (
        ["a.example"], []
    )
    # No list at all is unreadable, not empty.
    assert site_lists({"enabled": True}) is None


async def test_site_lists_are_read_from_the_mobile_api_with_exceptions():
    api = FamilySafetyWebAPI(authenticator=None)
    api._request = AsyncMock(return_value={"enabled": True, "exceptions": []})
    assert await api.get_web_restrictions(CHILD) == {"enabled": True, "exceptions": []}
    call = api._request.await_args
    assert call.args == ("GET", f"/v1/WebRestrictions/{CHILD}")
    assert call.kwargs["params"] == {"exceptions": "true"}

    api._request = AsyncMock(return_value=None)
    with pytest.raises(FamilySafetyWebAPIError):
        await api.get_web_restrictions(CHILD)


async def test_web_filter_switch_reads_and_writes(hass, rig):
    coord, entry = rig
    added: list = []
    await switch.async_setup_entry(hass, entry, added.extend)
    web_switch = next(e for e in added if isinstance(e, switch.FamilySafetyWebFilterSwitch))
    web_switch.hass = hass
    web_switch.entity_id = "switch.alex_web_filtering"
    assert web_switch.is_on is True

    await web_switch.async_turn_off()

    assert coord.web_api.calls == [("toggle", CHILD, False)]
    assert web_switch.is_on is False


async def test_website_services_take_several_sites(hass, rig):
    coord, _ = rig
    _register_services(hass)
    await hass.services.async_call(
        DOMAIN, "block_website",
        {"account_id": CHILD, "website": ["a.example", "b.example", "a.example"]},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN, "remove_website", {"account_id": CHILD, "website": "c.example"}, blocking=True,
    )
    assert coord.web_api.calls == [
        ("block", CHILD, "a.example"),
        ("block", CHILD, "b.example"),
        ("remove", CHILD, "c.example"),
    ]
