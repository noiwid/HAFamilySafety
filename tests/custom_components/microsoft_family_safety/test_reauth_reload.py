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
