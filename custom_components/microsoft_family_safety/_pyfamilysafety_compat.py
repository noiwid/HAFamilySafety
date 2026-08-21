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

5. The combined client preferred slow private web endpoints for data that is
   also available from the authenticated mobile aggregator. A web timeout could
   therefore hide otherwise readable policy data and make every coordinator
   cycle take more than a minute.

6. The supplied browser HAR proves that the active Windows/per-device screen-
   time schedule is read directly from ``/family/api/st?childId=...``. Earlier
   compatibility code guessed that the mobile PATCH schedule resource might
   also support GET, which Microsoft rejects with HTTP 400.

7. Runtime logs show intermittent ``Timeout while contacting DNS servers``
   errors from aiohttp's resolver path, including for both account.microsoft.com
   and the mobile aggregator. The Family web client therefore uses an explicit
   threaded system resolver with IPv4, matching the successful browser HAR's
   IPv4 transport while leaving the rest of Home Assistant untouched.

This module patches those paths while keeping token values out of the log.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import inspect
import logging
import socket
import time
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
_DATA_SOURCE_PATCH_MARKER = "_hafs_mobile_first_data_sources_patch"
_WEB_PROBE_PATCH_MARKER = "_hafs_web_probe_backoff_patch"
_WEB_TRANSPORT_PATCH_MARKER = "_hafs_ipv4_threaded_web_transport_patch"
_CONNECTION_STATE_PATCH_MARKER = "_hafs_connection_state_diagnostics_patch"

# Back off private account.microsoft.com probes after transport failures. Mobile
# data continues to refresh normally during the backoff window.
_WEB_BACKOFF_SECONDS = 30 * 60

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
    """Return the mobile API Authorization value across library versions."""
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
    """Normalize the combined client's mobile Authorization header."""
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


def _patch_web_transport() -> bool:
    """Use a stable IPv4/threaded resolver for private Family web traffic.

    This patch is intentionally scoped to the integration's dedicated browser
    session. It does not change Home Assistant's global resolver or the mobile
    Family Safety client.
    """
    try:
        from .api_client import FamilySafetyWebAPI
    except (ImportError, AttributeError):
        return False

    original = FamilySafetyWebAPI._get_web_session
    if getattr(original, _WEB_TRANSPORT_PATCH_MARKER, False):
        return False

    def _patched_get_web_session(self):
        if self._web_session is None or self._web_session.closed:
            connector = aiohttp.TCPConnector(
                resolver=aiohttp.ThreadedResolver(),
                family=socket.AF_INET,
                ttl_dns_cache=300,
            )
            self._web_session = aiohttp.ClientSession(
                connector=connector,
                cookie_jar=self._build_cookie_jar(),
                timeout=aiohttp.ClientTimeout(
                    total=20, connect=8, sock_connect=8, sock_read=15
                ),
            )
            self._hafs_web_transport = "ipv4_threaded_resolver"
            _LOGGER.debug(
                "Created Microsoft Family web session with IPv4 threaded DNS resolver"
            )
        return self._web_session

    setattr(_patched_get_web_session, _WEB_TRANSPORT_PATCH_MARKER, True)
    FamilySafetyWebAPI._get_web_session = _patched_get_web_session
    return True


def _patch_web_probe_backoff() -> bool:
    """Back off browser-session probes after transport failures."""
    try:
        from .api_client import FamilySafetyWebAPI
    except (ImportError, AttributeError):
        return False

    original = FamilySafetyWebAPI.async_check_web_session
    if getattr(original, _WEB_PROBE_PATCH_MARKER, False):
        return False

    async def _patched_check_web_session(self) -> bool:
        now = time.monotonic()
        backoff_until = float(getattr(self, "_web_probe_backoff_until", 0.0) or 0.0)
        if backoff_until > now:
            error = str(getattr(self, "_web_probe_backoff_error", "TIMEOUT"))
            self.web_session_state = "error"
            self.last_web_error_code = error
            _LOGGER.debug(
                "Skipping Microsoft account web-session probe during transport backoff "
                "(error=%s remaining=%ds)",
                error,
                int(backoff_until - now),
            )
            return False

        result = await original(self)
        if self.web_session_state == "error" and self.last_web_error_code in {
            "TIMEOUT",
            "NETWORK_ERROR",
        }:
            self._web_probe_backoff_until = time.monotonic() + _WEB_BACKOFF_SECONDS
            self._web_probe_backoff_error = self.last_web_error_code
            _LOGGER.debug(
                "Backing off Microsoft account web-session probes for %d seconds "
                "after %s",
                _WEB_BACKOFF_SECONDS,
                self.last_web_error_code,
            )
        elif self.web_session_state == "authenticated":
            self._web_probe_backoff_until = 0.0
            self._web_probe_backoff_error = None
        return result

    setattr(_patched_check_web_session, _WEB_PROBE_PATCH_MARKER, True)
    FamilySafetyWebAPI.async_check_web_session = _patched_check_web_session
    return True


def _patch_combined_client_data_sources() -> bool:
    """Prefer authenticated mobile reads and HAR-confirmed policy reads."""
    try:
        from .api_client import FamilySafetyWebAPI
    except (ImportError, AttributeError):
        return False

    original_web_browsing = FamilySafetyWebAPI.get_web_browsing_settings
    original_content_settings = FamilySafetyWebAPI.get_content_settings
    original_screentime_policy = FamilySafetyWebAPI.get_screentime_policy
    if getattr(original_web_browsing, _DATA_SOURCE_PATCH_MARKER, False):
        return False

    async def _patched_get_web_browsing_settings(self, child_id: str):
        self.web_browsing_source = None
        self.web_browsing_status = "checking"
        try:
            result = await self._request("GET", f"/v1/WebRestrictions/{child_id}")
        except Exception as err:
            self.web_browsing_status = "mobile_error"
            _LOGGER.debug(
                "Mobile WebRestrictions read failed for child=%s; trying web fallback: %r",
                child_id,
                err,
            )
        else:
            if isinstance(result, dict):
                self.web_browsing_source = "mobile"
                self.web_browsing_status = "ok"
                _LOGGER.debug(
                    "Loaded web browsing restrictions from mobile API for child=%s",
                    child_id,
                )
                return result
            self.web_browsing_status = "mobile_no_data"

        if not self.has_web_cookies:
            return None

        try:
            result = await original_web_browsing(self, child_id)
        except Exception as err:
            self.web_browsing_source = "web"
            self.web_browsing_status = "web_error"
            _LOGGER.debug("Family web WebRestrictions fallback failed: %r", err)
            return None
        self.web_browsing_source = "web"
        if isinstance(result, dict):
            self.web_browsing_status = "ok"
            return result
        self.web_browsing_status = str(
            self.last_web_error_code or "web_no_data"
        ).lower()
        return None

    async def _patched_get_content_settings(self, child_id: str):
        self.content_settings_source = "mobile"
        self.content_settings_status = "checking"
        try:
            result = await original_content_settings(self, child_id)
        except Exception:
            self.content_settings_status = "error"
            raise
        self.content_settings_status = "ok" if isinstance(result, dict) else "no_data"
        return result

    async def _patched_get_screentime_policy(
        self, child_id: str, platform: str = "Windows",
        *, require_child_match: bool = False,
    ):
        """Read the active Windows/per-device policy exactly as seen in the HAR."""
        self.screentime_policy_status = "checking"
        self.screentime_policy_source = None

        now = time.monotonic()
        backoff_until = float(getattr(self, "_family_web_backoff_until", 0.0) or 0.0)
        if backoff_until > now:
            self.screentime_policy_status = "web_api_backoff"
            self.web_api_state = "backoff"
            _LOGGER.debug(
                "Skipping Family web screen-time policy request during backoff "
                "(remaining=%ds)",
                int(backoff_until - now),
            )
            return None

        st_url = f"{self.WEB_API_BASE}/family/api/st"
        result = await self._web_request(
            "GET", st_url, params={"childId": str(child_id)}
        )
        policy = self._normalize_screentime_policy(
            result, child_id, require_child_match=require_child_match
        )
        if policy is not None:
            self.screentime_policy_status = "ok"
            self.screentime_policy_source = "st_har"
            self._family_web_backoff_until = 0.0
            _LOGGER.info(
                "Loaded screen-time weekday policy from HAR-confirmed Family /st endpoint "
                "for child=%s",
                child_id,
            )
            return policy

        st_error = self.last_web_error_code
        if st_error == "LOGIN_REDIRECT":
            self.screentime_policy_status = "session_expired"
            return None
        if st_error in {"TIMEOUT", "NETWORK_ERROR"}:
            self.screentime_policy_status = (
                "web_api_timeout" if st_error == "TIMEOUT" else "web_api_network_error"
            )
            self._family_web_backoff_until = time.monotonic() + _WEB_BACKOFF_SECONDS
            _LOGGER.debug(
                "Backing off Family web policy requests for %d seconds after %s on /st",
                _WEB_BACKOFF_SECONDS,
                st_error,
            )
            return None

        policy = await original_screentime_policy(
            self, child_id, platform, require_child_match=require_child_match
        )
        if policy is not None:
            self._family_web_backoff_until = 0.0
            return policy

        if self.last_web_error_code in {"TIMEOUT", "NETWORK_ERROR"}:
            self._family_web_backoff_until = time.monotonic() + _WEB_BACKOFF_SECONDS
            _LOGGER.debug(
                "Backing off Family web policy requests for %d seconds after %s",
                _WEB_BACKOFF_SECONDS,
                self.last_web_error_code,
            )
        return None

    setattr(_patched_get_web_browsing_settings, _DATA_SOURCE_PATCH_MARKER, True)
    FamilySafetyWebAPI.get_web_browsing_settings = _patched_get_web_browsing_settings
    FamilySafetyWebAPI.get_content_settings = _patched_get_content_settings
    FamilySafetyWebAPI.get_screentime_policy = _patched_get_screentime_policy
    return True


def _patch_connection_state_diagnostics() -> bool:
    """Expose per-dataset sources and do not recommend reauth for timeouts."""
    try:
        from .coordinator import FamilySafetyDataUpdateCoordinator
    except (ImportError, AttributeError):
        return False

    original = FamilySafetyDataUpdateCoordinator.connection_state
    if getattr(original, _CONNECTION_STATE_PATCH_MARKER, False):
        return False

    def _patched_connection_state(self) -> dict[str, Any]:
        state = original(self)
        web_error = state.get("web_last_error_code")
        if web_error in {"TIMEOUT", "NETWORK_ERROR"} and not state.get(
            "reauth_required"
        ):
            state["reauth_recommended"] = False

        api = getattr(self, "web_api", None)
        state.update(
            {
                "web_browsing_source": getattr(api, "web_browsing_source", None),
                "web_browsing_status": getattr(api, "web_browsing_status", "unknown"),
                "content_settings_source": getattr(api, "content_settings_source", None),
                "content_settings_status": getattr(api, "content_settings_status", "unknown"),
                "screentime_policy_source": getattr(
                    api, "screentime_policy_source", None
                ),
                "screentime_policy_endpoint": "/family/api/st",
                "web_transport": getattr(api, "_hafs_web_transport", "default"),
                "web_probe_backoff_active": bool(
                    api
                    and float(getattr(api, "_web_probe_backoff_until", 0.0) or 0.0)
                    > time.monotonic()
                ),
                "family_web_backoff_active": bool(
                    api
                    and float(getattr(api, "_family_web_backoff_until", 0.0) or 0.0)
                    > time.monotonic()
                ),
            }
        )
        return state

    setattr(_patched_connection_state, _CONNECTION_STATE_PATCH_MARKER, True)
    FamilySafetyDataUpdateCoordinator.connection_state = _patched_connection_state
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

    if _patch_web_transport():
        applied.append("IPv4 threaded Family web transport")

    if _patch_web_probe_backoff():
        applied.append("web transport backoff")

    if _patch_combined_client_data_sources():
        applied.append("mobile-first + HAR screen-time data sources")

    if _patch_connection_state_diagnostics():
        applied.append("connection source diagnostics")

    if applied:
        _LOGGER.debug(
            "Applied pyfamilysafety compatibility patches (%s)",
            "; ".join(applied),
        )


def authenticator_access_token_expired(authenticator: Authenticator) -> bool:
    """Return token-expiry state across pyfamilysafety API versions."""
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
    now = (
        datetime.now(tz=expires.tzinfo)
        if getattr(expires, "tzinfo", None)
        else datetime.now()
    )
    return expires <= now + timedelta(seconds=60)


async def create_authenticator(
    hass: HomeAssistant,
    token: str,
    *,
    use_refresh_token: bool,
) -> Authenticator:
    """Create an Authenticator across pyfamilysafety API versions."""
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
