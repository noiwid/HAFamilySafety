"""pause_app / resume_app for a child's Windows and Xbox apps.

A pause blocks an app for now and resume_app puts back exactly what the app
had before: its time limit, or no limit.

Microsoft's mobile API has no read of one app's policy (every GET on
v1-v4/AppLimits/policies answers 405). The app activity report
(v4/activityReport/appUsage, read per platform) carries it: each entry has
``policy``, the whole app policy (blockState, enabled,
appTimeEnforcementPolicy, the day keys with allowance and intervals...), or
null for an app without a limit.

block_app alone cannot be used: its write carries no day keys and
``appTimeEnforcementPolicy: WeekendAndWeekday``, which wipes the app's
limit. So:

* An app without a limit (``policy`` null) is paused with the block_app
  write and resumed with the unblock_app write.
* An app with a limit is paused by writing its saved policy back with
  ``blockState: BlockedAlways`` and the same day keys, so the limit stays
  stored while blocked; resume writes it back with ``NotBlocked``.
* The saved state is the one the app had before its first pause. A repeat
  pause, also after a restart, keeps it.
* An app already blocked is skipped and never touched, so its block stays.
  An app whose entry cannot be read, or whose policy is in a shape that may
  hide a limit, is not paused: there would be nothing safe to put back.
* Resume (and a repeat pause) first read the entry live and compare it with
  what the pause wrote. Unblocked since: someone released it
  (``released``). Its days or block changed since: a parent changed it
  (``changed_by_parent``). Either way the parent's state is left as it is
  and the saved state is forgotten. A failed read keeps it for later.
* The block switch and the block_app / unblock_app services go through the
  same paths for a paused app (``async_block`` / ``async_unblock``), so
  turning the switch off puts the limit back instead of wiping it.
* Saved states are kept until resumed or until the config entry is removed,
  not pruned when a child is missing from one roster read.

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

#: Keys of a policy that hold a limit: the seven days and the groupings.
LIMIT_KEYS = (*DAY_KEYS, "weekday", "weekend", "everyday")

NOT_BLOCKED = "NotBlocked"
BLOCKED = "BlockedAlways"

UNREADABLE = "policy_unreadable"
ALREADY_BLOCKED = "already blocked"


def app_platform(app_id: str) -> str | None:
    """The Plat-Info platform of an app id, None for other apps."""
    app_id = app_id.lower()
    if app_id.startswith(("appx:", "win32:")) or (
        app_id.startswith("x:") and "windowsplt" in app_id
    ):
        return "windows"
    if app_id.startswith("x:"):
        return "xbox"
    return None


def _limit_days(policy: dict[str, Any]) -> dict[str, Any] | None:
    """The policy's non-null limit keys, None if one is not an object."""
    days: dict[str, Any] = {}
    for key in LIMIT_KEYS:
        value = policy.get(key)
        if value is None:
            continue
        if not isinstance(value, dict):
            return None
        days[key] = value
    return days


def read_entry(entry: Any) -> dict[str, Any] | None:
    """An activity report entry as {block, days, policy, name}, None if unreadable.

    ``block`` is the block state in force (``Legacy`` for a legacy block),
    ``days`` the limit keys set (empty without a limit) and ``policy`` the
    entry's policy object or None.
    """
    if not isinstance(entry, dict) or not isinstance(entry.get("blockState"), str):
        return None
    policy = entry.get("policy")
    if policy is not None and not (
        isinstance(policy, dict) and isinstance(policy.get("blockState"), str)
    ):
        return None
    days = _limit_days(policy) if policy else {}
    if days is None:
        return None
    if entry.get("isLegacyBlocked"):
        block = "Legacy"
    elif policy and policy["blockState"] != NOT_BLOCKED:
        block = policy["blockState"]
    else:
        block = entry["blockState"]
    return {
        "block": block,
        "days": days,
        "policy": policy,
        "name": (policy or {}).get("displayName") or entry.get("displayName"),
    }


def saved_policy(policy: dict[str, Any] | None) -> dict[str, Any] | None:
    """The writable part of a policy, kept to write it back.

    Raises ValueError when the policy may hold a limit outside the limit
    keys (``enabled`` but no day set): it could not be put back.
    """
    if policy is None:
        return None
    days = _limit_days(policy) or {}
    if not days and policy.get("enabled") is not False:
        raise ValueError("enabled policy without a limit")
    return {
        "enabled": policy.get("enabled", True),
        "appTimeEnforcementPolicy": policy.get("appTimeEnforcementPolicy") or "Custom",
        "alternateAppIds": list(policy.get("alternateAppIds") or []),
        "days": days,
    }


def write_body(
    app_id: str, name: str | None, policy: dict[str, Any] | None, block_state: str
) -> dict[str, Any]:
    """The PATCH body: the saved policy with ``block_state``.

    Without a policy it is the block_app / unblock_app body, with the app's
    name instead of its id as displayName.
    """
    body: dict[str, Any] = {
        "appId": app_id,
        "displayName": name or app_id,
        "blockState": block_state,
        "blocked": False,
    }
    if policy is None:
        return {**body, "appTimeEnforcementPolicy": "WeekendAndWeekday", "enabled": True}
    return {
        **body,
        "enabled": bool(policy["enabled"]),
        "appTimeEnforcementPolicy": policy["appTimeEnforcementPolicy"],
        "alternateAppIds": list(policy["alternateAppIds"]),
        **policy["days"],
    }


def _refused(err: Exception) -> bool:
    """Microsoft answered and refused the write (a 4xx), so it did not apply."""
    status = getattr(err, "status", None)
    return isinstance(status, int) and 400 <= status < 500


def _valid_record(record: Any) -> bool:
    if not isinstance(record, dict) or record.get("platform") not in ("windows", "xbox"):
        return False
    if not isinstance(record.get("written_days"), dict):
        return False
    policy = record.get("policy", False)
    return policy is None or (
        isinstance(policy, dict)
        and isinstance(policy.get("days"), dict)
        and isinstance(policy.get("appTimeEnforcementPolicy"), str)
        and isinstance(policy.get("alternateAppIds"), list)
        and "enabled" in policy
    )


def _validated(data: Any) -> dict[str, dict[str, Any]]:
    """The stored data, minus anything not in the shape this module writes."""
    if not isinstance(data, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for account_id, apps in data.items():
        if not isinstance(apps, dict):
            _LOGGER.warning("Dropped unreadable paused apps of account %s", account_id)
            continue
        kept = {
            app_id: record for app_id, record in apps.items()
            if isinstance(app_id, str) and _valid_record(record)
        }
        if len(kept) != len(apps):
            _LOGGER.warning(
                "Dropped %d unreadable paused app record(s) of account %s",
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


def _parent_change(record: dict[str, Any], live: dict[str, Any]) -> str | None:
    """``changed_by_parent``, ``released`` or None (our pause still in force)."""
    if live["days"] != record["written_days"]:
        return "changed_by_parent"
    if live["block"] == NOT_BLOCKED:
        return "released"
    if live["block"] != BLOCKED:
        return "changed_by_parent"
    return None


class AppPauser:
    """The saved states and the two operations, one at a time per entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.app_pause.{entry_id}")
        self._lock = asyncio.Lock()
        #: account id -> app id -> {platform, name, policy, written_days, paused_at}.
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

    async def _async_save(self) -> None:
        self._data = {k: v for k, v in self._data.items() if v}
        await self._store.async_save(self._data)

    @staticmethod
    async def _async_read(
        coordinator: FamilySafetyDataUpdateCoordinator, account_id: str,
        app_ids: list[str],
    ) -> dict[str, dict[str, Any] | None]:
        """app id -> read_entry() of its activity report entry now, or None.

        One report per platform for all the apps of the call.
        """
        result: dict[str, dict[str, Any] | None] = {app_id: None for app_id in app_ids}
        if coordinator.web_api is None or not app_ids:
            return result
        platforms = {app_platform(app_id) for app_id in app_ids} - {None}
        for platform in sorted(platforms):
            try:
                entries = await coordinator.web_api.get_app_activity(account_id, platform)
            except Exception as err:  # noqa: BLE001 - reported per app
                _LOGGER.debug("Reading the %s app activity failed: %s", platform, err)
                continue
            by_id = {entry.get("appId"): entry for entry in entries}
            for app_id in app_ids:
                if app_platform(app_id) == platform:
                    result[app_id] = read_entry(by_id.get(app_id))
        return result

    async def async_pause(
        self, coordinator: FamilySafetyDataUpdateCoordinator, account_id: str,
        apps: list[str],
    ) -> dict[str, Any]:
        account_id = str(account_id)
        paused: list[str] = []
        changed: list[str] = []
        skipped: dict[str, str] = {}
        failed: dict[str, str] = {}
        written: list[str] = []
        async with self._lock:
            saved = self._data.setdefault(account_id, {})
            todo: list[str] = []
            for app_id in dict.fromkeys(apps):
                if app_platform(app_id) is None:
                    skipped[app_id] = "not a Windows or Xbox app"
                elif app_id not in saved and coordinator.get_application(
                    account_id, app_id
                ) is None:
                    skipped[app_id] = "unknown app"
                else:
                    todo.append(app_id)
            live = await self._async_read(coordinator, account_id, todo)
            for app_id in todo:
                now = live[app_id]
                if now is None:
                    failed[app_id] = UNREADABLE
                    continue
                record = saved.get(app_id)
                new_record = record is None
                if new_record:
                    if now["block"] != NOT_BLOCKED:
                        skipped[app_id] = ALREADY_BLOCKED
                        continue
                    try:
                        policy = saved_policy(now["policy"])
                    except ValueError:
                        failed[app_id] = UNREADABLE
                        continue
                    record = {
                        "platform": app_platform(app_id),
                        "name": now["name"],
                        "policy": policy,
                        # What the block writes; replaced by a read back.
                        "written_days": dict(policy["days"]) if policy else {},
                        "paused_at": dt_util.utcnow().isoformat(),
                    }
                    saved[app_id] = record
                    # Saved before the write: a block that went through but
                    # answered an error must still be resumable.
                    await self._async_save()
                else:
                    change = _parent_change(record, now)
                    if change == "changed_by_parent":
                        saved.pop(app_id, None)
                        changed.append(app_id)
                        continue
                    if change is None:
                        paused.append(app_id)  # still paused, nothing to write
                        continue
                    # Released since: block again, keeping the saved state.
                try:
                    await coordinator.async_write_app_policy(
                        account_id, app_id, record["platform"],
                        write_body(app_id, record["name"], record["policy"], BLOCKED),
                        blocked=True,
                    )
                except Exception as err:  # noqa: BLE001 - reported per app
                    if new_record and _refused(err):
                        # Refused, so not blocked by us: keeping the record
                        # would let resume_app undo a later parent block.
                        saved.pop(app_id, None)
                    # Otherwise the block may have gone through: the record
                    # stays and resume_app can undo it.
                    failed[app_id] = f"block failed: {err}"[:200]
                    continue
                paused.append(app_id)
                written.append(app_id)
            # Note the days as Microsoft now reports them under our block,
            # so a later comparison is not fooled by its own formatting.
            for app_id, now in (await self._async_read(coordinator, account_id, written)).items():
                if now is not None and now["block"] == BLOCKED and app_id in saved:
                    saved[app_id]["written_days"] = now["days"]
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
        async with self._lock:
            result = await self._async_resume_locked(coordinator, str(account_id), apps)
        coordinator.async_update_listeners()
        _raise_if_nothing_done(
            "resume_app",
            result["resumed"] + result["released"] + result["changed_by_parent"],
            result["failed"],
        )
        return result

    async def _async_resume_locked(
        self, coordinator: FamilySafetyDataUpdateCoordinator, account_id: str,
        apps: list[str] | None,
    ) -> dict[str, Any]:
        """async_resume's work, with self._lock held by the caller."""
        resumed: list[str] = []
        released: list[str] = []
        changed: list[str] = []
        skipped: dict[str, str] = {}
        failed: dict[str, str] = {}
        saved = self._data.setdefault(account_id, {})
        todo: list[str] = []
        for app_id in dict.fromkeys(apps if apps else list(saved)):
            if app_id in saved:
                todo.append(app_id)
            else:
                skipped[app_id] = "not paused"
        live = await self._async_read(coordinator, account_id, todo)
        for app_id in todo:
            now = live[app_id]
            if now is None:
                # Kept: a later resume_app can still put it back.
                failed[app_id] = UNREADABLE
                continue
            record = saved[app_id]
            change = _parent_change(record, now)
            if change is not None:
                # Leave the parent's state; our pause is no longer in force.
                saved.pop(app_id, None)
                (released if change == "released" else changed).append(app_id)
                continue
            try:
                await coordinator.async_write_app_policy(
                    account_id, app_id, record["platform"],
                    write_body(app_id, record["name"], record["policy"], NOT_BLOCKED),
                    blocked=False,
                )
            except Exception as err:  # noqa: BLE001 - kept for a later resume
                failed[app_id] = f"restore failed: {err}"[:200]
                continue
            saved.pop(app_id, None)
            resumed.append(app_id)
        await self._async_save()
        return {
            "account_id": account_id,
            "resumed": resumed,
            "released": released,
            "changed_by_parent": changed,
            "skipped": skipped,
            "failed": failed,
        }

    async def async_block(
        self, coordinator: FamilySafetyDataUpdateCoordinator, account_id: str,
        app_id: str,
    ) -> bool:
        """Block a paused app through the pause, so its limit stays stored.

        block_app's body has no day keys and would wipe it. True when done
        (still blocked by the pause, or blocked again after a release).
        False when the app is not paused, or a parent changed it since: the
        caller blocks it as usual.
        """
        if app_id not in self.saved(account_id):
            return False
        result = await self.async_pause(coordinator, account_id, [app_id])
        return bool(result["paused"])

    async def async_unblock(
        self, coordinator: FamilySafetyDataUpdateCoordinator, account_id: str,
        app_id: str,
    ) -> bool:
        """Unblock a paused app by resuming it, so its limit comes back.

        unblock_app's body has no day keys and would wipe it. True when done
        (resumed, or someone unblocked it already, also a concurrent resume).
        False when the app is not paused, or a parent changed it since and it
        is still blocked: the caller unblocks it as usual. Raises
        HomeAssistantError, keeping the saved state, when it cannot be read
        or written back.
        """
        account_id = str(account_id)
        if app_id not in self.saved(account_id):
            return False
        async with self._lock:
            if app_id not in self._data.get(account_id, {}):
                # Paused when this call started and resumed meanwhile (by a
                # concurrent unblock or resume_app): done unless it is
                # blocked again, so the restored limit is not wiped.
                live = (await self._async_read(coordinator, account_id, [app_id]))[app_id]
                return live is None or live["block"] == NOT_BLOCKED
            result = await self._async_resume_locked(coordinator, account_id, [app_id])
            done = bool(result["resumed"] or result["released"])
            if not done and result["changed_by_parent"]:
                live = (await self._async_read(coordinator, account_id, [app_id]))[app_id]
                done = live is not None and live["block"] == NOT_BLOCKED
        coordinator.async_update_listeners()
        _raise_if_nothing_done(
            "resume_app",
            result["resumed"] + result["released"] + result["changed_by_parent"],
            result["failed"],
        )
        return done
