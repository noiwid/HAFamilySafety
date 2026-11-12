"""Xbox features keep the Family web session warm and noticed (#52).

Since #64 the schedules use the mobile API, so the Xbox calls are the only
Family web requests left. These tests cover what that changed:

- the coordinator bootstraps the Family context itself when Xbox is on;
- an isolated 401 or login redirect drops the context (403/404 still only
  means "no Xbox"), and an isolated 2xx proves the session;
- the reauth prompt is kept while Xbox features are on;
- the +8 h / +16 h silent renewals do not prompt, the +24 h expiry does;
- an "allowUntil" Xbox override reads as not blocked.
"""

from __future__ import annotations

from datetime import timedelta
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from custom_components.microsoft_family_safety.api_client import FamilySafetyWebAPI
from custom_components.microsoft_family_safety.const import (
    CONF_WEB_COOKIES,
    CONF_XBOX_FEATURES,
    DOMAIN,
)
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)

CHILD = "1234567890123456"
XBOX_URL = "https://account.microsoft.com/family/api/xbox/screen-time-xbox"
LOGIN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?prompt=login"


class FakeResponse:
    def __init__(self, status: int, location: str | None = None, body: str = "{}") -> None:
        self.status = status
        self.headers = {"Location": location} if location else {}
        self.url = XBOX_URL
        self._body = body

    async def text(self) -> str:
        return self._body

    async def json(self, content_type=None):
        return json.loads(self._body)


class FakeRequest:
    def __init__(self, response: FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> FakeResponse:
        return self._response

    async def __aexit__(self, *exc) -> bool:
        return False


class FakeJar(list):
    def filter_cookies(self, url):
        return {}


class FakeSession:
    """Answers every Family request with the same response."""

    def __init__(self, response: FakeResponse) -> None:
        self._response = response
        self.cookie_jar = FakeJar()

    def request(self, *args, **kwargs) -> FakeRequest:
        return FakeRequest(self._response)


def _web_api(response: FakeResponse | None = None) -> FamilySafetyWebAPI:
    """A web API with a ready Family context whose requests get ``response``."""
    api = FamilySafetyWebAPI(authenticator=object())
    api._web_cookies = [{"name": "MSPAuth", "value": "x", "domain": ".live.com", "path": "/"}]
    api._web_csrf = "token"
    api.family_context_state = "ready"
    api.family_token_source = "auth_proxy"
    api._family_referer = "https://account.microsoft.com/family/home"
    session = FakeSession(response or FakeResponse(200))
    api._get_web_session = lambda: session
    api.sync_web_cookies_from_session = lambda: False
    return api


async def _xbox_request(api: FamilySafetyWebAPI):
    return await api._web_request("GET", XBOX_URL, isolated=True)


@pytest.fixture
def make_coordinator(hass):
    """Build a coordinator for a native entry, with or without Xbox features."""

    def make(*, xbox: bool = True) -> FamilySafetyDataUpdateCoordinator:
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={"refresh_token": "x", CONF_WEB_COOKIES: [{"name": "MSPAuth"}]},
            options={CONF_XBOX_FEATURES: xbox},
        )
        entry.add_to_hass(hass)
        coord = FamilySafetyDataUpdateCoordinator(hass, entry)
        coord._create_auth_notification = AsyncMock()
        coord._dismiss_auth_notification = AsyncMock()
        coord._request_reauth = MagicMock()
        return coord

    return make


# --- isolated Xbox requests --------------------------------------------------
async def test_isolated_401_drops_the_family_context():
    api = _web_api(FakeResponse(401, body='{"statusCode":401}'))
    assert await _xbox_request(api) is None
    assert api.family_context_state == "auth_required"
    assert api._web_csrf is None
    assert api.family_context_dropped is True
    assert api.last_isolated_error_code == "HTTP_401"
    assert api.web_session_state != "expired"


async def test_isolated_403_keeps_the_context():
    # A child without an Xbox answers 403; that says nothing about the session.
    api = _web_api(FakeResponse(403, body='{"statusCode":403}'))
    api.web_session_state = "unknown"
    await _xbox_request(api)
    assert api.family_context_state == "ready"
    assert api._web_csrf == "token"
    assert api.web_session_state == "unknown"


async def test_isolated_login_redirect_drops_the_context():
    api = _web_api(FakeResponse(302, location=LOGIN_URL))
    await _xbox_request(api)
    assert api.family_context_state == "auth_required"
    assert api._web_csrf is None


async def test_isolated_other_redirect_keeps_the_context():
    api = _web_api(FakeResponse(302, location="https://account.microsoft.com/family/home"))
    await _xbox_request(api)
    assert api.family_context_state == "ready"


async def test_isolated_success_proves_the_session():
    api = _web_api(FakeResponse(200, body='{"ok": true}'))
    api.web_session_state = "unknown"
    await _xbox_request(api)
    assert api.web_session_state == "authenticated"


# --- reauth escalation -------------------------------------------------------
@pytest.mark.parametrize(("xbox", "prompted"), [(True, True), (False, False)])
async def test_reauth_prompt_kept_only_while_xbox_is_on(make_coordinator, xbox, prompted):
    coord = make_coordinator(xbox=xbox)
    coord.web_api = SimpleNamespace(family_context_state="auth_required", mobile_schedule_ok=True)
    await coord._async_track_family_context()
    coord._request_reauth.assert_not_called()
    await coord._async_track_family_context()
    assert coord._request_reauth.called is prompted
    if prompted:
        coord._request_reauth.assert_called_with("family_context_auth_required")


# --- bootstrap at the start of a poll ----------------------------------------
class StopPoll(Exception):
    """Cuts the poll short right after the warm-up."""


async def _poll_warms(coord, state, *, backoff=0.0, cookies=True) -> bool:
    warm = AsyncMock()
    coord.web_api = SimpleNamespace(
        family_context_state=state,
        has_web_cookies=cookies,
        _family_web_backoff_until=backoff,
        _warm_family_context=warm,
        async_check_web_session=AsyncMock(return_value=True),
        web_session_state="authenticated",
    )
    coord._async_load_web_cookies = AsyncMock()
    coord._async_persist_current_auth = AsyncMock()
    coord.api = SimpleNamespace(update=AsyncMock(side_effect=StopPoll))
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()
    return warm.called


async def test_poll_bootstraps_when_xbox_needs_the_context(make_coordinator):
    coord = make_coordinator()
    assert await _poll_warms(coord, "unknown")  # after a restart without a stored token
    assert await _poll_warms(coord, "auth_required")


async def test_poll_leaves_a_ready_or_unneeded_context_alone(make_coordinator):
    coord = make_coordinator()
    assert not await _poll_warms(coord, "ready")
    assert not await _poll_warms(coord, "unknown", backoff=time.monotonic() + 600)
    assert not await _poll_warms(coord, "unknown", cookies=False)
    assert not await _poll_warms(make_coordinator(xbox=False), "unknown")


# --- poll sequences ----------------------------------------------------------
async def _prompted_polls(coord, polls) -> list[int]:
    """Replay polls of (xbox_401, [bootstrap outcomes]); return the polls that prompted."""
    api = _web_api()
    outcomes: list[str] = []

    async def warm():
        api.family_context_state = outcomes.pop(0)
        if api.family_context_state == "ready":
            api._web_csrf = "token"
        return api._web_csrf

    api._warm_family_context = warm
    coord.web_api = api
    prompted: list[int] = []
    for number, (xbox_401, results) in enumerate(polls, 1):
        outcomes[:] = list(results)
        coord._request_reauth = lambda reason, n=number: prompted.append(n)
        if api.family_context_state != "ready":
            await api._warm_family_context()
        if xbox_401 and api.family_context_state == "ready":
            api._drop_family_context()
        await coord._async_rewarm_dropped_family_context()
        await coord._async_track_family_context()
    return prompted


async def test_silent_renewal_dips_do_not_prompt(make_coordinator):
    # +8 h / +16 h: the bootstrap ends on the silent sign-in page once, then recovers.
    polls = [(True, ["auth_required"]), (False, ["ready"]), (False, [])]
    assert await _prompted_polls(make_coordinator(), polls) == []


async def test_24h_expiry_prompts_on_the_second_failed_poll(make_coordinator):
    polls = [(True, ["auth_required"]), (False, ["auth_required"]), (False, ["auth_required"])]
    assert (await _prompted_polls(make_coordinator(), polls))[:1] == [2]


async def test_healthy_session_never_prompts(make_coordinator):
    polls = [(False, []), (False, []), (False, [])]
    assert await _prompted_polls(make_coordinator(), polls) == []


# --- Xbox lock state ---------------------------------------------------------
def _lock_state(coord, override, *, known_block=False) -> tuple[bool, bool]:
    coord._platform_override_until = {}
    coord._xbox_lock_known = set()
    if known_block:
        coord._platform_override_until[(CHILD, "xbox")] = dt_util.utcnow() + timedelta(hours=5)
        coord._xbox_lock_known.add(CHILD)
    data = {"dailyRestrictions": {}}
    if override is not None:
        data["screenTimeOverride"] = override
    account = {"blocked_platforms": []}
    coord._apply_xbox_lock_state(CHILD, account, {"data": data})
    return CHILD in coord._xbox_lock_known, "Xbox" in account["blocked_platforms"]


async def test_allow_until_reads_as_not_blocked(make_coordinator):
    coord = make_coordinator()
    allow = {"screenTimeOverrideType": "AllowUntil", "validUntil": "2026-10-28T20:00:00Z"}
    assert _lock_state(coord, allow) == (True, False)
    assert _lock_state(coord, allow, known_block=True) == (True, False)


async def test_block_until_and_unknown_types_unchanged(make_coordinator):
    coord = make_coordinator()
    until = (dt_util.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    block = {"screenTimeOverrideType": "BlockUntil", "validUntil": until}
    assert _lock_state(coord, block) == (True, True)
    assert _lock_state(coord, {"screenTimeOverrideType": "SomethingNew"}) == (False, False)


# --- review follow-ups ----------------------------------------------------------
async def test_rejection_is_persisted_when_the_rewarm_times_out(make_coordinator):
    # An Xbox 401 drops the token; the same-poll re-warm then times out, so the
    # state is no longer "auth_required" when the poll persists credentials.
    coord = make_coordinator()
    coord._auth_store = MagicMock(async_save=AsyncMock())
    coord._runtime_auth_state = {"web_family_token": "rejected-token"}
    api = _web_api(FakeResponse(401, body='{"statusCode":401}'))
    await _xbox_request(api)

    async def timeout():
        api.family_context_state = "timeout"

    api._warm_family_context = timeout
    coord.web_api = api
    await coord._async_rewarm_dropped_family_context()
    await coord._async_persist_runtime_auth(family_token=None)
    assert "web_family_token" not in coord._runtime_auth_state
    assert coord._runtime_auth_state["web_family_token_rejected"] is True
    # A token captured later is a fresh one and clears the flag.
    await coord._async_persist_runtime_auth(family_token="fresh-token")
    assert api.family_token_rejected is False
    assert "web_family_token_rejected" not in coord._runtime_auth_state


async def test_schedule_read_keeps_a_pending_family_reauth_notification(make_coordinator):
    coord = make_coordinator()
    coord.web_api = AsyncMock()
    coord._fetch_screentime_policy = AsyncMock(return_value={"dailyRestrictions": {}})
    coord._reauth_reason = "family_context_auth_required"
    await coord._fetch_web_api_data("1")
    coord._dismiss_auth_notification.assert_not_called()
    coord._reauth_reason = None
    await coord._fetch_web_api_data("1")
    coord._dismiss_auth_notification.assert_awaited()


@pytest.mark.parametrize("outcome", ["timeout", "error", "unavailable"])
async def test_failed_bootstrap_backs_off_both_warm_ups(make_coordinator, outcome):
    coord = make_coordinator()
    calls = []

    async def warm():
        calls.append(1)
        coord.web_api.family_context_state = outcome

    coord.web_api = SimpleNamespace(
        family_context_state="unknown",
        has_web_cookies=True,
        _family_web_backoff_until=0.0,
        _warm_family_context=warm,
        family_context_dropped=True,
    )
    await coord._async_warm_family_context_for_xbox()
    assert coord.web_api._family_web_backoff_until > time.monotonic() + 60
    # The same-poll re-warm after an Xbox 401 honours it too.
    await coord._async_rewarm_dropped_family_context()
    assert len(calls) == 1


async def test_ready_bootstrap_sets_no_backoff(make_coordinator):
    coord = make_coordinator()

    async def warm():
        coord.web_api.family_context_state = "ready"

    coord.web_api = SimpleNamespace(
        family_context_state="unknown", _family_web_backoff_until=0.0, _warm_family_context=warm
    )
    await coord._async_warm_family_context_for_xbox()
    assert coord.web_api._family_web_backoff_until == 0.0


# --- only when Xbox depends on the web session (after #69) -----------------------
def _link_xbox(coord, usable: bool = True) -> None:
    coord.xbox_family = MagicMock(linked=usable, usable=usable)


@pytest.mark.parametrize(
    ("xbox", "linked", "mobile_ok", "needed"),
    [
        (True, False, True, True),  # Xbox on, no Xbox Family Settings link
        (True, True, True, False),  # Xbox served by its own sign-in
        (False, False, True, False),  # mobile-only
        (True, True, False, True),  # mobile schedule API not answered yet
    ],
)
async def test_web_session_needed(make_coordinator, xbox, linked, mobile_ok, needed):
    coord = make_coordinator(xbox=xbox)
    _link_xbox(coord, linked)
    coord.web_api = SimpleNamespace(mobile_schedule_ok=mobile_ok)
    assert coord._web_session_needed is needed


async def test_linked_xbox_needs_no_prompt_nor_warm_up(make_coordinator):
    coord = make_coordinator()
    _link_xbox(coord)
    coord.web_api = SimpleNamespace(family_context_state="auth_required", mobile_schedule_ok=True)
    await coord._async_track_family_context()
    await coord._async_track_family_context()
    coord._request_reauth.assert_not_called()
    assert not await _poll_warms(coord, "auth_required")


async def test_linked_xbox_skips_the_same_poll_rewarm(make_coordinator):
    coord = make_coordinator()
    _link_xbox(coord)
    warm = AsyncMock()
    coord.web_api = SimpleNamespace(family_context_dropped=True, _warm_family_context=warm)
    await coord._async_rewarm_dropped_family_context()
    warm.assert_not_called()
    assert coord.web_api.family_context_dropped is False


@pytest.mark.parametrize(("linked", "prompted"), [(True, False), (False, True)])
async def test_expired_account_session_prompts_only_when_needed(make_coordinator, linked, prompted):
    coord = make_coordinator()
    _link_xbox(coord, linked)
    coord.web_api = SimpleNamespace(
        family_context_state="ready",
        has_web_cookies=True,
        _family_web_backoff_until=0.0,
        _warm_family_context=AsyncMock(),
        async_check_web_session=AsyncMock(return_value=False),
        web_session_state="expired",
        mobile_schedule_ok=True,
    )
    coord._async_load_web_cookies = AsyncMock()
    coord._async_persist_current_auth = AsyncMock()
    coord.api = SimpleNamespace(update=AsyncMock(side_effect=StopPoll))
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()
    assert coord._request_reauth.called is prompted


async def test_unneeded_reauth_is_withdrawn(make_coordinator, hass):
    coord = make_coordinator()
    _link_xbox(coord)
    coord.web_api = SimpleNamespace(mobile_schedule_ok=True)
    coord._reauth_requested = True
    coord._reauth_reason = "web_session_expired"
    progress = MagicMock(return_value=[
        {"flow_id": "untouched", "step_id": "reauth_full_confirm"},
        {"flow_id": "signing-in", "step_id": "check_mobile_proxy"},
    ])
    abort = MagicMock()
    hass.config_entries.flow.async_progress_by_handler = progress
    hass.config_entries.flow.async_abort = abort
    await coord._async_withdraw_unneeded_reauth()
    assert (coord._reauth_requested, coord._reauth_reason) == (False, None)
    coord._dismiss_auth_notification.assert_awaited_once()
    abort.assert_called_once_with("untouched")  # a sign-in under way is left alone


@pytest.mark.parametrize(
    ("reason", "linked", "withdrawn"),
    [("manual", True, False), ("family_context_auth_required", False, False)],
)
async def test_reauth_kept_when_asked_for_or_still_needed(make_coordinator, reason, linked, withdrawn):
    coord = make_coordinator()
    _link_xbox(coord, linked)
    coord.web_api = SimpleNamespace(mobile_schedule_ok=True)
    coord._reauth_requested = True
    coord._reauth_reason = reason
    await coord._async_withdraw_unneeded_reauth()
    assert (coord._reauth_requested is False) is withdrawn


async def test_manual_reauth_survives_a_later_automatic_reason(make_coordinator, hass):
    # The user asks for a sign-in, a poll then raises an automatic reason while
    # the web session is still needed, and the Xbox link becomes usable later.
    coord = make_coordinator()
    coord._request_reauth = FamilySafetyDataUpdateCoordinator._request_reauth.__get__(coord)
    coord.entry.async_start_reauth = MagicMock()
    coord._request_reauth("manual")
    coord._request_reauth("family_context_auth_required")
    assert coord._reauth_reason == "manual"
    _link_xbox(coord)
    coord.web_api = SimpleNamespace(mobile_schedule_ok=True)
    abort = MagicMock()
    hass.config_entries.flow.async_progress_by_handler = MagicMock(
        return_value=[{"flow_id": "f1", "step_id": "reauth_full_confirm"}]
    )
    hass.config_entries.flow.async_abort = abort
    await coord._async_withdraw_unneeded_reauth()
    assert coord._reauth_requested is True
    abort.assert_not_called()
