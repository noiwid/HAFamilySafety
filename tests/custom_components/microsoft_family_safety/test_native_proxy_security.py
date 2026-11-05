"""The sign-in proxy keeps Home Assistant's defences intact.

Review of the HACS default submission: the unauthenticated callback must only
touch this integration's own flow, core's security filter must not be patched,
and only the sign-in hosts may be served from the Home Assistant origin.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qsl, urlsplit

from aiohttp import web
from aiohttp.test_utils import make_mocked_request
import httpx
import pytest

from homeassistant.components.http import security_filter
from homeassistant.components.http.security_filter import setup_security_filter

from custom_components.microsoft_family_safety.auth import native_proxy
from custom_components.microsoft_family_safety.auth.native_proxy import (
    _CALLBACK_TOKENS,
    MicrosoftFamilyAuthorizationCallbackView,
    MicrosoftFamilyAuthProxy,
    _is_allowed_host,
    ensure_native_auth_views,
)
from custom_components.microsoft_family_safety.const import DOMAIN

ORIGIN = "https://ha.example"
FLOW = "flow-1"
SSO_REDIRECT = "https://login.live.com/oauth20_authorize.srf?client_id=1&epctrc=/w/V6cI&lc=1036"


@pytest.fixture
def proxy() -> MicrosoftFamilyAuthProxy:
    with patch.object(native_proxy, "create_async_httpx_client", return_value=MagicMock()):
        return MicrosoftFamilyAuthProxy(
            MagicMock(), callback_url=f"{ORIGIN}/auth/microsoft_family_safety/callback?flow_id={FLOW}"
        )


def _hass(handler: str = DOMAIN) -> MagicMock:
    hass = MagicMock()
    hass.data = {_CALLBACK_TOKENS: {"good-token": (FLOW, time.monotonic() + 60)}}
    hass.config_entries.flow.async_get.return_value = {"handler": handler, "step_id": "other"}
    hass.config_entries.flow.async_configure = AsyncMock(return_value={"type": "external_done"})
    return hass


async def _callback(hass: MagicMock, query: str) -> web.Response:
    request = make_mocked_request(
        "GET", f"/auth/microsoft_family_safety/callback?{query}", app={"hass": hass}
    )
    return await MicrosoftFamilyAuthorizationCallbackView().get(request)


@pytest.mark.parametrize(
    "query",
    [
        f"flow_id={FLOW}",
        f"flow_id={FLOW}&token=bad-token",
        "flow_id=other-flow&token=good-token",
    ],
)
async def test_callback_without_the_flow_token_never_touches_the_flow(query: str) -> None:
    hass = _hass()
    with pytest.raises(web.HTTPForbidden):
        await _callback(hass, query)
    hass.config_entries.flow.async_get.assert_not_called()
    hass.config_entries.flow.async_configure.assert_not_awaited()


async def test_callback_refuses_an_expired_token() -> None:
    hass = _hass()
    hass.data[_CALLBACK_TOKENS]["good-token"] = (FLOW, time.monotonic() - 1)
    with pytest.raises(web.HTTPForbidden):
        await _callback(hass, f"flow_id={FLOW}&token=good-token")


async def test_callback_refuses_a_flow_of_another_integration() -> None:
    hass = _hass(handler="other_integration")
    with pytest.raises(web.HTTPForbidden):
        await _callback(hass, f"flow_id={FLOW}&token=good-token")
    hass.config_entries.flow.async_configure.assert_not_awaited()


async def test_callback_resumes_its_own_flow() -> None:
    hass = _hass()
    response = await _callback(hass, f"flow_id={FLOW}&token=good-token")
    assert response.status == 200
    hass.config_entries.flow.async_configure.assert_awaited_once()


def test_callback_url_carries_the_proxy_token(proxy: MicrosoftFamilyAuthProxy) -> None:
    query = dict(parse_qsl(urlsplit(proxy.callback_url).query))
    assert query == {"flow_id": FLOW, "token": proxy.token}
    assert proxy.flow_id == FLOW


def test_core_security_filter_is_left_alone() -> None:
    before = security_filter.FILTERS
    hass = MagicMock()
    hass.data = {}
    ensure_native_auth_views(hass)
    assert security_filter.FILTERS is before


async def _through_core_filter(path: str) -> int:
    app = web.Application()
    setup_security_filter(app)

    async def handler(request: web.Request) -> web.Response:
        return web.Response()

    try:
        response = await app.middlewares[0](make_mocked_request("GET", path), handler)
    except web.HTTPBadRequest as err:
        return err.status
    return response.status


def _path_and_query(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.path}?{parts.query}" if parts.query else parts.path


async def test_sso_redirect_passes_the_real_filter_and_reaches_microsoft_intact(
    proxy: MicrosoftFamilyAuthProxy,
) -> None:
    raw = f"{proxy.proxy_path}/__ms_host__/login.live.com/oauth20_authorize.srf?epctrc=/w/V6cI"
    assert await _through_core_filter(raw) == 400

    proxied = proxy._proxyize_url(SSO_REDIRECT, SSO_REDIRECT, ORIGIN)
    assert await _through_core_filter(_path_and_query(proxied)) == 200

    tail = _path_and_query(proxied).split(f"{proxy.proxy_path}/", 1)[1]
    request = make_mocked_request("GET", _path_and_query(proxied), match_info={"tail": tail.split("?")[0]})
    target = proxy._target_from_request(request)
    assert target.host == "login.live.com"
    assert dict(target.query) == {"client_id": "1", "epctrc": "/w/V6cI", "lc": "1036"}
    # A proxied URL nested in a form field or a parameter is restored as well
    restored = urlsplit(proxy._deproxy_value(proxied, ORIGIN))
    assert restored.netloc == "login.live.com"
    assert dict(parse_qsl(restored.query)) == {"client_id": "1", "epctrc": "/w/V6cI", "lc": "1036"}


def test_a_harmless_query_stays_readable(proxy: MicrosoftFamilyAuthProxy) -> None:
    url = "https://login.live.com/login.srf?wa=wsignin1.0&lc=1036&ru=/fr-fr/family"
    assert proxy._proxyize_url(url, url, ORIGIN).endswith("?wa=wsignin1.0&lc=1036&ru=/fr-fr/family")


async def test_a_mangled_hidden_query_is_refused(proxy: MicrosoftFamilyAuthProxy) -> None:
    path = f"{proxy.proxy_path}/__ms_host__/login.live.com/x?__ms_q=%%%"
    request = make_mocked_request("GET", path, match_info={"tail": "__ms_host__/login.live.com/x"})
    with pytest.raises(web.HTTPBadRequest):
        proxy._target_from_request(request)


@pytest.mark.parametrize(
    "host",
    [
        "account.microsoft.com",
        "account.live.com",
        "login.live.com",
        "login.live.com:443",
        "LOGIN.MICROSOFTONLINE.COM",
        "login.microsoft.com",
    ],
)
def test_sign_in_hosts_are_proxied(host: str) -> None:
    assert _is_allowed_host(host)


@pytest.mark.parametrize(
    "host",
    [
        None,
        "microsoft.com",
        "www.microsoft.com",
        "fpt.live.com",
        "ipv6.login.live.com",
        "browser.events.data.microsoft.com",
        "answers.microsoft.com",
        "login.live.com.evil.example",
    ],
)
def test_other_hosts_are_not_served_from_the_home_assistant_origin(host: str | None) -> None:
    assert not _is_allowed_host(host)


def test_other_microsoft_urls_are_left_for_the_browser(proxy: MicrosoftFamilyAuthProxy) -> None:
    url = "https://fpt.live.com/?a=1"
    assert proxy._proxyize_url(url, url, ORIGIN) == url
    page = proxy._rewrite_html(
        f'<html><head></head><a href="{url}">x</a><a href="https://login.live.com/y">y</a></html>',
        "https://login.live.com/",
        ORIGIN,
    )
    assert f'href="{url}"' in page
    assert f'href="{ORIGIN}{proxy.proxy_path}/__ms_host__/login.live.com/y"' in page


async def _redirect(proxy: MicrosoftFamilyAuthProxy, location: str) -> web.StreamResponse:
    upstream = httpx.Request("GET", "https://account.microsoft.com/family/home")
    proxy._client.request = AsyncMock(
        return_value=httpx.Response(302, headers={"location": location}, request=upstream)
    )
    request = make_mocked_request(
        "GET",
        f"{proxy.proxy_path}/__ms_host__/account.microsoft.com/family/home",
        match_info={"tail": "__ms_host__/account.microsoft.com/family/home"},
        headers={"Host": "ha.example"},
    )
    try:
        return await proxy.handle(request)
    except web.HTTPFound as err:
        return err


async def test_redirect_to_another_microsoft_site_leaves_the_proxy(
    proxy: MicrosoftFamilyAuthProxy,
) -> None:
    response = await _redirect(proxy, "https://support.microsoft.com/help")
    assert response.headers["Location"] == "https://support.microsoft.com/help"


async def test_redirect_outside_microsoft_is_refused(proxy: MicrosoftFamilyAuthProxy) -> None:
    with pytest.raises(web.HTTPBadGateway):
        await _redirect(proxy, "https://evil.example/")


async def test_marketing_page_moves_on_to_the_next_family_route(
    proxy: MicrosoftFamilyAuthProxy,
) -> None:
    response = await _redirect(proxy, "https://www.microsoft.com/fr-fr/microsoft-365/family-safety")
    assert "/__ms_host__/account.microsoft.com/family/windows/home/direct" in response.headers["Location"]
    assert proxy._family_bootstrap_attempts == 1
