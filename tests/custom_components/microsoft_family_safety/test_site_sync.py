"""sync_site_list keeps the blocked websites in step with a published list."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser
import voluptuous as vol

from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError, Unauthorized

from custom_components.microsoft_family_safety import _register_services
from custom_components.microsoft_family_safety.const import DOMAIN
from custom_components.microsoft_family_safety.coordinator import (
    FamilySafetyDataUpdateCoordinator,
)
from custom_components.microsoft_family_safety.site_sync import (
    WRITE_CONCURRENCY,
    allowed_overlaps,
    parse_site_list,
)

CHILD = "1234567890123456"
URL = "https://lists.example/blocklist.txt"


class FakeWebAPI:
    """Microsoft's website exceptions, one PATCH per site as captured."""

    def __init__(self) -> None:
        self.sites: dict[str, bool] = {}  # host -> allowed
        self.writes: list[tuple[str, str]] = []
        self.fail: set[str] = set()
        self.read_fails = False
        self.in_flight = 0
        self.max_in_flight = 0

    async def get_web_browsing_settings(self, child_id):
        raise AssertionError("the site lists are read from the mobile API only")

    async def get_web_restrictions(self, child_id):
        if self.read_fails:
            raise RuntimeError("HTTP 503")
        return {
            "enabled": True,
            "exceptions": [
                {"website": f"https://{host}", "allowed": allowed}
                for host, allowed in self.sites.items()
            ],
        }

    async def _write(self, kind, site):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)
            if site in self.fail:
                raise RuntimeError("API request failed with status 500")
            self.writes.append((kind, site))
            if kind == "block":
                self.sites[site] = False
            else:
                self.sites.pop(site, None)
        finally:
            self.in_flight -= 1

    async def block_website(self, child_id, site):
        await self._write("block", site)

    async def remove_website(self, child_id, site):
        await self._write("remove", site)


@pytest.fixture
def rig(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"refresh_token": "x"})
    entry.add_to_hass(hass)
    coord = FamilySafetyDataUpdateCoordinator(hass, entry)
    coord.update_interval = None
    coord.web_api = FakeWebAPI()
    coord.async_request_refresh = AsyncMock()
    coord.data = {"accounts": {CHILD: {"first_name": "Alex"}}, "devices": {}}
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
    _register_services(hass)
    return coord


def _serve(aioclient_mock, text: str, status: int = 200) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(URL, text=text, status=status)


async def _sync(hass, user=None, **data):
    return await hass.services.async_call(
        DOMAIN, "sync_site_list", {"account_id": CHILD, "url": URL, **data},
        blocking=True, return_response=True,
        context=Context(user_id=user.id) if user else None,
    )


def test_list_parsing():
    ignored: list[str] = []
    text = "# comment\nexample.com\n0.0.0.0 ads.example.net # tracker\n\n*.bad.example\nco.uk\n10.0.0.1\nEXAMPLE.com\n"
    assert parse_site_list(text, ignored) == ["example.com", "ads.example.net"]
    assert ignored == ["*.bad.example", "co.uk", "10.0.0.1"]
    assert allowed_overlaps("www.example.org", "example.org")
    assert allowed_overlaps("example.org", "cdn.example.org")
    assert not allowed_overlaps("example.org", "example.com")


async def test_first_sync_adds_everything_a_few_at_a_time(hass, rig, aioclient_mock):
    domains = [f"site{i}.example" for i in range(450)]
    _serve(aioclient_mock, "\n".join(domains))

    result = await _sync(hass)

    assert result["added"]["count"] == 450
    assert len(result["added"]["examples"]) == 10
    assert result["removed"]["count"] == 0
    assert rig.web_api.max_in_flight <= WRITE_CONCURRENCY
    assert len(rig.web_api.writes) == 450


async def test_resync_removes_only_what_the_list_added(hass, rig, aioclient_mock):
    rig.web_api.sites = {"manual.example": False, "listed-manual.example": False}
    _serve(aioclient_mock, "a.example\nb.example\nlisted-manual.example\n")
    first = await _sync(hass)
    assert sorted(first["added"]["examples"]) == ["a.example", "b.example"]
    assert first["kept"]["examples"] == ["listed-manual.example"]

    # The list drops b.example and listed-manual.example.
    _serve(aioclient_mock, "a.example\n")
    rig.web_api.writes.clear()
    second = await _sync(hass)

    assert rig.web_api.writes == [("remove", "b.example")]
    assert second["removed"]["examples"] == ["b.example"]
    assert set(rig.web_api.sites) == {"manual.example", "listed-manual.example", "a.example"}


async def test_allowed_sites_are_never_blocked(hass, rig, aioclient_mock):
    rig.web_api.sites = {"www.school.example": True}
    _serve(aioclient_mock, "school.example\ngame.example\n")

    result = await _sync(hass)

    assert result["skipped_allowed"]["examples"] == ["school.example"]
    assert rig.web_api.writes == [("block", "game.example")]
    assert rig.web_api.sites["www.school.example"] is True


@pytest.mark.parametrize(
    ("text", "status"),
    [("", 200), ("# nothing but comments\n", 200), ("<html>error</html>", 200), ("x.example", 500)],
)
async def test_empty_or_failed_download_changes_nothing(hass, rig, aioclient_mock, text, status):
    _serve(aioclient_mock, "a.example\n")
    await _sync(hass)
    rig.web_api.writes.clear()

    _serve(aioclient_mock, text, status)
    with pytest.raises(HomeAssistantError):
        await _sync(hass)
    assert rig.web_api.writes == []
    assert "a.example" in rig.web_api.sites


async def test_unreadable_site_lists_change_nothing(hass, rig, aioclient_mock):
    rig.web_api.read_fails = True
    _serve(aioclient_mock, "a.example\n")
    with pytest.raises(HomeAssistantError, match="nothing was changed"):
        await _sync(hass)
    assert rig.web_api.writes == []


async def test_failed_sites_are_reported_and_retried_next_time(hass, rig, aioclient_mock):
    _serve(aioclient_mock, "a.example\nb.example\nc.example\n")
    rig.web_api.fail = {"b.example"}

    first = await _sync(hass)

    assert first["added"]["count"] == 2
    assert first["failed"]["count"] == 1
    assert first["failed"]["examples"][0]["site"] == "b.example"

    rig.web_api.fail = set()
    rig.web_api.writes.clear()
    second = await _sync(hass)
    assert rig.web_api.writes == [("block", "b.example")]
    assert second["kept"]["count"] == 2


async def test_a_list_cut_short_is_refused(hass, rig, aioclient_mock):
    domains = [f"site{i}.example" for i in range(100)]
    _serve(aioclient_mock, "\n".join(domains))
    await _sync(hass)
    rig.web_api.writes.clear()

    _serve(aioclient_mock, "\n".join(domains[:10]))
    with pytest.raises(HomeAssistantError, match="cut short"):
        await _sync(hass)
    assert rig.web_api.writes == []

    stopped = await _sync(hass, stop=True)
    assert stopped["removed"]["count"] == 100
    assert stopped["stopped"] is True
    assert rig.web_api.sites == {}


async def test_only_https_is_accepted(hass, rig):
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN, "sync_site_list", {"account_id": CHILD, "url": "http://lists.example/x"},
            blocking=True, return_response=True,
        )


async def test_administrators_only(hass, rig, aioclient_mock):
    _serve(aioclient_mock, "a.example\n")
    user = MockUser(is_owner=False).add_to_hass(hass)
    user.mock_policy({"entities": True})
    with pytest.raises(Unauthorized):
        await _sync(hass, user=user)
    assert rig.web_api.writes == []

    admin = MockUser(is_owner=True).add_to_hass(hass)
    result = await _sync(hass, user=admin)
    assert result["added"]["count"] == 1


async def test_a_read_that_lost_every_site_changes_nothing(hass, rig, aioclient_mock):
    _serve(aioclient_mock, "a.example\nb.example\n")
    await _sync(hass)
    rig.web_api.writes.clear()

    async def empty(child_id):
        return {"enabled": True, "exceptions": []}  # a stale or truncated answer

    rig.web_api.get_web_restrictions = empty
    _serve(aioclient_mock, "a.example\n")
    with pytest.raises(HomeAssistantError, match="incomplete"):
        await _sync(hass)
    # stop neither removes nor forgets anything on such an answer.
    with pytest.raises(HomeAssistantError, match="incomplete"):
        await _sync(hass, stop=True)
    assert rig.web_api.writes == []
    stored = await rig._site_sync_store.async_load()
    assert stored[CHILD][URL] == ["a.example", "b.example"]


# --- get_sites -----------------------------------------------------------------


async def _get_sites(hass, user=None):
    return await hass.services.async_call(
        DOMAIN, "get_sites", {"account_id": CHILD}, blocking=True, return_response=True,
        context=Context(user_id=user.id) if user else None,
    )


async def test_get_sites_returns_what_microsoft_stores_and_what_the_list_added(
    hass, rig, aioclient_mock
):
    rig.web_api.sites = {"manual.example": False, "www.school.example": True}
    _serve(aioclient_mock, "a.example\nb.example\n")
    await _sync(hass)

    result = await _get_sites(hass)

    assert sorted(result["blocked"]) == [
        "https://a.example", "https://b.example", "https://manual.example"
    ]
    assert result["allowed"] == ["https://www.school.example"]
    assert result["filter_enabled"] is True
    assert result["from_list"] == ["a.example", "b.example"]
    assert result["from_list_by_url"] == {URL: ["a.example", "b.example"]}


async def test_get_sites_is_for_administrators(hass, rig):
    user = MockUser(is_owner=False).add_to_hass(hass)
    user.mock_policy({"entities": True})
    with pytest.raises(Unauthorized):
        await _get_sites(hass, user=user)


async def test_get_sites_refuses_an_unreadable_answer(hass, rig):
    rig.web_api.read_fails = True
    with pytest.raises(HomeAssistantError):
        await _get_sites(hass)
