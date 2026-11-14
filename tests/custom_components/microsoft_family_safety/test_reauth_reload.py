"""A finished reauthentication reloads the entry.

The update listener only reloads on an options change since 2.0.24. A coordinator
loaded without web credentials (Xbox just ticked) kept that view after the
sign-in, and asked for a new one at its next update.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE
import pytest

from custom_components.microsoft_family_safety.config_flow import FamilySafetyConfigFlow


@pytest.mark.parametrize("source", [SOURCE_REAUTH, SOURCE_RECONFIGURE])
async def test_finished_sign_in_reloads_the_entry(source: str) -> None:
    flow = FamilySafetyConfigFlow()
    flow.hass = MagicMock()
    flow.context = {"source": source}
    flow._pending_data = {"refresh_token": "new"}
    entry = MagicMock(entry_id="entry-1", options={})

    with (
        patch.object(flow, "_get_existing_entry", return_value=entry),
        patch.object(flow, "async_update_and_abort", return_value={"type": "abort"}) as update,
    ):
        result = await flow.async_step_reauth_success({})

    assert result == {"type": "abort"}
    assert update.call_args.kwargs["data_updates"]["refresh_token"] == "new"
    flow.hass.config_entries.async_schedule_reload.assert_called_once_with("entry-1")


INFO = {"refresh_token": "M.R3_token", "user_id": "1111", "title": "Parent"}


async def test_manual_step_keys_a_new_entry_on_the_user_id() -> None:
    """Audit 2026-11-13: the same account added twice must be refused, its id stored."""
    from homeassistant.config_entries import SOURCE_USER

    flow = FamilySafetyConfigFlow()
    flow.hass = MagicMock()
    flow.context = {"source": SOURCE_USER}
    with (
        patch.object(flow, "async_set_unique_id") as set_id,
        patch.object(flow, "_abort_if_unique_id_configured") as abort_if,
        patch.object(flow, "async_create_entry", return_value={"type": "create"}) as create,
    ):
        await flow._async_finish_manual_auth(dict(INFO))
    set_id.assert_awaited_once_with("1111")
    abort_if.assert_called_once()
    assert create.call_args.kwargs["data"]["auth_user_id"] == "1111"


async def test_manual_step_refuses_another_account_on_reauth() -> None:
    flow = FamilySafetyConfigFlow()
    flow.hass = MagicMock()
    flow.context = {"source": SOURCE_REAUTH}
    entry = MagicMock(data={"auth_user_id": "2222"}, options={})
    with patch.object(flow, "_get_existing_entry", return_value=entry):
        result = await flow._async_finish_manual_auth(dict(INFO))
    assert result["reason"] == "wrong_account"


async def test_manual_step_updates_the_entry_on_reauth() -> None:
    flow = FamilySafetyConfigFlow()
    flow.hass = MagicMock()
    flow.context = {"source": SOURCE_REAUTH}
    entry = MagicMock(entry_id="entry-1", data={"auth_user_id": "1111"}, options={})
    with (
        patch.object(flow, "_get_existing_entry", return_value=entry),
        patch.object(flow, "async_update_and_abort", return_value={"type": "abort"}) as update,
    ):
        await flow._async_finish_manual_auth(dict(INFO))
    updates = update.call_args.kwargs["data_updates"]
    assert updates["refresh_token"] == "M.R3_token" and updates["web_cookies"] is None
