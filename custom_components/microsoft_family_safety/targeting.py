"""Which child a service call is about: ``account_id``, or one of its entities or devices.

Every per-child service takes ``account_id`` (the Microsoft user id) as it
always has, or ``entity_id``: any entity of this integration that belongs to
the child, either on the child's own device or on one of its physical
devices. The account is read from the entity's device in the registry, then
from its ``user_id`` attribute. ``device_id`` works the same way for a device
of this integration (the child's own device or one of its physical devices).
``account_id`` wins when it is given; entities and devices must all belong to
the same child.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import ATTR_USER_ID, DOMAIN

ATTR_ACCOUNT_ID = "account_id"
ATTR_ENTITY_ID = "entity_id"
ATTR_DEVICE_ID = "device_id"

#: Fields every per-child service accepts; at least one of them is required.
TARGET_FIELDS: dict[Any, Any] = {
    vol.Optional(ATTR_ACCOUNT_ID): cv.string,
    # One entity, or a list (what a "target:" with entities gives).
    vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
    # One device, or a list (what a "target:" with devices gives).
    vol.Optional(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
}


def _has_target(data: dict[str, Any]) -> dict[str, Any]:
    account_id = str(data.get(ATTR_ACCOUNT_ID) or "").strip()
    # A template that rendered nothing gives "" or "None": no account.
    if account_id.lower() == "none":
        account_id = ""
    if not account_id and not data.get(ATTR_ENTITY_ID) and not data.get(ATTR_DEVICE_ID):
        raise vol.Invalid(
            "Give the child: account_id, or entity_id / device_id of one of its "
            "Microsoft Family Safety entities or devices"
        )
    if account_id:
        data[ATTR_ACCOUNT_ID] = account_id
    else:
        data.pop(ATTR_ACCOUNT_ID, None)
    return data


def child_schema(fields: dict[Any, Any]) -> vol.All:
    """Schema of a per-child service: its own fields plus the child target."""
    return vol.All(vol.Schema({**TARGET_FIELDS, **fields}), _has_target)


def _known_accounts(hass: HomeAssistant) -> tuple[set[str], dict[str, str]]:
    """Account ids, and physical device id -> account id, across all entries."""
    accounts: set[str] = set()
    devices: dict[str, str] = {}
    for coordinator in (hass.data.get(DOMAIN) or {}).values():
        data = getattr(coordinator, "data", None) or {}
        if not isinstance(data, dict):
            continue
        accounts.update(str(key) for key in (data.get("accounts") or {}))
        for device_id, device in (data.get("devices") or {}).items():
            if isinstance(device, dict) and device.get("account_id") is not None:
                devices[str(device_id)] = str(device["account_id"])
    return accounts, devices


def _account_from_device(
    hass: HomeAssistant, device_id: str, accounts: set[str], devices: dict[str, str]
) -> str | None:
    """The account behind a device of this integration, or ``None``."""
    device = dr.async_get(hass).async_get(device_id)
    for domain, identifier in (device.identifiers if device else ()):
        if domain != DOMAIN:
            continue
        identifier = str(identifier)
        if identifier in accounts:
            return identifier
        if identifier in devices:
            return devices[identifier]
    return None


def account_from_entity(hass: HomeAssistant, entity_id: str) -> str:
    """The account id behind one of this integration's entities."""
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None or entry.platform != DOMAIN:
        raise ServiceValidationError(
            f"{entity_id} is not a Microsoft Family Safety entity. Select an "
            "entity of the child, or pass account_id."
        )
    if entry.device_id:
        accounts, devices = _known_accounts(hass)
        account = _account_from_device(hass, entry.device_id, accounts, devices)
        if account is not None:
            return account
    state = hass.states.get(entity_id)
    user_id = state.attributes.get(ATTR_USER_ID) if state else None
    if user_id not in (None, ""):
        return str(user_id)
    raise ServiceValidationError(
        f"{entity_id} does not belong to a child account (it is, for example, "
        "the connection sensor). Select an entity of the child, or pass account_id."
    )


def account_from_device(hass: HomeAssistant, device_id: str) -> str:
    """The account id behind one of this integration's devices."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None or not any(domain == DOMAIN for domain, _ in device.identifiers):
        raise ServiceValidationError(
            f"Device {device_id} is not a Microsoft Family Safety device. Select a "
            "device of the child, or pass account_id."
        )
    accounts, devices = _known_accounts(hass)
    account = _account_from_device(hass, device_id, accounts, devices)
    if account is not None:
        return account
    raise ServiceValidationError(
        f"Device {device.name_by_user or device.name or device_id} does not belong "
        "to a child account (it is, for example, the connection device). Select a "
        "device of the child, or pass account_id."
    )


def _as_list(value: Any) -> list[str]:
    if not value:
        return []
    return [value] if isinstance(value, str) else list(value)


def resolve_account_id(hass: HomeAssistant, data: dict[str, Any]) -> str:
    """The child a validated service call targets."""
    account_id = data.get(ATTR_ACCOUNT_ID)
    if account_id:
        return str(account_id)
    entity_ids = _as_list(data.get(ATTR_ENTITY_ID))
    device_ids = _as_list(data.get(ATTR_DEVICE_ID))
    found = [account_from_entity(hass, e) for e in entity_ids]
    found += [account_from_device(hass, d) for d in device_ids]
    accounts = list(dict.fromkeys(found))
    if len(accounts) > 1:
        raise ServiceValidationError(
            f"The selected entities and devices belong to {len(accounts)} different "
            "children; target one child per call."
        )
    if accounts:
        return accounts[0]
    raise ServiceValidationError("Give the child: account_id, entity_id or device_id.")
