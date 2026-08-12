"""Runtime compatibility patches for pyfamilysafety 1.1.2.

The pinned PyPI release of ``pyfamilysafety`` (1.1.2, the newest published
version) has two problems that surface on Home Assistant, especially on
Python 3.14:

1. ``Authenticator._request_handler`` creates a brand new
   ``aiohttp.ClientSession()`` on *every* auth/refresh request and never
   reuses Home Assistant's shared session. Beyond leaking sessions, this is
   the line that throws ``TypeError: 'ClientSession' object is not callable``
   when another component in the same process has replaced the
   ``aiohttp.ClientSession`` symbol with an instance (observed in the wild
   together with the Family Link integration — see issue #22). It is also the
   root cause of the cascading 400/401 failures in issues #20 and #23.

2. ``_request_handler`` calls ``await resp.json()`` unconditionally. When
   Microsoft answers an expired/invalid session with an HTML error page
   (the recurring 400 in issue #23), that call raises and crashes the whole
   update cycle instead of surfacing a clean status code.

This module monkey-patches ``Authenticator._request_handler`` to:
- reuse Home Assistant's shared aiohttp session (no per-request session,
  no dependency on the fragile module-level ``aiohttp.ClientSession`` symbol);
- decode JSON defensively so non-JSON error bodies don't crash the refresh.

The patch is idempotent and only ever wraps the original once.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import inspect
import logging
from typing import Any

import aiohttp
from pyfamilysafety.authenticator import Authenticator
from pyfamilysafety.authenticator.const import USER_AGENT

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

# Marker so we never double-patch.
_PATCH_MARKER = "_hafs_shared_session_patch"

# The HA-managed session, injected at setup time.
_shared_session: aiohttp.ClientSession | None = None


def set_shared_session(session: aiohttp.ClientSession) -> None:
    """Register the Home Assistant shared aiohttp session for the patch."""
    global _shared_session
    _shared_session = session


async def _patched_request_handler(
    self: Authenticator,
    method: str,
    url: str,
    body: Any = None,
    headers: dict | None = None,
    data: Any = None,
) -> dict:
    """Drop-in replacement for ``Authenticator._request_handler``.

    Reuses the shared HA session instead of creating a new ClientSession,
    and decodes the response body defensively.
    """
    response: dict = {"status": 0, "text": "", "json": "", "headers": ""}

    session = _shared_session
    if session is None or session.closed:
        # Fallback: behave like the original but without depending on the
        # module-level ClientSession symbol being a class. This still avoids
        # the "object is not callable" failure mode because we hold a real
        # class reference here.
        _LOGGER.debug("Shared session unavailable, using a temporary session")
        session_cm = aiohttp.ClientSession()
    else:
        session_cm = None

    active_session = session_cm if session_cm is not None else session

    req_headers = {
        "user-agent": USER_AGENT,
        "X-Requested-With": "com.microsoft.familysafety",
    }
    if headers:
        req_headers.update(headers)

    try:
        async with active_session.request(
            method=method,
            url=url,
            json=body,
            headers=req_headers,
            data=data,
        ) as resp:
            response["status"] = resp.status
            response["text"] = await resp.text()
            response["headers"] = resp.headers
            try:
                response["json"] = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                # Non-JSON body (e.g. Microsoft HTML error page). Keep the
                # status/text so callers can react instead of crashing.
                _LOGGER.debug(
                    "Auth response from %s was not JSON (status %s)",
                    url, resp.status,
                )
                response["json"] = {}
    finally:
        if session_cm is not None:
            await session_cm.close()

    return response


def apply_patches(hass: HomeAssistant) -> None:
    """Apply the pyfamilysafety compatibility patches (idempotent)."""
    set_shared_session(async_get_clientsession(hass))

    if getattr(Authenticator._request_handler, _PATCH_MARKER, False):
        return

    setattr(_patched_request_handler, _PATCH_MARKER, True)
    Authenticator._request_handler = _patched_request_handler
    _LOGGER.debug(
        "Applied pyfamilysafety compatibility patch "
        "(shared session + tolerant JSON decode)"
    )


def authenticator_access_token_expired(authenticator: Authenticator) -> bool:
    """Return token-expiry state across pyfamilysafety API versions.

    pyfamilysafety 1.1.2 has ``expires`` but no ``access_token_expired``
    property. Newer releases expose the property. A small safety margin avoids
    starting a request with a token that is about to expire.
    """
    try:
        value = getattr(authenticator, "access_token_expired")
    except AttributeError:
        value = None
    if value is not None:
        return bool(value() if callable(value) else value)

    if not getattr(authenticator, "access_token", None):
        return True
    expires = getattr(authenticator, "expires", None)
    if expires is None:
        return True
    now = datetime.now(tz=expires.tzinfo) if getattr(expires, "tzinfo", None) else datetime.now()
    return expires <= now + timedelta(seconds=60)


async def create_authenticator(
    hass: HomeAssistant,
    token: str,
    *,
    use_refresh_token: bool,
) -> Authenticator:
    """Create an Authenticator across pyfamilysafety API versions.

    pyfamilysafety 1.1.2 exposes ``Authenticator.create(token,
    use_refresh_token=False)`` and creates its own aiohttp session inside the
    request handler. Newer releases accept ``client_session``. Our request
    handler patch always uses Home Assistant's shared session, so on 1.1.2 we
    intentionally omit the unsupported constructor argument.
    """
    apply_patches(hass)
    create = Authenticator.create
    parameters = inspect.signature(create).parameters
    kwargs: dict[str, Any] = {
        "token": token,
        "use_refresh_token": use_refresh_token,
    }
    if "client_session" in parameters:
        kwargs["client_session"] = async_get_clientsession(hass)
        _LOGGER.debug(
            "Creating pyfamilysafety Authenticator with Home Assistant client session"
        )
    else:
        _LOGGER.debug(
            "Creating pyfamilysafety Authenticator using legacy create() signature"
        )
    return await create(**kwargs)
