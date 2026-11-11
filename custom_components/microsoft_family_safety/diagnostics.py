"""Diagnostics for Microsoft Family Safety, with credentials and personal data removed.

Tokens, cookies, e-mail addresses, names and browsing data are redacted by
key. Account, device and Xbox user ids, and the parent's user id, are replaced
by stable aliases ("account_1", "device_1", "xbox_user_1", "parent") wherever
they appear, also inside dictionary keys and longer strings (an endpoint
path), so the structure stays readable without identifying anyone.
"""
from __future__ import annotations

import re
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ALLOWED_USERS,
    CONF_API_KEY,
    CONF_AUTH_URL,
    CONF_AUTH_USER_ID,
    CONF_REDIRECT_URL,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN,
    CONF_WEB_COOKIES,
    CONF_WEB_FAMILY_REFERER,
    CONF_WEB_FAMILY_TOKEN,
    DOMAIN,
)

TO_REDACT = {
    # credentials
    CONF_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_REDIRECT_URL,
    CONF_WEB_COOKIES,
    CONF_WEB_FAMILY_TOKEN,
    CONF_WEB_FAMILY_REFERER,
    CONF_API_KEY,
    CONF_AUTH_URL,
    "access_token",
    "cookies",
    # people
    CONF_AUTH_USER_ID,
    CONF_ALLOWED_USERS,
    "first_name",
    "surname",
    "profile_picture",
    "email",
    "emailAddress",
    "displayName",
    "gamertag",
    "requester",
    "device_name",
    "title",
    "account_balance",
    # browsing: site lists, websites visited and searches
    "exceptions",
    "Exceptions",
    "blockedSites",
    "allowedSites",
    "BlockedSites",
    "AllowedSites",
    "web_activity",
}

_REDACTED = "**REDACTED**"
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")


def _aliases(entry: ConfigEntry, data: dict[str, Any], coordinator: Any) -> dict[str, str]:
    """Ids -> aliases, longest first so that none is replaced inside another."""
    aliases: dict[str, str] = {}
    for index, account_id in enumerate((data.get("accounts") or {}), start=1):
        aliases[str(account_id)] = f"account_{index}"
    for index, device_id in enumerate((data.get("devices") or {}), start=1):
        aliases.setdefault(str(device_id), f"device_{index}")
    xuids = getattr(coordinator, "_xbox_xuids", None) or {}
    for index, xuid in enumerate(sorted({str(x) for x in xuids.values() if x}), start=1):
        aliases.setdefault(xuid, f"xbox_user_{index}")
    parent = entry.data.get(CONF_AUTH_USER_ID)
    if parent:
        aliases.setdefault(str(parent), "parent")
    return dict(sorted(aliases.items(), key=lambda item: -len(item[0])))


def _anonymise(value: Any, aliases: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {
            _anonymise(str(key), aliases): _anonymise(item, aliases)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_anonymise(item, aliases) for item in value]
    if isinstance(value, int) and not isinstance(value, bool) and str(value) in aliases:
        return aliases[str(value)]
    if isinstance(value, str):
        for real, alias in aliases.items():
            if real and real in value:
                value = value.replace(real, alias)
        return _EMAIL.sub(_REDACTED, value)
    return value


def _account_summary(account: dict[str, Any]) -> dict[str, Any]:
    """An account's data without the long app list."""
    summary = {k: v for k, v in account.items() if k != "applications"}
    summary["application_count"] = len(account.get("applications") or [])
    return summary


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    coordinator = (hass.data.get(DOMAIN) or {}).get(entry.entry_id)
    data: dict[str, Any] = dict(getattr(coordinator, "data", None) or {})
    aliases = _aliases(entry, data, coordinator)
    result: dict[str, Any] = {
        "entry": {
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
    }
    if coordinator is None:
        return _anonymise(result, aliases)
    result["connection"] = async_redact_data(coordinator.connection_state(), TO_REDACT)
    result["data"] = async_redact_data(
        {
            "accounts": {
                key: _account_summary(value)
                for key, value in (data.get("accounts") or {}).items()
                if isinstance(value, dict)
            },
            "devices": data.get("devices") or {},
            "pending_request_count": len(data.get("pending_requests") or []),
        },
        TO_REDACT,
    )
    result["saved_screentime_accounts"] = len(getattr(coordinator, "_saved_screentime", {}))
    return _anonymise(result, aliases)
