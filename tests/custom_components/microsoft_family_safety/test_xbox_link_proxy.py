"""The Xbox Family Settings link captures the OAuth code through the sign-in proxy.

The options flow opens the Microsoft sign-in through the integration's
temporary proxy, which captures the oauth20_desktop.srf code itself, so the
user copies nothing. Without a usable Home Assistant URL, or when the sign-in
does not finish, the manual paste step takes over.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp.test_utils import make_mocked_request
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.data_entry_flow import FlowResultType, UnknownFlow

from custom_components.microsoft_family_safety.auth.native_proxy import (
    _CALLBACK_TOKENS,
    MicrosoftFamilyAuthorizationCallbackView,
    _is_native_options_flow,
)
from custom_components.microsoft_family_safety.config_flow import (
    FamilySafetyConfigFlow,
    FamilySafetyOptionsFlow,
    NativeAuthHttpsRequired,
    NativeAuthLocalHttpRequired,
    _options_browser_hass_url,
)
from custom_components.microsoft_family_safety.const import DOMAIN
from custom_components.microsoft_family_safety.xbox_family_api import XboxFamilyError

CODE_URL = "https://login.live.com/oauth20_desktop.srf?code=abc123"
ERROR_URL = "https://login.live.com/oauth20_desktop.srf?error=access_denied"
CONFIG_FLOW = "custom_components.microsoft_family_safety.config_flow"
NATIVE_PROXY = "custom_components.microsoft_family_safety.auth.native_proxy"
XBOX_API = "custom_components.microsoft_family_safety.xbox_family_api.XboxFamilyAPI"


def _flow(hass) -> FamilySafetyOptionsFlow:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"refresh_token": "x"},
        options={"platforms": ["Windows", "Xbox"], "xbox_features": True},
    )
    entry.add_to_hass(hass)
    flow = FamilySafetyOptionsFlow(entry)
    flow.hass = hass
    flow.handler = entry.entry_id
    flow.flow_id = "options-flow"
    flow._pending_options = {"xbox_features": True}
    return flow


def _proxy(redirect: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        oauth_redirect_url=redirect,
        complete=bool(redirect and "code=" in redirect),
        token="t",
        access_path="/p",
    )


# --- which Home Assistant URL the browser gets ---------------------------------
def test_https_is_used_and_plain_http_only_when_allowed_and_local(hass) -> None:
    with patch(f"{CONFIG_FLOW}.get_url", return_value="https://ha.example"):
        assert _options_browser_hass_url(hass, False) == "https://ha.example"
    with patch(f"{CONFIG_FLOW}.get_url", return_value="http://192.0.2.10:8123"):
        assert _options_browser_hass_url(hass, False) is None
    with patch(f"{CONFIG_FLOW}.get_url", return_value="http://homeassistant.local:8123"):
        assert _options_browser_hass_url(hass, True) == "http://homeassistant.local:8123"
    with patch(f"{CONFIG_FLOW}.get_url", return_value="http://ha.example.com"):
        assert _options_browser_hass_url(hass, True) is None


@pytest.mark.parametrize(
    ("url", "allow_http", "expected"),
    [
        ("https://ha.example", False, "https://ha.example"),
        ("http://192.0.2.10:8123", False, NativeAuthHttpsRequired),
        ("http://homeassistant.local:8123", True, "http://homeassistant.local:8123"),
        ("http://ha.example.com", True, NativeAuthLocalHttpRequired),
    ],
)
def test_the_main_sign_in_and_the_xbox_link_share_the_rule(
    hass, url, allow_http, expected
) -> None:
    flow = FamilySafetyConfigFlow()
    flow.hass = hass
    flow._allow_insecure_http_auth = allow_http
    with patch(f"{CONFIG_FLOW}.get_url", return_value=url):
        if isinstance(expected, str):
            assert flow._browser_hass_url() == expected
            assert _options_browser_hass_url(hass, allow_http) == expected
        else:
            with pytest.raises(expected):
                flow._browser_hass_url()
            assert _options_browser_hass_url(hass, allow_http) is None


async def test_no_usable_url_goes_to_the_manual_paste_step(hass) -> None:
    flow = _flow(hass)
    with patch(f"{CONFIG_FLOW}._options_browser_hass_url", return_value=None):
        result = await flow.async_step_xbox_link()
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "xbox_link_manual"


# --- the proxied sign-in ----------------------------------------------------------
async def test_the_link_opens_the_proxy_and_shows_progress(hass) -> None:
    flow = _flow(hass)
    with patch(f"{CONFIG_FLOW}._options_browser_hass_url", return_value="https://ha.example"), patch(
        f"{NATIVE_PROXY}.register_native_proxy"
    ) as register, patch(f"{NATIVE_PROXY}.create_async_httpx_client", return_value=MagicMock()):
        result = await flow.async_step_xbox_link()
        proxy = register.call_args.args[1]
        assert result["type"] == FlowResultType.SHOW_PROGRESS
        assert result["step_id"] == "xbox_link"
        assert result["description_placeholders"]["url"] == f"https://ha.example{proxy.access_path}"
        assert proxy.flow_id == "options-flow"
        assert flow._xbox_verifier
        flow._xbox_link_task.cancel()


async def test_the_wait_ends_on_a_code_or_an_error(hass) -> None:
    for redirect in (CODE_URL, ERROR_URL):
        flow = _flow(hass)
        flow._xbox_proxy = _proxy(redirect)
        await flow._async_wait_xbox_redirect()  # returns at once


async def test_the_wait_gives_up_after_the_timeout(hass) -> None:
    flow = _flow(hass)
    flow._xbox_proxy = _proxy(None)
    with patch(f"{CONFIG_FLOW}.XBOX_LINK_PROXY_TIMEOUT", -1):
        await flow._async_wait_xbox_redirect()


async def test_after_the_wait_the_flow_moves_to_the_finish_step(hass) -> None:
    flow = _flow(hass)
    flow._xbox_link_task = SimpleNamespace(done=lambda: True)  # the re-entry after the wait
    flow._xbox_proxy = _proxy(CODE_URL)
    result = await flow.async_step_xbox_link()
    assert result["type"] == FlowResultType.SHOW_PROGRESS_DONE
    assert result["step_id"] == "finish_xbox_proxy"


async def test_asking_again_during_the_sign_in_keeps_waiting(hass) -> None:
    """The frontend can ask again before the sign-in is done (the app coming back
    from the browser): that must not end the wait and fall back to the paste step."""
    flow = _flow(hass)
    with patch(f"{CONFIG_FLOW}._options_browser_hass_url", return_value="https://ha.example"), patch(
        f"{NATIVE_PROXY}.register_native_proxy"
    ), patch(f"{NATIVE_PROXY}.create_async_httpx_client", return_value=MagicMock()):
        first = await flow.async_step_xbox_link()
        task = flow._xbox_link_task
        again = await flow.async_step_xbox_link()
        assert again["type"] == FlowResultType.SHOW_PROGRESS
        assert again["step_id"] == "xbox_link"
        assert again["description_placeholders"] == first["description_placeholders"]
        assert flow._xbox_link_task is task and not task.done()
        task.cancel()


async def test_finish_links_from_the_captured_code(hass) -> None:
    flow = _flow(hass)
    flow._xbox_proxy = _proxy(CODE_URL)
    flow._xbox_verifier = "verifier"
    with patch(f"{NATIVE_PROXY}.unregister_native_proxy", new=AsyncMock()) as close, patch(
        f"{XBOX_API}.async_link_from_redirect", new=AsyncMock()
    ) as link:
        result = await flow.async_step_finish_xbox_proxy()
    link.assert_awaited_once_with(CODE_URL, "verifier", "proxy")
    close.assert_awaited_once()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert flow._xbox_proxy is None


async def test_finish_links_through_the_running_coordinator(hass) -> None:
    flow = _flow(hass)
    flow._xbox_proxy = _proxy(CODE_URL)
    flow._xbox_verifier = "verifier"
    coordinator = SimpleNamespace(
        xbox_family=SimpleNamespace(async_link_from_redirect=AsyncMock()),
        async_xbox_linked=MagicMock(),
    )
    hass.data.setdefault(DOMAIN, {})[flow._config_entry.entry_id] = coordinator
    with patch(f"{NATIVE_PROXY}.unregister_native_proxy", new=AsyncMock()):
        result = await flow.async_step_finish_xbox_proxy()
    coordinator.xbox_family.async_link_from_redirect.assert_awaited_once()
    coordinator.async_xbox_linked.assert_called_once()
    assert result["type"] == FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize("redirect", [None, ERROR_URL])
async def test_finish_without_a_code_offers_the_manual_step(hass, redirect) -> None:
    flow = _flow(hass)
    flow._xbox_proxy = _proxy(redirect)
    flow._xbox_verifier = "verifier"
    with patch(f"{NATIVE_PROXY}.unregister_native_proxy", new=AsyncMock()) as close, patch(
        f"{XBOX_API}.async_link_from_redirect", new=AsyncMock()
    ) as link:
        result = await flow.async_step_finish_xbox_proxy()
    link.assert_not_awaited()
    close.assert_awaited_once()
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "xbox_link_manual"


async def test_finish_offers_the_manual_step_when_microsoft_refuses_the_code(hass) -> None:
    flow = _flow(hass)
    flow._xbox_proxy = _proxy(CODE_URL)
    flow._xbox_verifier = "verifier"
    with patch(f"{NATIVE_PROXY}.unregister_native_proxy", new=AsyncMock()), patch(
        f"{XBOX_API}.async_link_from_redirect", new=AsyncMock(side_effect=XboxFamilyError("no"))
    ):
        result = await flow.async_step_finish_xbox_proxy()
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "xbox_link_manual"


async def test_closing_the_dialog_closes_the_proxy(hass) -> None:
    flow = _flow(hass)
    flow._xbox_proxy = _proxy(None)
    with patch(f"{NATIVE_PROXY}.unregister_native_proxy", new=AsyncMock()) as close:
        flow.async_remove()
        await hass.async_block_till_done()
    close.assert_awaited_once()
    assert flow._xbox_proxy is None


# --- the browser callback ---------------------------------------------------------
def test_options_flows_are_told_apart_from_config_flows() -> None:
    hass = MagicMock()
    hass.config_entries.flow.async_get.side_effect = UnknownFlow
    hass.config_entries.options.async_get.return_value = {"handler": "entry-1"}
    hass.config_entries.async_get_entry.return_value = SimpleNamespace(domain=DOMAIN)
    assert _is_native_options_flow(hass, "fid") is True

    hass.config_entries.async_get_entry.return_value = SimpleNamespace(domain="other")
    assert _is_native_options_flow(hass, "fid") is False

    hass.config_entries.options.async_get.side_effect = UnknownFlow
    assert _is_native_options_flow(hass, "fid") is False

    config_flow = MagicMock()
    config_flow.config_entries.flow.async_get.return_value = {"handler": DOMAIN}
    assert _is_native_options_flow(config_flow, "fid") is False


async def test_callback_for_the_options_flow_only_shows_a_close_page() -> None:
    hass = MagicMock()
    hass.data = {_CALLBACK_TOKENS: {"good-token": ("options-flow", time.monotonic() + 60)}}
    hass.config_entries.flow.async_get.side_effect = UnknownFlow
    hass.config_entries.flow.async_configure = AsyncMock()
    hass.config_entries.options.async_get.return_value = {"handler": "entry-1"}
    hass.config_entries.options.async_configure = AsyncMock()
    hass.config_entries.async_get_entry.return_value = SimpleNamespace(domain=DOMAIN)
    request = make_mocked_request(
        "GET",
        "/auth/microsoft_family_safety/callback?flow_id=options-flow&token=good-token",
        app={"hass": hass},
    )
    response = await MicrosoftFamilyAuthorizationCallbackView().get(request)
    assert response.status == 200
    assert "close this window" in response.text
    hass.config_entries.flow.async_configure.assert_not_awaited()
    hass.config_entries.options.async_configure.assert_not_awaited()
