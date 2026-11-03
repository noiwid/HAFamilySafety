"""The "Users allowed to control Microsoft Family Safety" option.

Home Assistant lets every user press any button or flip any switch unless an
entity permission policy says otherwise. When the option lists users, a
non-administrator who is not listed is refused on every entity action and every
microsoft_family_safety.* service; administrators and calls without a user
(automations) stay allowed, and an empty list changes nothing.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from homeassistant.core import Context
from homeassistant.exceptions import Unauthorized

from custom_components.microsoft_family_safety import (
    _async_options_updated,
    _LOADED_OPTIONS,
    _register_services,
    _reload_relevant,
)
from custom_components.microsoft_family_safety.config_flow import _allowed_users_selector
from custom_components.microsoft_family_safety.const import CONF_ALLOWED_USERS, DOMAIN
from custom_components.microsoft_family_safety.permissions import (
    allowed_user_ids,
    async_check_entity_action,
)

CHILD = "1234567890123456"


@pytest.fixture
def coordinator():
    """A coordinator whose service methods are mocked."""
    return SimpleNamespace(async_lock_account=AsyncMock(), async_unlock_account=AsyncMock())


@pytest.fixture
def services(hass, coordinator):
    with patch(
        "custom_components.microsoft_family_safety._get_coordinator",
        return_value=coordinator,
    ):
        _register_services(hass)
        yield coordinator


def _user(hass, *, is_admin: bool = False) -> MockUser:
    user = MockUser(is_owner=is_admin).add_to_hass(hass)
    if not is_admin:
        user.mock_policy({"entities": True})
    return user


def _restrict(hass, *user_ids: str) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_ALLOWED_USERS: list(user_ids)})
    entry.add_to_hass(hass)
    return entry


async def _lock(hass, user: MockUser | None) -> None:
    context = Context(user_id=user.id) if user else Context()
    await hass.services.async_call(
        DOMAIN, "lock_account", {"account_id": CHILD}, blocking=True, context=context
    )


def _entity(hass, user: MockUser | None) -> SimpleNamespace:
    return SimpleNamespace(
        hass=hass,
        entity_id="switch.kid_family_safety_kid_lock",
        _context=Context(user_id=user.id) if user else Context(),
    )


async def test_empty_option_changes_nothing(hass, services) -> None:
    _restrict(hass)
    kid = _user(hass)

    assert allowed_user_ids(hass) is None
    await _lock(hass, kid)
    await async_check_entity_action(_entity(hass, kid))

    assert services.async_lock_account.await_count == 1


async def test_unlisted_user_is_refused_on_services(hass, services) -> None:
    parent = _user(hass)
    kid = _user(hass)
    _restrict(hass, parent.id)

    with pytest.raises(Unauthorized):
        await _lock(hass, kid)

    services.async_lock_account.assert_not_awaited()


async def test_unlisted_user_is_refused_on_entities(hass) -> None:
    parent = _user(hass)
    kid = _user(hass)
    _restrict(hass, parent.id)

    with pytest.raises(Unauthorized):
        await async_check_entity_action(_entity(hass, kid))


async def test_listed_user_is_allowed(hass, services) -> None:
    parent = _user(hass)
    _restrict(hass, parent.id)

    await _lock(hass, parent)
    await async_check_entity_action(_entity(hass, parent))

    assert services.async_lock_account.await_count == 1


async def test_admin_is_allowed_without_being_listed(hass, services) -> None:
    parent = _user(hass)
    admin = _user(hass, is_admin=True)
    _restrict(hass, parent.id)

    await _lock(hass, admin)
    await async_check_entity_action(_entity(hass, admin))

    assert services.async_lock_account.await_count == 1


async def test_call_without_user_is_allowed(hass, services) -> None:
    parent = _user(hass)
    _restrict(hass, parent.id)

    await _lock(hass, None)
    await async_check_entity_action(_entity(hass, None))

    assert services.async_lock_account.await_count == 1


async def test_lists_of_several_entries_are_merged(hass) -> None:
    first = _user(hass)
    second = _user(hass)
    _restrict(hass, first.id)
    _restrict(hass, second.id)

    assert allowed_user_ids(hass) == {first.id, second.id}
    await async_check_entity_action(_entity(hass, second))


async def test_selector_lists_people_only(hass) -> None:
    person = _user(hass)
    system = MockUser(system_generated=True).add_to_hass(hass)
    inactive = MockUser(is_active=False).add_to_hass(hass)

    selector, selected = await _allowed_users_selector(hass, [person.id, "deleted-user"])
    values = {option["value"] for option in selector.config["options"]}

    assert person.id in values
    assert system.id not in values
    assert inactive.id not in values
    # A deleted user would make the form impossible to save.
    assert selected == [person.id]


async def test_changing_only_the_allowed_users_does_not_reload(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, options={"update_interval": 300})
    entry.add_to_hass(hass)
    _LOADED_OPTIONS[entry.entry_id] = _reload_relevant(entry.options)

    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
        hass.config_entries.async_update_entry(
            entry, options={"update_interval": 300, CONF_ALLOWED_USERS: ["someone"]}
        )
        await _async_options_updated(hass, entry)
        reload.assert_not_awaited()

        hass.config_entries.async_update_entry(
            entry, options={"update_interval": 600, CONF_ALLOWED_USERS: ["someone"]}
        )
        await _async_options_updated(hass, entry)
        reload.assert_awaited_once()
