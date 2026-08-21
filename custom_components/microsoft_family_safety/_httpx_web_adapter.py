"""httpx-backed adapter for Microsoft Family private web traffic.

The native authentication proxy already reaches account.microsoft.com through
Home Assistant's httpx transport.  This adapter presents the small aiohttp-like
surface expected by ``FamilySafetyWebAPI`` so runtime private-web requests use
that proven transport without changing the mobile pyfamilysafety client.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp
import httpx
from yarl import URL

from homeassistant.core import HomeAssistant
from homeassistant.helpers.httpx_client import create_async_httpx_client

from .api_client import FamilySafetyWebAPI

_LOGGER = logging.getLogger(__name__)
# Same marker used by _pyfamilysafety_compat so its aiohttp IPv4 patch skips us.
_TRANSPORT_MARKER = "_hafs_ipv4_threaded_web_transport_patch"
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0"
)


def _timeout_phase(err: httpx.TimeoutException) -> str:
    if isinstance(err, httpx.ConnectTimeout):
        return "connect"
    if isinstance(err, httpx.ReadTimeout):
        return "read"
    if isinstance(err, httpx.WriteTimeout):
        return "write"
    if isinstance(err, httpx.PoolTimeout):
        return "pool"
    return "unknown"


class _CookieView:
    """Expose http.cookiejar.Cookie with the morsel fields api_client expects."""

    def __init__(self, cookie: Any) -> None:
        self._cookie = cookie
        self.key = cookie.name
        self.value = cookie.value

    def __getitem__(self, key: str) -> Any:
        if key == "domain":
            return self._cookie.domain or ""
        if key == "path":
            return self._cookie.path or "/"
        if key == "secure":
            return bool(self._cookie.secure)
        if key == "httponly":
            return bool(self._cookie.has_nonstandard_attr("HttpOnly"))
        if key == "expires":
            return self._cookie.expires or ""
        return ""


class _CookieJarAdapter:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    def __iter__(self):
        for cookie in self._client.cookies.jar:
            yield _CookieView(cookie)

    def filter_cookies(self, url: URL) -> dict[str, str]:
        host = (url.host or "").lower()
        path = url.path or "/"
        result: dict[str, str] = {}
        for cookie in self._client.cookies.jar:
            domain = str(cookie.domain or "").lstrip(".").lower()
            cookie_path = str(cookie.path or "/")
            if not domain or not (host == domain or host.endswith(f".{domain}")):
                continue
            if cookie.secure and url.scheme != "https":
                continue
            if not path.startswith(cookie_path):
                continue
            result[cookie.name] = cookie.value
        return result


class _ResponseAdapter:
    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self.status = response.status_code
        self.url = response.url
        self.headers = response.headers

    async def text(self) -> str:
        return self._response.text

    async def json(self, *, content_type: Any = None) -> Any:
        del content_type
        return self._response.json()

    async def read(self) -> bytes:
        return self._response.content


class _RequestContext:
    def __init__(
        self,
        owner: FamilySafetyWebAPI,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        kwargs: dict[str, Any],
    ) -> None:
        self._owner = owner
        self._client = client
        self._method = method
        self._url = url
        self._kwargs = kwargs
        self._response: httpx.Response | None = None

    async def __aenter__(self) -> _ResponseAdapter:
        started = time.monotonic()
        kwargs = dict(self._kwargs)
        if "allow_redirects" in kwargs:
            kwargs["follow_redirects"] = bool(kwargs.pop("allow_redirects"))
        try:
            self._response = await self._client.request(
                self._method, self._url, **kwargs
            )
        except httpx.TimeoutException as err:
            phase = _timeout_phase(err)
            self._owner._hafs_web_timeout_phase = phase
            _LOGGER.warning(
                "Microsoft Family httpx transport timed out during %s after %.1fs: %s %s",
                phase,
                time.monotonic() - started,
                self._method.upper(),
                URL(self._url).path,
            )
            raise asyncio.TimeoutError from err
        except httpx.RequestError as err:
            self._owner._hafs_web_transport_error = type(err).__name__
            _LOGGER.warning(
                "Microsoft Family httpx transport failed after %.1fs: %s %s error=%s",
                time.monotonic() - started,
                self._method.upper(),
                URL(self._url).path,
                type(err).__name__,
            )
            raise aiohttp.ClientError(str(err)) from err

        self._owner._hafs_web_timeout_phase = None
        self._owner._hafs_web_transport_error = None
        _LOGGER.debug(
            "Microsoft Family httpx transport response in %.2fs: %s %s status=%s",
            time.monotonic() - started,
            self._method.upper(),
            URL(self._url).path,
            self._response.status_code,
        )
        return _ResponseAdapter(self._response)

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._response is not None:
            await self._response.aclose()


class _HttpxSessionAdapter:
    def __init__(self, owner: FamilySafetyWebAPI, client: httpx.AsyncClient) -> None:
        self._owner = owner
        self._client = client
        self.cookie_jar = _CookieJarAdapter(client)

    @property
    def closed(self) -> bool:
        return self._client.is_closed

    async def close(self) -> None:
        await httpx.AsyncClient.aclose(self._client)

    def request(self, method: str, url: str, **kwargs: Any) -> _RequestContext:
        return _RequestContext(self._owner, self._client, method, url, kwargs)

    def get(self, url: str, **kwargs: Any) -> _RequestContext:
        return self.request("GET", url, **kwargs)


def apply_httpx_web_transport_patch(hass: HomeAssistant) -> None:
    """Use HA's httpx transport for FamilySafetyWebAPI's private web session."""
    current = FamilySafetyWebAPI._get_web_session
    if getattr(current, _TRANSPORT_MARKER, False):
        return

    def _patched_get_web_session(self) -> _HttpxSessionAdapter:
        session = getattr(self, "_web_session", None)
        if session is None or session.closed:
            client = create_async_httpx_client(
                hass,
                auto_cleanup=False,
                follow_redirects=False,
                timeout=httpx.Timeout(
                    connect=10.0,
                    read=20.0,
                    write=10.0,
                    pool=10.0,
                ),
                headers={"User-Agent": _BROWSER_USER_AGENT},
            )
            for cookie in self._web_cookies or []:
                name = cookie.get("name")
                value = cookie.get("value")
                domain = cookie.get("domain")
                if not name or value is None or not domain:
                    continue
                try:
                    client.cookies.set(
                        str(name),
                        str(value),
                        domain=str(domain),
                        path=str(cookie.get("path") or "/"),
                    )
                except Exception as err:
                    _LOGGER.debug(
                        "Could not preload Microsoft Family cookie %s: %s", name, err
                    )
            session = _HttpxSessionAdapter(self, client)
            self._web_session = session
            self._hafs_web_transport = "httpx_ha_client"
            self._hafs_web_timeout_phase = None
            self._hafs_web_transport_error = None
            _LOGGER.debug(
                "Created Microsoft Family web session using Home Assistant httpx transport"
            )
        return session

    setattr(_patched_get_web_session, _TRANSPORT_MARKER, True)
    FamilySafetyWebAPI._get_web_session = _patched_get_web_session
    _LOGGER.debug("Applied Home Assistant httpx Family web transport patch")
