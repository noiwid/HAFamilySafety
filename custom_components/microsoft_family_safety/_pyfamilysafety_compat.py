"""Runtime compatibility patches for pyfamilysafety 1.1.2.

The pinned PyPI release of ``pyfamilysafety`` (1.1.2) has several problems
that surface on Home Assistant, especially on Python 3.14:

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

3. ``Authenticator.perform_refresh`` silently returns when Microsoft rejects
   a refresh-token grant. The stale access token is then reused until the
   mobile API answers ``HTTP Unauthorized``.

4. In 1.1.2 ``Authenticator.access_token`` is the raw token value. Newer
   releases expose the complete ``MSAuth1.0`` Authorization value instead.
   Code written against the newer API can therefore send a raw token to the
   mobile aggregator and receive ``Authentication.BadCompactTicket``.

This module patches those paths while keeping token values out of the log.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import inspect
import logging
from typing import Any

import aiohttp
from pyfamilysafety.api import FamilySafetyAPI
from pyfamilysafety.authenticator import Authenticator
from pyfamilysafety.authenticator.const import (
    CLIENT_ID,
    SCOPE,
    TOKEN_ENDPOINT,
    USER_AGENT,
)
from pyfamilysafety.exceptions import HttpException, Unauthorized

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

# Markers so we never double-patch.
_REQUEST_PATCH_MARKER = "_hafs_shared_session_patch"
_REFRESH_PATCH_MARKER = "_hafs_refresh_validation_patch"
_API_PATCH_MARKER = "_hafs_unauthorized_retry_patch"
_WEB_API_HEADER_PATCH_MARKER = "_hafs_mobile_authorization_header_patch"

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


def _set_access_token(authenticator: Authenticator, token: str) -> None:
    """Set the raw access token across pyfamilysafety API versions."""
    if hasattr(authenticator, "_access_token"):
        authenticator._access_token = token
    else:
        authenticator.access_token = token


def authenticator_authorization_header(authenticator: Authenticator) -> str:
    """Return the mobile API Authorization value across library versions.

    pyfamilysafety 1.1.2 stores the raw access token in ``access_token`` and
    constructs the MSAuth wrapper inside FamilySafetyAPI. Newer releases expose
    the already wrapped value through the property itself. Never log the value.
    """
    value = str(getattr(authenticator, "access_token", "") or "")
    if not value:
        return value
    if value.lstrip().lower().startswith("msauth1.0 "):
        return value
    return f'MSAuth1.0 usertoken="{value}", type="MSACT"'


def _oauth_error(payload: object) -> str | None:
    """Extract a non-secret OAuth error code."""
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, dict) and error.get("code"):
        return str(error["code"])
    return None


async def _patched_perform_refresh(self: Authenticator) -> None:
    """Refresh the mobile token and fail when Microsoft rejects the grant."""
    lock = self._login_lock
    if lock.locked():
        _LOGGER.debug("Mobile token refresh already in progress; waiting")
        async with lock:
            pass
        if authenticator_access_token_expired(self):
            raise HttpException(
                "authentication failed: concurrent mobile token refresh "
                "did not produce a valid access token"
            )
        return

    async with lock:
        old_refresh_token = self.refresh_token
        _LOGGER.info(
            "Refreshing Microsoft Family Safety mobile access token "
            "(previous_expiry=%s)",
            self.expires.isoformat() if isinstance(self.expires, datetime) else None,
        )

        form = aiohttp.FormData()
        form.add_field("client_id", CLIENT_ID)
        form.add_field("refresh_token", old_refresh_token or "")
        form.add_field("grant_type", "refresh_token")
        form.add_field("scope", SCOPE)

        try:
            tokens = await self._request_handler(
                method="POST", url=TOKEN_ENDPOINT, data=form
            )
        except Exception as err:
            _LOGGER.warning(
                "Microsoft Family Safety mobile token refresh request failed: %s",
                type(err).__name__,
            )
            raise

        status = tokens.get("status")
        payload = tokens.get("json")
        error = _oauth_error(payload)
        if status != 200 or not isinstance(payload, dict):
            _LOGGER.error(
                "Microsoft Family Safety mobile token refresh rejected: "
                "status=%s error=%s",
                status, error,
            )
            raise HttpException(
                "authentication failed during mobile token refresh",
                status,
                error or "token_endpoint_rejected",
            )

        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        expires_in = payload.get("expires_in")
        if not access_token or not refresh_token or expires_in is None:
            _LOGGER.error(
                "Microsoft Family Safety mobile token refresh returned an "
                "incomplete response (status=%s)",
                status,
            )
            raise HttpException(
                "authentication failed: incomplete mobile token refresh response"
            )

        try:
            expires_seconds = int(expires_in)
        except (TypeError, ValueError) as err:
            raise HttpException(
                "authentication failed: invalid mobile token expiry"
            ) from err

        _set_access_token(self, str(access_token))
        self.expires = datetime.now() + timedelta(seconds=expires_seconds)
        self.refresh_token = str(refresh_token)
        if payload.get("user_id") is not None:
            self.user_id = payload["user_id"]

        _LOGGER.info(
            "Microsoft Family Safety mobile token refresh succeeded "
            "(expires_in=%ss refresh_token_rotated=%s)",
            expires_seconds,
            bool(old_refresh_token and self.refresh_token != old_refresh_token),
        )


_original_send_request = FamilySafetyAPI.send_request


async def _patched_send_request(
    self: FamilySafetyAPI,
    endpoint: str,
    body: object = None,
    headers: dict | None = None,
    platform: str | None = None,
    **kwargs: Any,
):
    """Retry one mobile API 401 after forcing a fresh access token."""
    try:
        return await _original_send_request(
            self,
            endpoint,
            body=body,
            headers=headers,
            platform=platform,
            **kwargs,
        )
    except Unauthorized as err:
        authenticator = getattr(self, "authenticator", None)
        if authenticator is None:
            raise HttpException(
                "authentication failed: mobile API returned HTTP Unauthorized"
            ) from err

        _LOGGER.warning(
            "Microsoft Family Safety mobile API returned HTTP Unauthorized "
            "for endpoint=%s; forcing one token refresh and retry",
            endpoint,
        )
        await authenticator.perform_refresh()

        session = getattr(self, "_session", None)
        if session is not None:
            try:
                session.headers.pop("Authorization")
            except KeyError:
                pass

        try:
            result = await _original_send_request(
                self,
                endpoint,
                body=body,
                headers=headers,
                platform=platform,
                **kwargs,
            )
        except Unauthorized as retry_err:
            _LOGGER.error(
                "Microsoft Family Safety mobile API still returned HTTP "
                "Unauthorized after token refresh for endpoint=%s",
                endpoint,
            )
            raise HttpException(
                "authentication failed: HTTP Unauthorized after mobile token refresh"
            ) from retry_err

        _LOGGER.info(
            "Microsoft Family Safety mobile API recovered after forced token "
            "refresh for endpoint=%s",
            endpoint,
        )
        return result


def _patch_combined_client_mobile_header() -> bool:
    """Normalize the combined client's mobile Authorization header.

    Import lazily to avoid a module import cycle: api_client imports this compat
    module, while this patch is only applied later during coordinator setup.
    """
    try:
        from .api_client import FamilySafetyWebAPI
    except (ImportError, AttributeError):
        return False

    original = FamilySafetyWebAPI._build_headers
    if getattr(original, _WEB_API_HEADER_PATCH_MARKER, False):
        return False

    def _patched_build_headers(self) -> dict[str, str]:
        headers = original(self)
        current = headers.get("Authorization", "")
        normalized = authenticator_authorization_header(self._authenticator)
        headers["Authorization"] = normalized
        if current != normalized and not getattr(
            self, "_legacy_mobile_auth_header_logged", False
        ):
            _LOGGER.debug(
                "Normalized legacy pyfamilysafety raw mobile access token to "
                "MSAuth1.0 Authorization format"
            )
            self._legacy_mobile_auth_header_logged = True
        return headers

    setattr(_patched_build_headers, _WEB_API_HEADER_PATCH_MARKER, True)
    FamilySafetyWebAPI._build_headers = _patched_build_headers
    return True


def apply_patches(hass: HomeAssistant) -> None:
    """Apply the pyfamilysafety compatibility patches (idempotent)."""
    set_shared_session(async_get_clientsession(hass))
    applied: list[str] = []

    if not getattr(Authenticator._request_handler, _REQUEST_PATCH_MARKER, False):
        setattr(_patched_request_handler, _REQUEST_PATCH_MARKER, True)
        Authenticator._request_handler = _patched_request_handler
        applied.append("shared session + tolerant JSON decode")

    if not getattr(Authenticator.perform_refresh, _REFRESH_PATCH_MARKER, False):
        setattr(_patched_perform_refresh, _REFRESH_PATCH_MARKER, True)
        Authenticator.perform_refresh = _patched_perform_refresh
        applied.append("validated mobile token refresh")

    if not getattr(FamilySafetyAPI.send_request, _API_PATCH_MARKER, False):
        setattr(_patched_send_request, _API_PATCH_MARKER, True)
        FamilySafetyAPI.send_request = _patched_send_request
        applied.append("single Unauthorized refresh retry")

    if _patch_combined_client_mobile_header():
        applied.append("legacy mobile Authorization header normalization")

    if applied:
        _LOGGER.debug(
            "Applied pyfamilysafety compatibility patches (%s)",
            "; ".join(applied),
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
