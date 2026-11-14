"""Dashboard cards.

The integration serves frontend/familysafety-card.js (its own per-child card)
and frontend/parental-screen-time.js (the screen time cards shared with Google
Family Link, the same file ships there; whichever loads first defines the
elements) and adds both to every dashboard. A websocket command gives the cards the entities of one
device, found by unique id, so the card config only names the device.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import async_get_integration

from .const import DAY_KEYS, DOMAIN

URL_BASE = "/familysafety_card"
FRONTEND = Path(__file__).parent / "frontend"
# Outside hass.data[DOMAIN], which is dropped when the last entry unloads.
_REGISTERED = f"{DOMAIN}_card_registered"


async def async_register_card(hass: HomeAssistant) -> None:
    """Serve the cards and register their websocket command, once per instance."""
    if hass.data.get(_REGISTERED):
        return
    hass.data[_REGISTERED] = True
    await hass.http.async_register_static_paths(
        [StaticPathConfig(URL_BASE, str(FRONTEND), cache_headers=False)]
    )
    # Version and file date in the URL: browsers and the mobile app fetch a changed file
    # after the next restart, even between two releases.
    version = (await async_get_integration(hass, DOMAIN)).version
    for name in ("parental-screen-time.js", "familysafety-card.js"):
        stamp = await hass.async_add_executor_job(_mtime, FRONTEND / name)
        add_extra_js_url(hass, f"{URL_BASE}/{name}?v={version}-{stamp}")
    websocket_api.async_register_command(hass, ws_card_entities)


def _mtime(path: Path) -> int:
    try:
        return int(path.stat().st_mtime)
    except OSError:
        return 0


#: Why the child's own device does not answer (the cards show it).
CHILD_ACCOUNT_MESSAGE = (
    "This is the child's Microsoft Family Safety account, not a device: choose its PC or its Xbox"
)


class ChildAccountDevice(Exception):
    """The entity belongs to the child's account device, which the cards do not take."""


def card_entities(hass: HomeAssistant, entity_id: str) -> dict[str, Any] | None:
    """Entities of the HA device that ``entity_id`` belongs to, by card role.

    A Windows PC gets its own time and the child's Windows lock, Windows time
    remaining and daily limits; an Xbox the child's Xbox ones. Other devices
    only have their time. None when the entity is not ours or has no time
    sensor. The child's own device raises ChildAccountDevice: its time is the
    total of its PC and Xbox, so taking it along with them would count it
    twice (the account total can still be added to a card as a plain source).
    """
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is None or entry.platform != DOMAIN:
        return None
    data = getattr(hass.data.get(DOMAIN, {}).get(entry.config_entry_id), "data", None) or {}
    prefix = f"{entry.config_entry_id}_"
    rest = entry.unique_id.removeprefix(prefix)

    def owner(ids: Any) -> Any:
        # Roster ids can be int: compare them as text, keep the key as it is.
        return next((i for i in ids if rest.startswith(f"{i}_")), None)

    def find(domain: str, suffix: str) -> str | None:
        return registry.async_get_entity_id(domain, DOMAIN, f"{prefix}{suffix}")

    devices = data.get("devices") or {}
    device_id = owner(devices)
    if device_id is not None:
        device = devices[device_id] or {}
        account_id = device.get("account_id")
        # Same test as coordinator._windows_usage
        kind = f"{device.get('os_name') or ''} {device.get('device_class') or ''}".lower()
        platform = "xbox" if "xbox" in kind else "windows" if "windows" in kind else None
        used = find("sensor", f"{device_id}_screentime")
        if platform == "xbox":
            used = find("sensor", f"{account_id}_xbox_screentime") or used
    else:
        if owner(data.get("accounts") or {}) is not None:
            raise ChildAccountDevice
        return None
    if used is None:
        return None

    roles: dict[str, str | None] = {"daily_screen_time": used}
    child: dict[str, str | None] = {}
    if platform is not None:
        limit = "xbox_limit" if platform == "xbox" else "limit"
        roles["screen_time_remaining"] = find("sensor", f"{account_id}_{platform}_time_remaining")
        roles["lock"] = find("switch", f"{account_id}_platform_{platform}")
        # The cards number the days 1 = Monday .. 7 = Sunday; DAY_KEYS starts on Sunday.
        child = {
            f"daily_limit_{n}": find("number", f"{account_id}_{limit}_{DAY_KEYS[n % 7]}")
            for n in range(1, 8)
        }
    return {
        "device": {k: v for k, v in roles.items() if v},
        "child": {k: v for k, v in child.items() if v},
        "locked_when": "on",
    }


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/card_entities", vol.Required("entity_id"): str}
)
@callback
def ws_card_entities(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return the entities the cards need for the device of ``entity_id``."""
    try:
        result = card_entities(hass, msg["entity_id"])
    except ChildAccountDevice:
        connection.send_error(msg["id"], "not_found", CHILD_ACCOUNT_MESSAGE)
        return
    if result is None:
        connection.send_error(
            msg["id"], "not_found", f"{msg['entity_id']} is not a Microsoft Family Safety device entity"
        )
        return
    connection.send_result(msg["id"], result)
