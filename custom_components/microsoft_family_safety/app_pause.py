"""pause_app / resume_app for a child's Windows and Xbox apps.

A pause blocks an app for now and resume_app puts back exactly what the app
had before: its daily time limit, or no limit. Both go through the mobile
API's app policy (v3/AppLimits/policies), the resource block_app and
set_app_time_limit write too.

block_app alone cannot be used for this: its write carries no day keys, and
a policy written without them loses its time limit (that is how
remove_app_time_limit removes one). So the policy is read first, saved, and
the pause writes it back with ``blockState`` set, keeping the limit stored
while the app is blocked; resume writes it back as it was.

* The saved policy is the one the app had before its first pause. A repeat
  pause, also after a restart, keeps it.
* An app already blocked (by a parent) is skipped and never touched, so its
  block stays.
* An app whose policy cannot be read is skipped: without it there is nothing
  to restore.
* An app found unblocked at resume time was released by someone else (a
  parent in the Family Safety app): its saved state is forgotten and nothing
  is written over the parent's change.

App ids are Microsoft's, with the platform prefix (``x:`` Xbox, ``appx:`` and
``win32:`` Windows), as the Applications sensor lists them.
"""
from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DAY_KEYS, DOMAIN

if TYPE_CHECKING:
    from .coordinator import FamilySafetyDataUpdateCoordinator

STORAGE_VERSION = 1

#: Saved keys of an app policy besides the day keys.
_POLICY_KEYS = ("enabled", "appTimeEnforcementPolicy", "alternateAppIds", "displayName")

#: blockState values that are not a block.
_NOT_BLOCKED = ("NotBlocked",)

_STATUS_4XX = re.compile(r"status 4\d\d")


def app_platform(app_id: str) -> str | None:
    """The app policy's platform for an app id, None for other apps."""
    if app_id.startswith("x:"):
        return "xbox"
    if app_id.startswith(("appx:", "win32:")):
        return "windows"
    return None


def policy_state(policy: Any) -> dict[str, Any] | None:
    """The restorable part of an app policy, None when it is not one.

    A policy is a dict with ``blockState`` whose day keys are each absent,
    null, or a dict (allowance and intervals, kept as Microsoft gave them).
    """
    if not isinstance(policy, dict) or not isinstance(policy.get("blockState"), str):
        return None
    days: dict[str, Any] = {}
    for day in DAY_KEYS:
        value = policy.get(day)
        if value is None:
            continue
        if not isinstance(value, dict):
            return None
        days[day] = value
    state = {key: policy[key] for key in _POLICY_KEYS if key in policy}
    state["blockState"] = policy["blockState"]
    state["days"] = days
    return state


def policy_body(app_id: str, state: dict[str, Any], block_state: str) -> dict[str, Any]:
    """The PATCH body putting ``state`` back, with ``block_state``."""
    return {
        "appId": app_id,
        "displayName": state.get("displayName") or app_id,
        "enabled": bool(state.get("enabled", True)),
        "blockState": block_state,
        "appTimeEnforcementPolicy": state.get("appTimeEnforcementPolicy") or "Custom",
        "alternateAppIds": list(state.get("alternateAppIds") or []),
        **(state.get("days") or {}),
    }


def _refused(err: Exception) -> bool:
    """Microsoft answered and refused the write (a 4xx), so it did not apply."""
    return bool(_STATUS_4XX.search(str(err)))


class AppPauser:
    """The saved states and the two operations, one at a time per entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.app_pause.{entry_id}")
        self._lock = asyncio.Lock()
        #: account id -> app id -> saved state, as stored.
        self._data: dict[str, dict[str, Any]] = {}

    async def async_load(self) -> None:
        """Read the store once at setup."""
        self._data = await self._store.async_load() or {}

    async def async_remove(self) -> None:
        """Delete the store (the entry is removed)."""
        await self._store.async_remove()

    def saved(self, account_id: Any) -> dict[str, Any]:
        """app id -> saved state, for one child."""
        return dict(self._data.get(str(account_id)) or {})

    async def _async_save(self) -> None:
        self._data = {k: v for k, v in self._data.items() if v}
        await self._store.async_save(self._data)

    async def _async_read_state(
        self, coordinator: FamilySafetyDataUpdateCoordinator, account_id: str,
        app_id: str, app: Any,
    ) -> dict[str, Any] | None:
        """The app's policy now: read from Microsoft, else the activity report's."""
        if coordinator.web_api is not None:
            try:
                state = policy_state(
                    await coordinator.web_api.get_app_policy(
                        account_id, app_id, app_platform(app_id)
                    )
                )
            except Exception:  # noqa: BLE001 - fall back to the report
                state = None
            if state is not None:
                return state
        return policy_state(getattr(app, "policy", None))

    async def async_pause(
        self, coordinator: FamilySafetyDataUpdateCoordinator, account_id: str,
        apps: list[str],
    ) -> dict[str, Any]:
        account_id = str(account_id)
        paused: list[str] = []
        skipped: dict[str, str] = {}
        async with self._lock:
            saved = self._data.setdefault(account_id, {})
            for app_id in dict.fromkeys(apps):
                platform = app_platform(app_id)
                if platform is None:
                    skipped[app_id] = "not a Windows or Xbox app"
                    continue
                app = coordinator.get_application(account_id, app_id)
                state = saved.get(app_id)
                new_record = state is None
                if new_record:
                    if app is None:
                        skipped[app_id] = "unknown app"
                        continue
                    if app.blocked:
                        skipped[app_id] = "already blocked"
                        continue
                    state = await self._async_read_state(coordinator, account_id, app_id, app)
                    if state is None:
                        skipped[app_id] = "app policy unreadable"
                        continue
                    if state["blockState"] not in _NOT_BLOCKED:
                        skipped[app_id] = "already blocked"
                        continue
                    state["platform"] = platform
                    state["paused_at"] = dt_util.utcnow().isoformat()
                    saved[app_id] = state
                    # Saved before the write: a block that went through but
                    # answered an error must still be resumable.
                    await self._async_save()
                # A repeat pause blocks again (it may have been released
                # since) but keeps the state saved by the first one.
                try:
                    await coordinator.async_write_app_policy(
                        account_id, app_id, platform,
                        policy_body(app_id, state, "BlockedAlways"), blocked=True,
                    )
                except Exception as err:  # noqa: BLE001 - reported per app
                    if new_record and _refused(err):
                        # Refused, so not blocked by us: keeping the record
                        # would let resume_app overwrite a later parent block.
                        saved.pop(app_id, None)
                    # Otherwise the block may have gone through: the record
                    # stays and resume_app can undo it.
                    skipped[app_id] = f"block failed: {err}"[:200]
                    continue
                paused.append(app_id)
            await self._async_save()
        coordinator.async_update_listeners()
        return {"account_id": account_id, "paused": paused, "skipped": skipped}

    async def async_resume(
        self, coordinator: FamilySafetyDataUpdateCoordinator, account_id: str,
        apps: list[str] | None,
    ) -> dict[str, Any]:
        account_id = str(account_id)
        resumed: list[str] = []
        released: list[str] = []
        skipped: dict[str, str] = {}
        async with self._lock:
            saved = self._data.setdefault(account_id, {})
            for app_id in dict.fromkeys(apps if apps else list(saved)):
                state = saved.get(app_id)
                if state is None:
                    skipped[app_id] = "not paused"
                    continue
                app = coordinator.get_application(account_id, app_id)
                if app is not None and not app.blocked:
                    # Unblocked by someone else since: leave their change.
                    saved.pop(app_id, None)
                    released.append(app_id)
                    continue
                try:
                    await coordinator.async_write_app_policy(
                        account_id, app_id, state.get("platform") or app_platform(app_id),
                        policy_body(app_id, state, "NotBlocked"), blocked=False,
                    )
                except Exception as err:  # noqa: BLE001 - kept for a later resume
                    skipped[app_id] = f"restore failed: {err}"[:200]
                    continue
                saved.pop(app_id, None)
                resumed.append(app_id)
            await self._async_save()
        coordinator.async_update_listeners()
        return {
            "account_id": account_id,
            "resumed": resumed,
            "released": released,
            "skipped": skipped,
        }
