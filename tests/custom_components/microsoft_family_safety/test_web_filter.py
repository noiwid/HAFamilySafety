"""Web filter (Microsoft Edge): on/off switch, get_sites, several sites per call."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.microsoft_family_safety import _register_services, switch
from custom_components.microsoft_family_safety.api_client import (
    FamilySafetyWebAPI,
    FamilySafetyWebAPIError,
)
from custom_components.microsoft_family_safety.const import DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)

CHILD = "1234567890123456"

WEB_RESTRICTIONS = {
    "enabled": True,
    "useAllowedListOnly": False,
    "exceptions": [
        {"website": "https://www.example.com/", "allowed": False},
        {"website": "https://school.example.org", "allowed": True},
    ],
}


class FakeWebAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.fail: dict[str, Exception] = {}
        self.restrictions: object = dict(WEB_RESTRICTIONS)

    async def _write(self, op, child_id, website):
        self.calls.append((op, child_id, website))
        if website in self.fail:
            raise self.fail[website]

    async def block_website(self, child_id, website):
        await self._write("block", child_id, website)

    async def allow_website(self, child_id, website):
        await self._write("allow", child_id, website)

    async def remove_website(self, child_id, website):
        await self._write("remove", child_id, website)

    async def toggle_web_filter(self, child_id, enabled):
        self.calls.append(("toggle", child_id, enabled))

    async def get_web_restrictions(self, child_id):
        self.calls.append(("read", child_id, None))
        if isinstance(self.restrictions, Exception):
            raise self.restrictions
        return self.restrictions


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
            "web_browsing": {"enabled": True, "useAllowedListOnly": False},
        }},
        "devices": {},
    }
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
    _register_services(hass)
    return coord, entry


def _child_entity(hass, entry, entity_id: str) -> str:
    """An entity of this integration on the child's device."""
    device_id = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, CHILD)}
    ).id
    domain, object_id = entity_id.split(".", 1)
    er.async_get(hass).async_get_or_create(
        domain, DOMAIN, f"uid-{object_id}", suggested_object_id=object_id,
        config_entry=entry, device_id=device_id,
    )
    return entity_id


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
    # Shown at once, before the next poll confirms it.
    assert web_switch.is_on is False
    coord.async_request_refresh.assert_awaited()


async def test_web_filter_switch_is_unknown_without_data(hass, rig):
    coord, entry = rig
    coord.data["accounts"][CHILD]["web_browsing"] = None
    web_switch = switch.FamilySafetyWebFilterSwitch(coord, entry, CHILD, "Alex")
    assert web_switch.is_on is None


async def test_website_services_take_one_site_or_several(hass, rig):
    coord, _ = rig
    await hass.services.async_call(
        DOMAIN, "block_website",
        {"account_id": CHILD, "website": ["a.example", "b.example", "a.example", " "]},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN, "allow_website", {"account_id": CHILD, "website": "c.example"}, blocking=True,
    )
    await hass.services.async_call(
        DOMAIN, "remove_website", {"account_id": CHILD, "website": ["d.example"]},
        blocking=True,
    )
    assert coord.web_api.calls == [
        ("block", CHILD, "a.example"),
        ("block", CHILD, "b.example"),
        ("allow", CHILD, "c.example"),
        ("remove", CHILD, "d.example"),
    ]


async def test_the_same_site_spelled_twice_is_sent_once(hass, rig):
    coord, _ = rig
    await hass.services.async_call(
        DOMAIN, "block_website",
        {"account_id": CHILD, "website": [
            "a.example", "https://a.example", " A.EXAMPLE ", "HTTPS://A.Example",
            "http://a.example", "b.example",
        ]},
        blocking=True,
    )
    # Stored the same (https:// added, any case): only the first spelling goes.
    # http:// is stored as given, so it is a site of its own.
    assert coord.web_api.calls == [
        ("block", CHILD, "a.example"),
        ("block", CHILD, "http://a.example"),
        ("block", CHILD, "b.example"),
    ]


@pytest.mark.parametrize("website", [[""], "  ", ["", "   "]])
async def test_no_site_left_is_refused(hass, rig, website):
    coord, _ = rig
    with pytest.raises(ServiceValidationError, match="at least one website"):
        await hass.services.async_call(
            DOMAIN, "allow_website", {"account_id": CHILD, "website": website},
            blocking=True,
        )
    assert coord.web_api.calls == []
    coord.async_request_refresh.assert_not_awaited()


@pytest.mark.parametrize("service", ["block_website", "allow_website", "remove_website"])
async def test_website_services_answer_the_sites_done(hass, rig, service):
    coord, _ = rig
    answer = await hass.services.async_call(
        DOMAIN, service,
        {"account_id": CHILD, "website": ["a.example", "b.example", "a.example"]},
        blocking=True, return_response=True,
    )
    assert answer == {"done": ["a.example", "b.example"], "failed": []}
    # A response is optional: the call works without asking for one.
    assert await hass.services.async_call(
        DOMAIN, service, {"account_id": CHILD, "website": "c.example"}, blocking=True,
    ) is None


async def test_a_failed_site_does_not_stop_the_others_and_is_named(hass, rig):
    coord, _ = rig
    coord.web_api.fail = {"b.example": FamilySafetyWebAPIError("BadUrlFormat")}
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN, "block_website",
            {"account_id": CHILD, "website": ["a.example", "b.example", "c.example"]},
            blocking=True,
        )
    # Every site was tried.
    assert [c[2] for c in coord.web_api.calls] == ["a.example", "b.example", "c.example"]
    message = str(err.value)
    assert "1 of 3" in message
    assert "Done: a.example, c.example" in message
    assert "Failed: b.example (BadUrlFormat)" in message
    # The sites that went through are shown.
    coord.async_request_refresh.assert_awaited()


async def test_when_every_site_fails_none_is_done(hass, rig):
    coord, _ = rig
    coord.web_api.fail = {"a.example": TimeoutError("timed out")}
    with pytest.raises(HomeAssistantError, match=r"Done: none\. Failed: a\.example"):
        await coord.async_allow_website(CHILD, ["a.example"])
    coord.async_request_refresh.assert_not_awaited()


async def test_website_services_target_the_child_by_entity(hass, rig):
    coord, entry = rig
    entity_id = _child_entity(hass, entry, "switch.alex_web_filtering")
    await hass.services.async_call(
        DOMAIN, "block_website", {"entity_id": entity_id, "website": ["a.example"]},
        blocking=True,
    )
    sites = await hass.services.async_call(
        DOMAIN, "get_sites", {"entity_id": entity_id}, blocking=True, return_response=True,
    )
    assert coord.web_api.calls[0] == ("block", CHILD, "a.example")
    assert sites["account_id"] == CHILD


async def test_get_sites_answers_the_lists_as_stored(hass, rig):
    coord, _ = rig
    sites = await hass.services.async_call(
        DOMAIN, "get_sites", {"account_id": CHILD}, blocking=True, return_response=True,
    )
    assert sites == {
        "account_id": CHILD,
        "blocked": ["https://www.example.com/"],
        "allowed": ["https://school.example.org"],
        "filter_enabled": True,
    }
    # The web route's shape is read as well.
    coord.web_api.restrictions = {
        "isEnabled": False, "blockedSites": ["https://a.example"], "allowedSites": [],
    }
    sites = await coord.async_get_sites(CHILD)
    assert (sites["blocked"], sites["allowed"], sites["filter_enabled"]) == (
        ["https://a.example"], [], False
    )


async def test_get_sites_fails_when_the_lists_cannot_be_read(hass, rig):
    coord, _ = rig
    coord.web_api.restrictions = {"enabled": True}  # no list at all: not "empty"
    with pytest.raises(HomeAssistantError, match="no list"):
        await coord.async_get_sites(CHILD)
    coord.web_api.restrictions = FamilySafetyWebAPIError("status 503")
    with pytest.raises(HomeAssistantError, match="Could not read"):
        await hass.services.async_call(
            DOMAIN, "get_sites", {"account_id": CHILD}, blocking=True, return_response=True,
        )


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
