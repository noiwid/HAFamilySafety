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
* An app whose policy cannot be read, or reads in a shape that may hold a
  limit this code does not see, is not paused: there would be nothing safe
  to put back.
* Resume (and a repeat pause) first reads the policy live. Unblocked since
  the pause: someone released it (``released``). Its limit or block changed
  since the pause: a parent changed it (``changed_by_parent``). Either way
  the parent's state is left as it is and the saved state is forgotten.

App ids are Microsoft's, with the platform prefix (``x:`` Xbox, ``appx:`` and
``win32:`` Windows), as the Applications sensor lists them.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DAY_KEYS, DOMAIN

if TYPE_CHECKING:
    from .coordinator import FamilySafetyDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1

#: Saved keys of an app policy besides the day keys.
_POLICY_KEYS = ("enabled", "appTimeEnforcementPolicy", "alternateAppIds", "displayName")

NOT_BLOCKED = "NotBlocked"
BLOCKED = "BlockedAlways"

UNREADABLE = "policy_unreadable"


def app_platform(app_id: str) -> str | None:
    """The app policy's platform for an app id, None for other apps."""
    if app_id.startswith("x:"):
        return "xbox"
    if app_id.startswith(("appx:", "win32:")):
        return "windows"
    return None


def _limit_outside_days(policy: dict[str, Any]) -> bool:
    """A limit may be set somewhere other than the day keys."""
    if policy.get("enabled") is not False:
        return True
    return any(
        value not in (None, [], {}, "", 0)
        for key, value in policy.items()
        if key not in DAY_KEYS
        and ("allowance" in key.lower() or "interval" in key.lower())
    )


def policy_state(policy: Any) -> dict[str, Any] | None:
    """The restorable part of an app policy, None when it cannot be trusted.

    A policy is a dict with ``blockState`` whose day keys are each absent,
    null, or a dict (allowance and intervals, kept as Microsoft gave them).
    Without any day it must show no limit at all (``enabled`` false and no
    allowance or intervals elsewhere): else a limit could be lost.
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
    if not days and _limit_outside_days(policy):
        return None
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


def _mark(state: dict[str, Any]) -> dict[str, Any]:
    """What a later read is compared with: the block and the days."""
    return {"blockState": state["blockState"], "days": state["days"]}


def _refused(err: Exception) -> bool:
    """Microsoft answered and refused the write (a 4xx), so it did not apply."""
    status = getattr(err, "status", None)
    return isinstance(status, int) and 400 <= status < 500


def _valid_record(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    prior = record.get("prior")
    written = record.get("written")
    return (
        isinstance(prior, dict)
        and isinstance(prior.get("blockState"), str)
        and isinstance(prior.get("days"), dict)
        and isinstance(written, dict)
        and isinstance(written.get("blockState"), str)
        and isinstance(written.get("days"), dict)
        and isinstance(record.get("platform"), str)
    )


def _validated(data: Any) -> dict[str, dict[str, Any]]:
    """The stored data, minus anything not in the shape this module writes."""
    if not isinstance(data, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for account_id, apps in data.items():
        if not isinstance(apps, dict):
            continue
        kept = {
            app_id: record for app_id, record in apps.items()
            if isinstance(app_id, str) and _valid_record(record)
        }
        if len(kept) != len(apps):
            _LOGGER.warning(
                "Dropped %d unreadable paused app record(s) for account %s",
                len(apps) - len(kept), account_id,
            )
        if kept:
            result[str(account_id)] = kept
    return result


def _raise_if_nothing_done(service: str, done: list[str], failed: dict[str, str]) -> None:
    """Fail the call when apps failed and none was handled."""
    if failed and not done:
        details = "; ".join(f"{app}: {why}" for app, why in failed.items())
        raise HomeAssistantError(f"{service} failed: {details}"[:500])


class AppPauser:
    """The saved states and the two operations, one at a time per entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.app_pause.{entry_id}")
        self._lock = asyncio.Lock()
        #: account id -> app id -> {prior, written, platform, paused_at}.
        self._data: dict[str, dict[str, Any]] = {}

    async def async_load(self) -> None:
        """Read the store once at setup, dropping records of an unknown shape."""
        self._data = _validated(await self._store.async_load())

    async def async_remove(self) -> None:
        """Delete the store (the entry is removed)."""
        await self._store.async_remove()

    def saved(self, account_id: Any) -> dict[str, Any]:
        """app id -> saved record, for one child."""
        return dict(self._data.get(str(account_id)) or {})

    def stale_accounts(self, account_ids: set[str]) -> set[str]:
        """Children with saved records that are not in ``account_ids``."""
        return {a for a, apps in self._data.items() if apps} - account_ids

    async def async_prune(self, account_ids: set[str]) -> None:
        """Forget the records of children no longer in the family."""
        async with self._lock:
            stale = self.stale_accounts(account_ids)
            if not stale:
                return
            for account_id in stale:
                _LOGGER.warning(
                    "Forgetting the paused apps of account %s, no longer in the family",
                    account_id,
                )
                self._data.pop(account_id, None)
            await self._async_save()

    async def _async_save(self) -> None:
        self._data = {k: v for k, v in self._data.items() if v}
        await self._store.async_save(self._data)

    @staticmethod
    async def _async_read(
        coordinator: FamilySafetyDataUpdateCoordinator, account_id: str,
        app_id: str, platform: str,
    ) -> dict[str, Any] | None:
        """The app's policy now, from Microsoft; None when it cannot be trusted."""
        if coordinator.web_api is None:
            return None
        try:
            policy = await coordinator.web_api.get_app_policy(account_id, app_id, platform)
        except Exception as err:  # noqa: BLE001 - reported per app
            _LOGGER.debug("Reading the policy of %s failed: %s", app_id, err)
            return None
        return policy_state(policy)

    async def _async_block(
        self, coordinator: FamilySafetyDataUpdateCoordinator, account_id: str,
        app_id: str, record: dict[str, Any],
    ) -> None:
        """Write the saved policy blocked and note how Microsoft then reports it."""
        platform = record["platform"]
        answer = await coordinator.async_write_app_policy(
            account_id, app_id, platform,
            policy_body(app_id, record["prior"], BLOCKED), blocked=True,
        )
        written = policy_state(answer)
        if written is None:
            written = await self._async_read(coordinator, account_id, app_id, platform)
        if written is not None and written["blockState"] != NOT_BLOCKED:
            record["written"] = _mark(written)

    @staticmethod
    def _parent_change(record: dict[str, Any], live: dict[str, Any]) -> str | None:
        """``changed_by_parent``, ``released`` or None (our block still in force)."""
        written = record["written"]
        if live["days"] != written["days"]:
            return "changed_by_parent"
        if live["blockState"] == NOT_BLOCKED:
            return "released"
        if live["blockState"] != written["blockState"]:
            return "changed_by_parent"
        return None

    async def async_pause(
        self, coordinator: FamilySafetyDataUpdateCoordinator, account_id: str,
        apps: list[str],
    ) -> dict[str, Any]:
        account_id = str(account_id)
        paused: list[str] = []
        changed: list[str] = []
        skipped: dict[str, str] = {}
        failed: dict[str, str] = {}
        async with self._lock:
            saved = self._data.setdefault(account_id, {})
            for app_id in dict.fromkeys(apps):
                platform = app_platform(app_id)
                if platform is None:
                    skipped[app_id] = "not a Windows or Xbox app"
                    continue
                record = saved.get(app_id)
                new_record = record is None
                if new_record:
                    app = coordinator.get_application(account_id, app_id)
                    if app is None:
                        skipped[app_id] = "unknown app"
                        continue
                    if app.blocked:
                        skipped[app_id] = "already blocked"
                        continue
                live = await self._async_read(
                    coordinator, account_id, app_id, record["platform"] if record else platform
                )
                if live is None:
                    failed[app_id] = UNREADABLE
                    continue
                if new_record:
                    if live["blockState"] != NOT_BLOCKED:
                        skipped[app_id] = "already blocked"
                        continue
                    record = {
                        "prior": live,
                        # What the block sends; replaced by Microsoft's answer.
                        "written": {"blockState": BLOCKED, "days": dict(live["days"])},
                        "platform": platform,
                        "paused_at": dt_util.utcnow().isoformat(),
                    }
                    saved[app_id] = record
                    # Saved before the write: a block that went through but
                    # answered an error must still be resumable.
                    await self._async_save()
                else:
                    change = self._parent_change(record, live)
                    if change == "changed_by_parent":
                        saved.pop(app_id, None)
                        changed.append(app_id)
                        continue
                    if change is None:
                        paused.append(app_id)  # still paused, nothing to write
                        continue
                    # Released since: block again, keeping the first saved state.
                try:
                    await self._async_block(coordinator, account_id, app_id, record)
                except Exception as err:  # noqa: BLE001 - reported per app
                    if new_record and _refused(err):
                        # Refused, so not blocked by us: keeping the record
                        # would let resume_app overwrite a later parent block.
                        saved.pop(app_id, None)
                    # Otherwise the block may have gone through: the record
                    # stays and resume_app can undo it.
                    failed[app_id] = f"block failed: {err}"[:200]
                    continue
                paused.append(app_id)
            await self._async_save()
        coordinator.async_update_listeners()
        _raise_if_nothing_done("pause_app", paused + changed, failed)
        return {
            "account_id": account_id,
            "paused": paused,
            "changed_by_parent": changed,
            "skipped": skipped,
            "failed": failed,
        }

    async def async_resume(
        self, coordinator: FamilySafetyDataUpdateCoordinator, account_id: str,
        apps: list[str] | None,
    ) -> dict[str, Any]:
        account_id = str(account_id)
        resumed: list[str] = []
        released: list[str] = []
        changed: list[str] = []
        skipped: dict[str, str] = {}
        failed: dict[str, str] = {}
        async with self._lock:
            saved = self._data.setdefault(account_id, {})
            for app_id in dict.fromkeys(apps if apps else list(saved)):
                record = saved.get(app_id)
                if record is None:
                    skipped[app_id] = "not paused"
                    continue
                live = await self._async_read(
                    coordinator, account_id, app_id, record["platform"]
                )
                if live is None:
                    # Kept: a later resume_app can still put it back.
                    failed[app_id] = UNREADABLE
                    continue
                change = self._parent_change(record, live)
                if change is not None:
                    # Leave the parent's state; ours is no longer in force.
                    saved.pop(app_id, None)
                    (released if change == "released" else changed).append(app_id)
                    continue
                try:
                    await coordinator.async_write_app_policy(
                        account_id, app_id, record["platform"],
                        policy_body(app_id, record["prior"], NOT_BLOCKED), blocked=False,
                    )
                except Exception as err:  # noqa: BLE001 - kept for a later resume
                    failed[app_id] = f"restore failed: {err}"[:200]
                    continue
                saved.pop(app_id, None)
                resumed.append(app_id)
            await self._async_save()
        coordinator.async_update_listeners()
        _raise_if_nothing_done("resume_app", resumed + released + changed, failed)
        return {
            "account_id": account_id,
            "resumed": resumed,
            "released": released,
            "changed_by_parent": changed,
            "skipped": skipped,
            "failed": failed,
        }
