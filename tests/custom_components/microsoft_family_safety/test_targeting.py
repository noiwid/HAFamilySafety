"""Services take the child as account_id, as before, or as one of its entities."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.microsoft_family_safety import _register_services
from custom_components.microsoft_family_safety.const import DOMAIN

CHILD = "1234567890123456"
OTHER = "6543210987654321"
DEVICE = "device-abc"


@pytest.fixture
def coordinator(hass):
    coord = SimpleNamespace(
        data={
            "accounts": {CHILD: {}, OTHER: {}},
            "devices": {DEVICE: {"account_id": CHILD}},
        },
        async_lock_account=AsyncMock(),
        async_block_website=AsyncMock(),
        async_approve_request=AsyncMock(),
    )
    hass.data.setdefault(DOMAIN, {})["entry"] = coord
    with patch(
        "custom_components.microsoft_family_safety._get_coordinator",
        return_value=coord,
    ):
        _register_services(hass)
        yield coord


def _entity(hass, entity_id: str, identifier: str | None, platform: str = DOMAIN) -> str:
    entry = MockConfigEntry(domain=platform)
    entry.add_to_hass(hass)
    device_id = None
    if identifier is not None:
        device_id = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(platform, identifier)}
        ).id
    domain, object_id = entity_id.split(".", 1)
    er.async_get(hass).async_get_or_create(
        domain, platform, f"uid-{object_id}", suggested_object_id=object_id,
        config_entry=entry, device_id=device_id,
    )
    return entity_id


async def _lock(hass, **data) -> None:
    await hass.services.async_call(DOMAIN, "lock_account", data, blocking=True)


async def test_account_id_still_works(hass, coordinator) -> None:
    await _lock(hass, account_id=CHILD)
    coordinator.async_lock_account.assert_awaited_once_with(CHILD)


async def test_entity_on_the_child_device_names_the_child(hass, coordinator) -> None:
    entity_id = _entity(hass, "switch.alex_lock", CHILD)
    await _lock(hass, entity_id=entity_id)
    coordinator.async_lock_account.assert_awaited_once_with(CHILD)


async def test_entity_on_a_physical_device_names_its_child(hass, coordinator) -> None:
    entity_id = _entity(hass, "sensor.laptop_screen_time", DEVICE)
    await hass.services.async_call(
        DOMAIN, "block_website",
        {"entity_id": entity_id, "website": "example.com"}, blocking=True,
    )
    coordinator.async_block_website.assert_awaited_once_with(CHILD, ["example.com"])


async def test_entity_without_device_uses_its_user_id_attribute(hass, coordinator) -> None:
    entity_id = _entity(hass, "sensor.alex_account_info", None)
    hass.states.async_set(entity_id, "Alex", {"user_id": OTHER})
    await _lock(hass, entity_id=entity_id)
    coordinator.async_lock_account.assert_awaited_once_with(OTHER)


async def test_account_id_wins_over_entity(hass, coordinator) -> None:
    entity_id = _entity(hass, "switch.alex_lock", CHILD)
    await _lock(hass, entity_id=entity_id, account_id=OTHER)
    coordinator.async_lock_account.assert_awaited_once_with(OTHER)


async def test_entity_of_another_integration_is_refused(hass, coordinator) -> None:
    entity_id = _entity(hass, "light.kitchen", "x", platform="hue")
    with pytest.raises(ServiceValidationError):
        await _lock(hass, entity_id=entity_id)
    coordinator.async_lock_account.assert_not_awaited()


async def test_entity_without_a_child_is_refused(hass, coordinator) -> None:
    entity_id = _entity(hass, "sensor.connection", "entry_service")
    with pytest.raises(ServiceValidationError):
        await _lock(hass, entity_id=entity_id)


@pytest.mark.parametrize("data", [{}, {"account_id": ""}, {"account_id": "None"}])
async def test_a_call_without_child_is_refused(hass, coordinator, data) -> None:
    with pytest.raises(vol.Invalid):
        await _lock(hass, **data)
    coordinator.async_lock_account.assert_not_awaited()


async def test_services_without_a_child_are_unchanged(hass, coordinator) -> None:
    await hass.services.async_call(
        DOMAIN, "approve_request", {"request_id": "r1"}, blocking=True
    )
    coordinator.async_approve_request.assert_awaited_once_with("r1", 3600)


async def test_an_entity_list_of_one_child_names_it(hass, coordinator) -> None:
    first = _entity(hass, "switch.alex_lock", CHILD)
    second = _entity(hass, "sensor.laptop_screen_time", DEVICE)
    await _lock(hass, entity_id=[first, second])
    coordinator.async_lock_account.assert_awaited_once_with(CHILD)


async def test_entities_of_two_children_are_refused(hass, coordinator) -> None:
    first = _entity(hass, "switch.alex_lock", CHILD)
    other = _entity(hass, "switch.sam_lock", OTHER)
    with pytest.raises(ServiceValidationError, match="different"):
        await _lock(hass, entity_id=[first, other])
    coordinator.async_lock_account.assert_not_awaited()


# The roster keys accounts by the int id the mobile API gives, while service
# calls pass it as a str.


def _int_keyed_coordinator(hass):
    from custom_components.microsoft_family_safety.coordinator import (
        FamilySafetyDataUpdateCoordinator,
    )

    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    account = SimpleNamespace(user_id=int(CHILD))
    coord._accounts = {int(CHILD): account}
    coord.data = {"accounts": {int(CHILD): {"blocked_platforms": []}}, "devices": {}}
    return coord, account


async def test_a_str_id_finds_an_int_keyed_account(hass) -> None:
    coord, account = _int_keyed_coordinator(hass)
    assert coord.get_account(CHILD) is account
    assert coord.get_account(int(CHILD)) is account
    assert coord.account_data(CHILD) is coord.data["accounts"][int(CHILD)]
    assert coord.get_account(OTHER) is None
    assert coord.account_data(OTHER) is None
