"""HAR-aligned tuning for the Microsoft Family private-web httpx adapter."""
from __future__ import annotations

import logging
from typing import Any

import httpx
from yarl import URL

from .api_client import FamilySafetyWebAPI
from ._httpx_web_adapter import _HttpxSessionAdapter

_LOGGER = logging.getLogger(__name__)
_REQUEST_TUNING_MARKER = "_hafs_har_request_tuning"
_SESSION_PROBE_MARKER = "_hafs_family_context_probe_defer"

# Match the already-working native auth proxy rather than the shorter runtime
# diagnostic timeout. Microsoft can take considerably longer than the HAR on
# the Home Assistant server path even after TCP/TLS is established.
_RUNTIME_TIMEOUT = httpx.Timeout(
    connect=30.0,
    read=120.0,
    write=30.0,
    pool=30.0,
)

# Client hints present on the successful browser HAR Family XHR. These are not
# credentials; they simply make the server-side request resemble the browser
# request that Microsoft actually accepted.
_BROWSER_XHR_HEADERS = {
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "sec-ch-ua": '"Not=A?Brand";v="99", "Microsoft Edge";v="151", "Chromium";v="151"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "X-Edge-Shopping-Flag": "1",
}


def _tune_request_kwargs(url: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    tuned = dict(kwargs)
    tuned["timeout"] = _RUNTIME_TIMEOUT

    try:
        target = URL(url)
    except ValueError:
        return tuned

    if (target.host or "").lower() != "account.microsoft.com":
        return tuned

    headers = dict(tuned.get("headers") or {})
    headers.update({key: value for key, value in _BROWSER_XHR_HEADERS.items() if key not in headers})

    # The HAR carries this telemetry context on the authoritative Windows
    # screen-time read. Do not fabricate MS-CV because it is a per-request
    # correlation vector rather than an authentication requirement.
    if target.path == "/family/api/st":
        headers.setdefault(
            "Correlation-Context",
            "v=1,ms.b.tel.market=de-DE,ms.b.qos.rootOperationName=Family.React.GetScreenTimeV2",
        )

    tuned["headers"] = headers
    return tuned


def apply_httpx_web_tuning_patch() -> None:
    """Align runtime Family web requests with the successful browser HAR."""
    current_request = _HttpxSessionAdapter.request
    if not getattr(current_request, _REQUEST_TUNING_MARKER, False):
        original_request = current_request

        def _patched_request(self, method: str, url: str, **kwargs: Any):
            return original_request(
                self,
                method,
                url,
                **_tune_request_kwargs(url, kwargs),
            )

        setattr(_patched_request, _REQUEST_TUNING_MARKER, True)
        _HttpxSessionAdapter.request = _patched_request
        _LOGGER.debug(
            "Applied HAR browser headers and native-auth timeouts to Family httpx requests"
        )

    current_probe = FamilySafetyWebAPI.async_check_web_session
    if not getattr(current_probe, _SESSION_PROBE_MARKER, False):
        original_probe = current_probe

        async def _patched_probe(self) -> bool:
            family_token = self._web_csrf or self._web_canary
            if (
                self.has_web_cookies
                and self.family_context_state == "ready"
                and family_token
            ):
                # The family context came from the authenticated native browser
                # flow. Avoid an extra /account request on every coordinator
                # cycle; the actual private Family request below is a stronger
                # health check and marks web_session authenticated on HTTP 200.
                if self.web_session_state == "error":
                    self.web_session_state = "unknown"
                    self.web_session_last_checked = None
                    self.web_session_last_http_status = None
                if self.last_web_error_code in {"TIMEOUT", "NETWORK_ERROR"}:
                    self.last_web_error_code = None
                self._web_probe_backoff_until = 0.0
                self._web_probe_backoff_error = None
                _LOGGER.debug(
                    "Deferring Microsoft account /account probe because an authenticated "
                    "Family context is already available; Family API request will verify it"
                )
                return self.web_session_state == "authenticated"
            return await original_probe(self)

        setattr(_patched_probe, _SESSION_PROBE_MARKER, True)
        FamilySafetyWebAPI.async_check_web_session = _patched_probe
        _LOGGER.debug("Applied Family-context-aware web-session probe deferral")
