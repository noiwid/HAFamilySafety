"""The mobile API client uses Home Assistant's shared HTTP session."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import aiohttp

from custom_components.microsoft_family_safety.api_client import FamilySafetyWebAPI


async def test_mobile_api_uses_the_shared_session_and_never_closes_it() -> None:
    shared = Mock(spec=aiohttp.ClientSession)
    shared.closed = False
    shared.close = AsyncMock()
    api = FamilySafetyWebAPI(Mock(), session=shared)

    await api._ensure_session()
    assert api._session is shared

    await api.close()
    shared.close.assert_not_awaited()


async def test_without_a_shared_session_a_private_one_is_closed() -> None:
    api = FamilySafetyWebAPI(Mock())

    await api._ensure_session()
    private = api._session
    assert private is not None and not private.closed

    await api.close()
    assert private.closed
