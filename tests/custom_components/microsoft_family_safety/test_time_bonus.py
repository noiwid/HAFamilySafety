"""add_time_bonus / cancel_time_bonus: Xbox extra time through xapprove only."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.microsoft_family_safety import _register_services
from custom_components.microsoft_family_safety.const import CONF_XBOX_FEATURES, DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)
from custom_components.microsoft_family_safety.xbox_family_api import (
    OVERRIDE_ALLOW,
    OVERRIDE_BLOCK,
    OVERRIDE_REVOKE,
    XboxFamilyError,
)

CHILD = "1234567890123456"
XUID = "2535400000000001"
FAR = "2099-01-01T00:00:00Z"


@pytest.fixture
async def coordinator(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"refresh_token": "x"}, options={CONF_XBOX_FEATURES: True}
    )
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.xbox_family = MagicMock(linked=True, ready=True, usable=True)
    coord.xbox_family.async_child_xuid = AsyncMock(return_value=XUID)
    coord.xbox_family.async_get_overrides = AsyncMock(return_value={"overrides": []})
    coord.xbox_family.async_create_override = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    coord._accounts = {int(CHILD): SimpleNamespace(user_id=int(CHILD), first_name="Sam", surname="Doe")}
    # No web session at all: nothing here may use it.
    coord.web_api = None
    return coord


def _answer(*overrides: tuple[int, str]) -> dict:
    return {"overrides": [{"overrideType": t, "validUntil": u} for t, u in overrides]}


def _iso(when) -> str:
    return dt_util.as_utc(when).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --- add ----------------------------------------------------------------------------
async def test_extra_time_through_xapprove_lifts_our_lock_state(coordinator):
    until = dt_util.utcnow() + timedelta(minutes=30)
    coordinator._platform_override_until[(CHILD, "xbox")] = until + timedelta(hours=2)
    coordinator.xbox_family.async_create_override.return_value = _answer((OVERRIDE_ALLOW, _iso(until)))

    await coordinator.async_add_time_bonus(CHILD, "Xbox", until)

    args = coordinator.xbox_family.async_create_override.await_args.args
    assert args[0] == XUID and args[1] == OVERRIDE_ALLOW and args[2] == until
    assert (CHILD, "xbox") not in coordinator._platform_override_until
    assert CHILD in coordinator._xbox_lock_known
    coordinator.async_request_refresh.assert_awaited()


@pytest.mark.parametrize(
    "answer",
    [
        {"overrides": []},
        _answer((OVERRIDE_ALLOW, "2099-01-01T00:00:00Z")),  # another end time
        _answer((OVERRIDE_BLOCK, FAR)),
    ],
)
async def test_extra_time_not_confirmed_fails(coordinator, answer):
    coordinator.xbox_family.async_create_override.return_value = answer
    with pytest.raises(HomeAssistantError, match="did not confirm"):
        await coordinator.async_add_time_bonus(CHILD, "Xbox", dt_util.utcnow() + timedelta(minutes=30))


async def test_extra_time_with_a_lock_left_in_force_fails_and_keeps_the_lock(coordinator):
    until = dt_util.utcnow() + timedelta(minutes=30)
    lock = until + timedelta(hours=2)
    coordinator._platform_override_until[(CHILD, "xbox")] = lock
    coordinator.xbox_family.async_create_override.return_value = _answer(
        (OVERRIDE_BLOCK, _iso(lock)), (OVERRIDE_ALLOW, _iso(until))
    )
    with pytest.raises(HomeAssistantError, match="still locked"):
        await coordinator.async_add_time_bonus(CHILD, "Xbox", until)
    assert coordinator._platform_override_until[(CHILD, "xbox")] == lock


async def test_an_xapprove_error_fails(coordinator):
    coordinator.xbox_family.async_create_override.side_effect = XboxFamilyError("HTTP 500")
    with pytest.raises(HomeAssistantError, match="Could not give Xbox extra time"):
        await coordinator.async_add_time_bonus(CHILD, "Xbox", dt_util.utcnow() + timedelta(minutes=5))


async def test_without_the_xbox_link_it_fails_clearly(coordinator):
    coordinator.xbox_family.linked = False
    with pytest.raises(HomeAssistantError, match="Xbox Family Settings sign-in linked"):
        await coordinator.async_add_time_bonus(CHILD, "Xbox", dt_util.utcnow() + timedelta(minutes=5))
    coordinator.xbox_family.async_create_override.assert_not_awaited()


async def test_windows_is_refused(coordinator):
    with pytest.raises(HomeAssistantError, match="only be given on the Xbox"):
        await coordinator.async_add_time_bonus(CHILD, "Windows", dt_util.utcnow() + timedelta(minutes=5))


async def test_an_unknown_child_is_refused(coordinator):
    with pytest.raises(ValueError):
        await coordinator.async_add_time_bonus("999", "Xbox", dt_util.utcnow() + timedelta(minutes=5))


# --- cancel -------------------------------------------------------------------------
async def test_cancelling_xbox_extra_time_revokes_it_and_leaves_a_lock_alone(coordinator):
    coordinator.xbox_family.async_get_overrides.return_value = _answer((OVERRIDE_ALLOW, FAR))
    coordinator.xbox_family.async_create_override.return_value = {"overrides": []}
    await coordinator.async_cancel_time_bonus(CHILD, "Xbox")
    assert coordinator.xbox_family.async_create_override.await_args.args[1] == OVERRIDE_REVOKE

    coordinator.xbox_family.async_create_override.reset_mock()
    coordinator.xbox_family.async_get_overrides.return_value = _answer((OVERRIDE_BLOCK, FAR))
    await coordinator.async_cancel_time_bonus(CHILD, "Xbox")
    coordinator.xbox_family.async_create_override.assert_not_called()


async def test_cancel_sends_nothing_while_a_lock_is_in_force(coordinator):
    # The revoke would lift the lock as well.
    coordinator.xbox_family.async_get_overrides.return_value = _answer(
        (OVERRIDE_BLOCK, FAR), (OVERRIDE_ALLOW, FAR)
    )
    await coordinator.async_cancel_time_bonus(CHILD, "Xbox")
    coordinator.xbox_family.async_create_override.assert_not_called()


async def test_cancel_with_nothing_in_force_sends_nothing(coordinator):
    await coordinator.async_cancel_time_bonus(CHILD, "Xbox")
    coordinator.xbox_family.async_create_override.assert_not_called()


async def test_cancel_fails_when_the_extra_time_stays(coordinator):
    coordinator.xbox_family.async_get_overrides.return_value = _answer((OVERRIDE_ALLOW, FAR))
    coordinator.xbox_family.async_create_override.return_value = _answer((OVERRIDE_ALLOW, FAR))
    with pytest.raises(HomeAssistantError, match="left the Xbox extra time"):
        await coordinator.async_cancel_time_bonus(CHILD, "Xbox")


async def test_cancel_fails_when_the_overrides_cannot_be_read(coordinator):
    coordinator.xbox_family.async_get_overrides.side_effect = XboxFamilyError("HTTP 503")
    with pytest.raises(HomeAssistantError, match="nothing was cancelled"):
        await coordinator.async_cancel_time_bonus(CHILD, "Xbox")
    coordinator.xbox_family.async_create_override.assert_not_called()


# --- services -----------------------------------------------------------------------
async def test_service_takes_minutes_or_until(hass):
    coord = MagicMock()
    coord.async_add_time_bonus = AsyncMock()
    coord.async_cancel_time_bonus = AsyncMock()
    with patch("custom_components.microsoft_family_safety._get_coordinator", return_value=coord):
        _register_services(hass)
        before = dt_util.now()
        await hass.services.async_call(
            DOMAIN, "add_time_bonus", {"account_id": CHILD, "minutes": 30}, blocking=True
        )
        account_id, platform, until = coord.async_add_time_bonus.await_args.args
        assert (account_id, platform) == (CHILD, "Xbox")
        assert timedelta(minutes=29) < until - before < timedelta(minutes=31)

        later = dt_util.now() + timedelta(hours=2)
        await hass.services.async_call(
            DOMAIN, "add_time_bonus",
            {"account_id": CHILD, "platform": "Xbox", "until": later.isoformat()}, blocking=True,
        )
        assert abs(coord.async_add_time_bonus.await_args.args[2] - later) < timedelta(seconds=1)

        for data in (
            {},
            {"minutes": 5, "until": later.isoformat()},
            {"until": (dt_util.now() - timedelta(minutes=1)).isoformat()},
            {"until": (dt_util.now() + timedelta(hours=25)).isoformat()},
        ):
            with pytest.raises(ServiceValidationError):
                await hass.services.async_call(
                    DOMAIN, "add_time_bonus", {"account_id": CHILD, **data}, blocking=True
                )

        await hass.services.async_call(
            DOMAIN, "cancel_time_bonus", {"account_id": CHILD}, blocking=True
        )
        coord.async_cancel_time_bonus.assert_awaited_once_with(CHILD, "Xbox")


async def test_service_refuses_windows(hass):
    import voluptuous as vol

    with patch("custom_components.microsoft_family_safety._get_coordinator", return_value=MagicMock()):
        _register_services(hass)
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN, "add_time_bonus",
                {"account_id": CHILD, "platform": "Windows", "minutes": 5}, blocking=True,
            )
