"""Who may control Microsoft Family Safety from Home Assistant.

Home Assistant lets every user press any button, flip any switch or change any
number unless an entity permission policy says otherwise, and core has no UI to
write such policies. A child with a Home Assistant account could therefore
lift their own limits from the entities of this integration.

The "Users allowed to control Microsoft Family Safety" option closes that gap.
When it lists users, a person who is neither an administrator nor in the list
is refused on every entity action of the integration (button, switch, number,
time) and on every ``microsoft_family_safety.*`` service. An empty list keeps
the previous behaviour, so nothing changes until a parent fills it in.

Calls without a user (automations, scripts run by Home Assistant itself) are
always allowed. Administrators are always allowed too: they can change the
option anyway, so the child's account must not be an administrator.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
import logging
from typing import Any

from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.exceptions import Unauthorized, UnknownUser

from .const import CONF_ALLOWED_USERS, DOMAIN

_LOGGER = logging.getLogger(__name__)


def allowed_user_ids(hass: HomeAssistant) -> set[str] | None:
    """Users the option allows, across every entry; ``None`` when no entry restricts."""
    allowed: set[str] = set()
    restricted = False
    for entry in hass.config_entries.async_entries(DOMAIN):
        users = entry.options.get(CONF_ALLOWED_USERS) or []
        if users:
            restricted = True
            allowed.update(users)
    return allowed if restricted else None


async def _async_check(hass: HomeAssistant, context: Context | None, action: str) -> None:
    """Raise ``Unauthorized`` for a non-administrator the option does not list."""
    user_id = context.user_id if context else None
    if not user_id:
        # No user: an automation or a script started by Home Assistant itself.
        return
    allowed = allowed_user_ids(hass)
    if allowed is None:
        return
    user = await hass.auth.async_get_user(user_id)
    if user is None:
        raise UnknownUser(context=context, permission=POLICY_CONTROL)
    if user.is_admin or user.id in allowed:
        return
    _LOGGER.warning(
        "%s refused: user %s is not in the users allowed to control "
        "Microsoft Family Safety",
        action, user.name,
    )
    raise Unauthorized(context=context, permission=POLICY_CONTROL)


async def async_check_entity_action(entity: Any) -> None:
    """Raise ``Unauthorized`` unless the user behind this entity action is allowed.

    Home Assistant sets the context of the service call on the entity right
    before it calls the action method, so ``entity._context`` names the user
    who pressed the button or flipped the switch.
    """
    await _async_check(
        entity.hass, getattr(entity, "_context", None), f"Action on {entity.entity_id}"
    )


async def async_check_service_call(hass: HomeAssistant, call: ServiceCall) -> None:
    """Raise ``Unauthorized`` unless the caller of this service is allowed."""
    await _async_check(hass, call.context, f"Service {call.domain}.{call.service}")


def guard_service(
    hass: HomeAssistant, handler: Callable[[ServiceCall], Awaitable[Any]]
) -> Callable[[ServiceCall], Awaitable[Any]]:
    """Wrap a service handler so it only runs for an allowed caller."""

    @wraps(handler)
    async def guarded(call: ServiceCall) -> Any:
        await async_check_service_call(hass, call)
        return await handler(call)

    guarded.familysafety_guarded = True  # checked by the tests: no service may skip the guard
    return guarded
