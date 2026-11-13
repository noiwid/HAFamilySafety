"""add_time_bonus / cancel_time_bonus: Xbox extra time through xapprove, Windows through the mobile overrides."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
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
async def test_extra_time_through_xapprove(coordinator):
    until = dt_util.utcnow() + timedelta(minutes=30)
    coordinator.xbox_family.async_create_override.return_value = _answer((OVERRIDE_ALLOW, _iso(until)))

    answer = await coordinator.async_add_time_bonus(CHILD, "Xbox", until)

    args = coordinator.xbox_family.async_create_override.await_args.args
    assert args[0] == XUID and args[1] == OVERRIDE_ALLOW and args[2] == until
    assert answer["added"] is True and answer["replaced_lock"] is False
    assert abs(dt_util.parse_datetime(answer["until"]) - until) < timedelta(seconds=1)
    assert CHILD in coordinator._xbox_lock_known
    coordinator.async_request_refresh.assert_awaited()


async def test_extra_time_over_a_lock_is_refused_without_replace_lock(coordinator):
    lock = dt_util.utcnow() + timedelta(hours=2)
    coordinator._platform_override_until[(CHILD, "xbox")] = lock
    coordinator.xbox_family.async_get_overrides.return_value = _answer((OVERRIDE_BLOCK, _iso(lock)))
    with pytest.raises(HomeAssistantError, match="replace_lock"):
        await coordinator.async_add_time_bonus(CHILD, "Xbox", dt_util.utcnow() + timedelta(minutes=30))
    coordinator.xbox_family.async_create_override.assert_not_called()
    assert coordinator._platform_override_until[(CHILD, "xbox")] == lock


async def test_extra_time_over_a_lock_with_replace_lock_lifts_our_lock_state(coordinator):
    until = dt_util.utcnow() + timedelta(minutes=30)
    lock = until + timedelta(hours=2)
    coordinator._platform_override_until[(CHILD, "xbox")] = lock
    coordinator.xbox_family.async_get_overrides.return_value = _answer((OVERRIDE_BLOCK, _iso(lock)))
    coordinator.xbox_family.async_create_override.return_value = _answer((OVERRIDE_ALLOW, _iso(until)))

    answer = await coordinator.async_add_time_bonus(CHILD, "Xbox", until, replace_lock=True)

    assert answer["replaced_lock"] is True
    assert (CHILD, "xbox") not in coordinator._platform_override_until


async def test_an_expired_lock_does_not_need_replace_lock(coordinator):
    until = dt_util.utcnow() + timedelta(minutes=30)
    coordinator.xbox_family.async_get_overrides.return_value = _answer(
        (OVERRIDE_BLOCK, "2020-01-01T00:00:00Z")
    )
    coordinator.xbox_family.async_create_override.return_value = _answer((OVERRIDE_ALLOW, _iso(until)))
    answer = await coordinator.async_add_time_bonus(CHILD, "Xbox", until)
    assert answer["replaced_lock"] is False


@pytest.mark.parametrize(
    "answer",
    [
        {"overrides": []},
        _answer((OVERRIDE_ALLOW, FAR)),  # another end time
        _answer((OVERRIDE_BLOCK, FAR)),
    ],
)
async def test_extra_time_not_confirmed_fails(coordinator, answer):
    coordinator.xbox_family.async_create_override.return_value = answer
    with pytest.raises(HomeAssistantError, match="did not confirm"):
        await coordinator.async_add_time_bonus(CHILD, "Xbox", dt_util.utcnow() + timedelta(minutes=30))


async def test_of_several_extra_times_the_one_ending_last_counts(coordinator):
    until = dt_util.utcnow() + timedelta(minutes=30)
    coordinator.xbox_family.async_create_override.return_value = _answer(
        (OVERRIDE_ALLOW, "2020-01-01T00:00:00Z"),  # expired
        (OVERRIDE_ALLOW, _iso(until - timedelta(minutes=20))),
        (OVERRIDE_ALLOW, _iso(until)),
    )
    answer = await coordinator.async_add_time_bonus(CHILD, "Xbox", until)
    assert abs(dt_util.parse_datetime(answer["until"]) - until) < timedelta(seconds=1)


def test_an_end_without_an_offset_is_utc(coordinator):
    until = (dt_util.utcnow() + timedelta(hours=1)).replace(microsecond=0)
    naive = until.replace(tzinfo=None).isoformat()
    found = coordinator._xapprove_override_of(_answer((OVERRIDE_ALLOW, naive)), OVERRIDE_ALLOW)
    assert coordinator._xapprove_until(found["validUntil"]) == until


async def test_extra_time_with_a_lock_left_in_force_fails_and_keeps_the_lock(coordinator):
    until = dt_util.utcnow() + timedelta(minutes=30)
    lock = until + timedelta(hours=2)
    coordinator._platform_override_until[(CHILD, "xbox")] = lock
    coordinator.xbox_family.async_create_override.return_value = _answer(
        (OVERRIDE_BLOCK, _iso(lock)), (OVERRIDE_ALLOW, _iso(until))
    )
    with pytest.raises(HomeAssistantError, match="still locked"):
        await coordinator.async_add_time_bonus(CHILD, "Xbox", until, replace_lock=True)
    assert coordinator._platform_override_until[(CHILD, "xbox")] == lock


async def test_an_xapprove_error_fails(coordinator):
    coordinator.xbox_family.async_create_override.side_effect = XboxFamilyError("HTTP 500")
    with pytest.raises(HomeAssistantError, match="Could not give Xbox extra time"):
        await coordinator.async_add_time_bonus(CHILD, "Xbox", dt_util.utcnow() + timedelta(minutes=5))


async def test_add_fails_when_the_overrides_cannot_be_read(coordinator):
    coordinator.xbox_family.async_get_overrides.side_effect = XboxFamilyError("HTTP 503")
    with pytest.raises(HomeAssistantError, match="no extra time was given"):
        await coordinator.async_add_time_bonus(CHILD, "Xbox", dt_util.utcnow() + timedelta(minutes=5))
    coordinator.xbox_family.async_create_override.assert_not_called()


async def test_without_the_xbox_link_it_fails_clearly(coordinator):
    coordinator.xbox_family.linked = False
    with pytest.raises(HomeAssistantError, match="Xbox Family Settings sign-in linked"):
        await coordinator.async_add_time_bonus(CHILD, "Xbox", dt_util.utcnow() + timedelta(minutes=5))
    coordinator.xbox_family.async_create_override.assert_not_awaited()


def _windows(*overrides: tuple[str, str]) -> dict:
    """A mobile overrides document with the Windows entry (listed as "Desktop", as Microsoft may)."""
    return {"lockablePlatforms": [{
        "appliesTo": "Desktop",
        "overrides": [{"overrideType": t, "validUntil": u} for t, u in overrides],
        "devices": [],
    }]}


@pytest.fixture
def mobile(coordinator):
    """The mobile API of the coordinator: overrides read and written."""
    coordinator.web_api = MagicMock()
    coordinator.web_api.get_mobile_device_overrides = AsyncMock(return_value=_windows())
    coordinator.web_api.set_mobile_device_override = AsyncMock()
    coordinator._MOBILE_READBACK_DELAYS = (0,)
    return coordinator.web_api


async def test_windows_extra_time_is_an_allow_until_override(coordinator, mobile):
    until = dt_util.utcnow() + timedelta(minutes=30)
    mobile.set_mobile_device_override.return_value = _windows(("AllowUntil", _iso(until)))
    answer = await coordinator.async_add_time_bonus(CHILD, "Windows", until)
    assert answer["added"] is True and answer["replaced_lock"] is False
    assert mobile.set_mobile_device_override.await_args.args[1:3] == ("Desktop", "AllowUntil")
    # Allowed now reads it until a poll does
    acct = coordinator._accounts[int(CHILD)]
    acct.extra_time = {}
    assert coordinator._merge_extra_time(acct)["Windows"]


async def test_windows_extra_time_is_refused_while_locked(coordinator, mobile):
    mobile.get_mobile_device_overrides.return_value = _windows(("BlockUntil", FAR))
    with pytest.raises(HomeAssistantError, match="Windows is locked"):
        await coordinator.async_add_time_bonus(CHILD, "Windows", dt_util.utcnow() + timedelta(minutes=5))
    mobile.set_mobile_device_override.assert_not_awaited()


async def test_windows_extra_time_not_listed_by_microsoft_fails(coordinator, mobile):
    mobile.set_mobile_device_override.return_value = _windows()
    with pytest.raises(HomeAssistantError, match="did not confirm the Windows extra time"):
        await coordinator.async_add_time_bonus(CHILD, "Windows", dt_util.utcnow() + timedelta(minutes=5))


async def test_cancelling_windows_extra_time(coordinator, mobile):
    assert await coordinator.async_cancel_time_bonus(CHILD, "Windows") == {
        "cancelled": False, "reason": "no_extra_time",
    }
    mobile.set_mobile_device_override.assert_not_awaited()
    mobile.get_mobile_device_overrides.return_value = _windows(("AllowUntil", FAR))
    mobile.set_mobile_device_override.return_value = _windows()
    assert await coordinator.async_cancel_time_bonus(CHILD, "Windows") == {
        "cancelled": True, "lock_in_force": False,
    }
    assert mobile.set_mobile_device_override.await_args.args[2] == "Cancel"
    # a lock is never lifted by a cancel of the extra time
    mobile.set_mobile_device_override.reset_mock()
    coordinator._mobile_writes.clear()  # our cancel would otherwise stand over the read for 2 min
    mobile.get_mobile_device_overrides.return_value = _windows(("BlockUntil", FAR), ("AllowUntil", FAR))
    with pytest.raises(HomeAssistantError, match="locked"):
        await coordinator.async_cancel_time_bonus(CHILD, "Windows")
    mobile.set_mobile_device_override.assert_not_awaited()


async def test_windows_allowed_now_counts_the_extra_time(coordinator):
    later = dt_util.utcnow() + timedelta(minutes=40)
    coordinator.data = {"accounts": {CHILD: {"extra_time": {"Windows": later.isoformat()}}}, "devices": {}}
    status = coordinator.platform_status(CHILD, "Windows")
    assert status["allowed"] is True and status["reason"] == "extra_time"
    assert 38 <= status["remaining_minutes"] <= 40


async def test_an_unknown_child_is_refused(coordinator):
    with pytest.raises(ValueError):
        await coordinator.async_add_time_bonus("999", "Xbox", dt_util.utcnow() + timedelta(minutes=5))


# --- cancel -------------------------------------------------------------------------
async def test_cancelling_xbox_extra_time_revokes_it_and_leaves_a_lock_alone(coordinator):
    coordinator.xbox_family.async_get_overrides.return_value = _answer((OVERRIDE_ALLOW, FAR))
    coordinator.xbox_family.async_create_override.return_value = {"overrides": []}
    answer = await coordinator.async_cancel_time_bonus(CHILD, "Xbox")
    assert answer == {"cancelled": True, "lock_in_force": False}
    assert coordinator.xbox_family.async_create_override.await_args.args[1] == OVERRIDE_REVOKE

    coordinator.xbox_family.async_create_override.reset_mock()
    coordinator.xbox_family.async_get_overrides.return_value = _answer((OVERRIDE_BLOCK, FAR))
    with pytest.raises(HomeAssistantError, match="locked"):
        await coordinator.async_cancel_time_bonus(CHILD, "Xbox")
    coordinator.xbox_family.async_create_override.assert_not_called()


async def test_cancel_sends_nothing_while_a_lock_is_in_force(coordinator):
    # The revoke would lift the lock as well.
    coordinator.xbox_family.async_get_overrides.return_value = _answer(
        (OVERRIDE_BLOCK, FAR), (OVERRIDE_ALLOW, FAR)
    )
    with pytest.raises(HomeAssistantError, match="locked"):
        await coordinator.async_cancel_time_bonus(CHILD, "Xbox")
    coordinator.xbox_family.async_create_override.assert_not_called()


async def test_cancel_with_nothing_in_force_sends_nothing(coordinator):
    answer = await coordinator.async_cancel_time_bonus(CHILD, "Xbox")
    assert answer == {"cancelled": False, "reason": "no_extra_time"}
    coordinator.xbox_family.async_create_override.assert_not_called()


async def test_cancel_ignores_expired_entries(coordinator):
    coordinator.xbox_family.async_get_overrides.return_value = _answer(
        (OVERRIDE_BLOCK, "2020-01-01T00:00:00Z"), (OVERRIDE_ALLOW, "2020-01-01T01:00:00Z")
    )
    answer = await coordinator.async_cancel_time_bonus(CHILD, "Xbox")
    assert answer == {"cancelled": False, "reason": "no_extra_time"}
    coordinator.xbox_family.async_create_override.assert_not_called()

    # an expired lock does not keep live extra time from being cancelled
    coordinator.xbox_family.async_get_overrides.return_value = _answer(
        (OVERRIDE_BLOCK, "2020-01-01T00:00:00Z"), (OVERRIDE_ALLOW, FAR)
    )
    coordinator.xbox_family.async_create_override.return_value = {"overrides": []}
    answer = await coordinator.async_cancel_time_bonus(CHILD, "Xbox")
    assert answer["cancelled"] is True


async def test_cancel_reports_a_lock_listed_after_the_revoke(coordinator):
    coordinator.xbox_family.async_get_overrides.return_value = _answer((OVERRIDE_ALLOW, FAR))
    coordinator.xbox_family.async_create_override.return_value = _answer((OVERRIDE_BLOCK, FAR))
    answer = await coordinator.async_cancel_time_bonus(CHILD, "Xbox")
    assert answer == {"cancelled": True, "lock_in_force": True}


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
    coord.async_add_time_bonus = AsyncMock(return_value={"added": True})
    coord.async_cancel_time_bonus = AsyncMock(return_value={"cancelled": False, "reason": "no_extra_time"})
    with patch("custom_components.microsoft_family_safety._get_coordinator", return_value=coord):
        _register_services(hass)
        before = dt_util.now()
        await hass.services.async_call(
            DOMAIN, "add_time_bonus", {"account_id": CHILD, "minutes": 30}, blocking=True
        )
        account_id, platform, until, replace_lock = coord.async_add_time_bonus.await_args.args
        assert (account_id, platform, replace_lock) == (CHILD, "Xbox", False)
        assert timedelta(minutes=29) < until - before < timedelta(minutes=31)

        later = dt_util.now() + timedelta(hours=2)
        response = await hass.services.async_call(
            DOMAIN, "add_time_bonus",
            {"account_id": CHILD, "platform": "Xbox", "until": later.isoformat(), "replace_lock": True},
            blocking=True, return_response=True,
        )
        assert response == {"added": True}
        assert abs(coord.async_add_time_bonus.await_args.args[2] - later) < timedelta(seconds=1)
        assert coord.async_add_time_bonus.await_args.args[3] is True

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

        response = await hass.services.async_call(
            DOMAIN, "cancel_time_bonus", {"account_id": CHILD}, blocking=True, return_response=True
        )
        assert response == {"cancelled": False, "reason": "no_extra_time"}
        coord.async_cancel_time_bonus.assert_awaited_once_with(CHILD, "Xbox")


async def test_service_takes_an_entity_of_the_child(hass):
    coord = SimpleNamespace(
        data={"accounts": {CHILD: {}}, "devices": {}},
        async_add_time_bonus=AsyncMock(return_value={"added": True}),
        async_cancel_time_bonus=AsyncMock(return_value={"cancelled": True}),
    )
    hass.data.setdefault(DOMAIN, {})["entry"] = coord
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, CHILD)}
    )
    er.async_get(hass).async_get_or_create(
        "switch", DOMAIN, "uid-alex_xbox", suggested_object_id="alex_xbox",
        config_entry=entry, device_id=device.id,
    )
    with patch("custom_components.microsoft_family_safety._get_coordinator", return_value=coord):
        _register_services(hass)
        await hass.services.async_call(
            DOMAIN, "add_time_bonus",
            {"entity_id": "switch.alex_xbox", "minutes": 30}, blocking=True,
        )
        assert coord.async_add_time_bonus.await_args.args[0] == CHILD
        await hass.services.async_call(
            DOMAIN, "cancel_time_bonus", {"entity_id": "switch.alex_xbox"}, blocking=True
        )
        coord.async_cancel_time_bonus.assert_awaited_once_with(CHILD, "Xbox")


async def test_service_refuses_mobile(hass):
    import voluptuous as vol

    with patch("custom_components.microsoft_family_safety._get_coordinator", return_value=MagicMock()):
        _register_services(hass)
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN, "add_time_bonus",
                {"account_id": CHILD, "platform": "Mobile", "minutes": 5}, blocking=True,
            )


def test_platform_is_accepted_in_any_case() -> None:
    """add_time_bonus / cancel_time_bonus take `xbox` like every other platform field (#84)."""
    from custom_components.microsoft_family_safety import (
        SERVICE_ADD_TIME_BONUS_SCHEMA,
        SERVICE_CANCEL_TIME_BONUS_SCHEMA,
    )

    for schema in (SERVICE_ADD_TIME_BONUS_SCHEMA, SERVICE_CANCEL_TIME_BONUS_SCHEMA):
        assert schema({"account_id": "1", "platform": " xbox "})["platform"] == "Xbox"
        assert schema({"account_id": "1", "platform": "windows"})["platform"] == "Windows"
        with pytest.raises(Exception):
            schema({"account_id": "1", "platform": "mobile"})
