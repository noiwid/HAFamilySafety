"""Data coordinator for Microsoft Family Safety."""
from __future__ import annotations

from datetime import datetime, timedelta
import asyncio
import hashlib
import json
import logging
import time
from typing import Any

from pyfamilysafety import FamilySafety
from pyfamilysafety.account import Account
from pyfamilysafety.application import Application
from pyfamilysafety.device import Device
from pyfamilysafety.enum import OverrideTarget, OverrideType
from pyfamilysafety.exceptions import AggregatorException, HttpException
from yarl import URL

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from ._pyfamilysafety_compat import apply_patches, is_stale_roster_error
from .api_client import FamilySafetyWebAPI
from .auth.addon_client import AddonCookieClient
from .xbox_family_api import (
    OVERRIDE_ALLOW,
    OVERRIDE_BLOCK,
    OVERRIDE_REVOKE,
    XboxFamilyAPI,
    XboxFamilyAuthError,
    XboxFamilyError,
    build_authorize_url,
    overrides_to_policy,
    schedule_to_policy,
    usage_today,
)
from .const import (
    CONF_XBOX_FEATURES,
    DEFAULT_XBOX_FEATURES,
    CONF_API_KEY,
    CONF_AUTH_URL,
    CONF_REFRESH_TOKEN,
    CONF_UPDATE_INTERVAL,
    CONF_WEB_COOKIES,
    CONF_WEB_FAMILY_REFERER,
    CONF_WEB_FAMILY_TOKEN,
    DAY_KEYS,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    ERROR_AUTH_FAILED,
    ERROR_TOKEN_EXPIRED,
)

_LOGGER = logging.getLogger(__name__)
STORAGE_KEY = f"{DOMAIN}.saved_screentime"
STORAGE_VERSION = 1
AUTH_STORAGE_VERSION = 1
AUTH_NOTIFICATION_ID = "familysafety_auth_expired"
ROSTER_NOTIFICATION_ID = "familysafety_roster_error"
#: Runtime-store flag set when Microsoft rejected the persisted Family token.
_FAMILY_TOKEN_REJECTED = "web_family_token_rejected"
#: Polls in a row ending in family_context_state == "auth_required" before the
#: coordinator treats the Family session as gone and starts reauthentication.
_FAMILY_AUTH_REQUIRED_POLLS = 2


def _range_to_slots(start_hour: int, start_minute: int, end_hour: int, end_minute: int) -> list[bool]:
    intervals = [False] * 48
    start_slot = (start_hour * 60 + start_minute) // 30
    end_slot = -(-(end_hour * 60 + end_minute) // 30)  # ceiling division
    for i in range(start_slot, min(end_slot, 48)):
        intervals[i] = True
    return intervals


def _policy_intervals_to_slots(day_data: dict[str, Any]) -> list[bool] | None:
    """Convert saved Microsoft interval formats to 48 half-hour slots.

    The current account.microsoft.com response uses allowedIntervals objects
    with begin/end values. Older integration builds stored a boolean timeline.
    Supporting both is required so account lock/unlock restores the exact
    schedule that was visible before the lock.
    """
    timeline = day_data.get("timeline")
    if (
        isinstance(timeline, list)
        and len(timeline) == 48
        and all(isinstance(item, bool) for item in timeline)
    ):
        return list(timeline)

    raw = day_data.get("allowedIntervals")
    if raw is None:
        raw = day_data.get("AllowedIntervals")
    if not isinstance(raw, list):
        return None

    slots = [False] * 48
    found = False

    def to_minutes(value: object) -> int | None:
        if not isinstance(value, str):
            return None
        try:
            parts = value.split(":")
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            return None
        if hour == 24 and minute == 0:
            return 1440
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            return None
        return hour * 60 + minute

    for interval in raw:
        if not isinstance(interval, dict):
            continue
        begin = to_minutes(
            interval.get("begin")
            or interval.get("Begin")
            or interval.get("start")
            or interval.get("Start")
        )
        end = to_minutes(interval.get("end") or interval.get("End"))
        if begin is None or end is None or end <= begin:
            continue
        start_slot = max(0, min(47, begin // 30))
        end_slot = max(0, min(48, -(-end // 30)))
        for slot in range(start_slot, end_slot):
            slots[slot] = True
        found = True

    # An explicitly empty list means Microsoft allows no time window for that
    # day. Preserve it as 48 disabled slots instead of substituting the legacy
    # 07:00-22:00 default during a later account-unlock restore.
    return slots if found or raw == [] else None


def _ms_to_minutes(milliseconds: int | float | None) -> int:
    if not milliseconds:
        return 0
    return int(milliseconds / 60000)


class FamilySafetyDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch and coordinate Microsoft Family Safety state."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        update_interval = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=update_interval),
            config_entry=entry,
        )
        self.entry = entry
        self.api: FamilySafety | None = None
        self.web_api: FamilySafetyWebAPI | None = None
        self._accounts: dict[str, Account] = {}
        self._devices: dict[str, Device] = {}
        self._is_retrying_auth = False
        self._saved_screentime: dict[str, dict[str, Any]] = {}
        #: Accounts whose restore point this coordinator deliberately released
        #: after a fully successful unlock. Only these may be removed from the
        #: on-disk store; anything else missing from the in-memory dict is
        #: simply not loaded yet and must be preserved.
        self._released_screentime: set[str] = set()
        # One lock per child: lock, unlock and the limits switch each read the
        # schedule, write 14 times and update the restore point, and two of
        # them interleaved can leave the child zeroed with no restore point.
        self._account_locks: dict[str, asyncio.Lock] = {}
        # One store per entry: two Microsoft accounts must not share a file.
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}"
        )
        #: Pre-2.0.25 shared file, read once when the per-entry store is empty.
        self._legacy_store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._auth_store: Store = Store(
            hass, AUTH_STORAGE_VERSION, f"{DOMAIN}.auth.{entry.entry_id}"
        )
        self._runtime_auth_state: dict[str, Any] = {}
        self._reauth_requested = False
        self._reauth_reason: str | None = None
        #: Consecutive polls whose Family bootstrap ended on a login page.
        self._family_auth_required_polls = 0
        auth_url = entry.options.get(CONF_AUTH_URL) or entry.data.get(CONF_AUTH_URL)
        api_key = entry.options.get(CONF_API_KEY) or entry.data.get(CONF_API_KEY)
        self._addon_client = AddonCookieClient(
            hass, auth_url=auth_url, api_key=api_key
        )
        #: Xbox features off: the entry runs on the mobile token only and
        #: never touches the Family web session or the legacy add-on.
        self._mobile_only = not entry.options.get(
            CONF_XBOX_FEATURES, DEFAULT_XBOX_FEATURES
        )
        self._has_web_credentials = bool(entry.data.get(CONF_WEB_COOKIES))
        self._web_cookies_loaded = False
        self._auth_notification_sent = False
        self._roster_notification_sent = False
        self._platform_override_until: dict[tuple[str, str], datetime] = {}
        #: Children whose Xbox lock state is known: Microsoft's Xbox policy
        #: was read, or a web lock/unlock from here succeeded. Until then the
        #: Xbox lock switch reports unknown rather than "unlocked".
        self._xbox_lock_known: set[str] = set()
        #: monotonic time of the next web activity read per child (#65); the
        #: report covers 7 days, so every 30 min is plenty.
        self._web_activity_next: dict[str, float] = {}
        #: Xbox settings through the Xbox Family Settings app's API
        #: (xapprove), on its own refresh token instead of the web session.
        self.xbox_family = XboxFamilyAPI(hass, entry.entry_id)
        self._xbox_xuids: dict[str, str] = {}
        #: Roster accounts with no Xbox child of the same name (no Xbox, or
        #: names that differ between the Xbox profile and the Microsoft account).
        self._xbox_unmatched: set[str] = set()
        #: monotonic time before which no silent Xbox link is attempted again.
        self._xbox_link_retry_at = 0.0
        self._xbox_auth_warned = False

    @property
    def _native_web_auth(self) -> bool:
        """True when the entry holds its own Family web session (native mode)."""
        return self._has_web_credentials and not self._mobile_only

    @property
    def xbox_features(self) -> bool:
        """Whether Xbox entities (and the web session they need) are wanted."""
        return not self._mobile_only

    def _entry_auth_anchor(self) -> str:
        """Fingerprint the base browser/mobile login stored in the config entry.

        Runtime credentials may rotate between restarts, but a manual reconfigure
        must supersede the previous runtime cache. Microsoft can return the same
        refresh-token string for a renewed login, so include the entry browser
        credential generation as well. Values are hashed locally and never logged.
        """
        material = {
            "refresh_token": self.entry.data.get(CONF_REFRESH_TOKEN, ""),
            "web_family_token": self.entry.data.get(CONF_WEB_FAMILY_TOKEN, ""),
            "web_family_referer": self.entry.data.get(CONF_WEB_FAMILY_REFERER, ""),
            "web_cookies": self.entry.data.get(CONF_WEB_COOKIES, []),
        }
        encoded = json.dumps(
            material, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _policy_key(account_id: Any) -> str:
        """Normalize an account id used as a saved-policy key.

        Account ids reach the coordinator as int (from the mobile API roster)
        and as str (from the web API and from service calls), while JSON object
        keys are always str. Without normalizing, a restore point saved before
        a reload became invisible afterwards: `1234567890123456 in
        {"1234567890123456": ...}` is False, so unlock believed there was
        nothing to restore and overwrote the schedule with its default.
        """
        return str(account_id)

    def has_saved_screentime(self, account_id: Any) -> bool:
        """Whether a restore point (account lock / limits off) is kept for this child."""
        return self._policy_key(account_id) in self._saved_screentime

    async def async_load_saved_screentime(self) -> None:
        data = await self._store.async_load()
        if data is None:
            # First run on the per-entry store: take over the shared file.
            # It is left in place (another entry may still need it); a
            # restore point released later is removed from the new store only.
            data = await self._legacy_store.async_load()
            if isinstance(data, dict) and data:
                await self._store.async_save(data)
        if isinstance(data, dict):
            self._saved_screentime = {
                self._policy_key(key): value for key, value in data.items()
            }
        auth_state = await self._auth_store.async_load()
        if (
            isinstance(auth_state, dict)
            and auth_state.get("anchor") == self._entry_auth_anchor()
        ):
            self._runtime_auth_state = auth_state
        else:
            self._runtime_auth_state = {}
        runtime_cookies = self._runtime_auth_state.get(CONF_WEB_COOKIES)
        self._has_web_credentials = bool(
            runtime_cookies or self.entry.data.get(CONF_WEB_COOKIES)
        )

    async def _async_save_screentime(self) -> None:
        """Persist saved screentime policies to HA storage.

        These policies are the only way back from a lock, so writing must never
        lose one. The in-memory dict is not authoritative: a config-entry reload
        builds a fresh coordinator whose dict starts empty, and any save before
        the store is read back would wipe the file. Merge onto what is on disk,
        and only drop an entry that this coordinator deliberately released.
        """
        stored = await self._store.async_load()
        merged: dict[str, Any] = dict(stored) if isinstance(stored, dict) else {}
        merged.update(self._saved_screentime)
        for account_id in self._released_screentime:
            merged.pop(account_id, None)
        if merged != self._saved_screentime:
            self._saved_screentime = merged
        await self._store.async_save(merged)

    async def _async_persist_runtime_auth(
        self,
        *,
        refresh_token: str | None = None,
        web_cookies: list[dict[str, Any]] | None = None,
        family_token: str | None = None,
        family_referer: str | None = None,
    ) -> None:
        """Persist rotated credentials without modifying/reloading the config entry."""
        next_state = dict(self._runtime_auth_state)
        next_state["anchor"] = self._entry_auth_anchor()
        if refresh_token:
            next_state[CONF_REFRESH_TOKEN] = refresh_token
        if web_cookies:
            next_state[CONF_WEB_COOKIES] = web_cookies
        if family_token:
            next_state[CONF_WEB_FAMILY_TOKEN] = family_token
        elif (
            self.web_api is not None
            and self.web_api.family_context_state == "auth_required"
        ):
            # The web API dropped its antiforgery token because Microsoft
            # rejected it. Keeping the old value here would hand it straight
            # back to set_web_cookies on the next poll, mark the Family context
            # "ready" again and skip the /account probe, so an expired session
            # would never surface. Forget it so the next poll re-probes.
            next_state.pop(CONF_WEB_FAMILY_TOKEN, None)
            next_state.pop(CONF_WEB_FAMILY_REFERER, None)
            # The config entry still holds the token captured at sign-in and
            # _async_load_web_cookies falls back to it. Record the rejection so
            # that fallback is skipped until a fresh token is captured.
            next_state[_FAMILY_TOKEN_REJECTED] = True
        if family_token:
            next_state.pop(_FAMILY_TOKEN_REJECTED, None)
        if family_referer:
            next_state[CONF_WEB_FAMILY_REFERER] = family_referer
        next_state["updated_at"] = dt_util.now().isoformat()

        comparable_old = {k: v for k, v in self._runtime_auth_state.items() if k != "updated_at"}
        comparable_new = {k: v for k, v in next_state.items() if k != "updated_at"}
        if comparable_old == comparable_new:
            return
        self._runtime_auth_state = next_state
        await self._auth_store.async_save(next_state)
        self._has_web_credentials = bool(
            next_state.get(CONF_WEB_COOKIES) or self.entry.data.get(CONF_WEB_COOKIES)
        )
        _LOGGER.debug("Persisted rotated Microsoft Family Safety credentials")

    async def _async_persist_current_auth(self) -> None:
        if self.api is None:
            return
        authenticator = self.api.api.authenticator
        refresh_token = getattr(authenticator, "refresh_token", None)
        cookies = self.web_api.export_web_cookies() if self.web_api else None
        family_token = family_referer = None
        if self.web_api:
            family_token, family_referer = self.web_api.export_family_context()
        await self._async_persist_runtime_auth(
            refresh_token=refresh_token, web_cookies=cookies,
            family_token=family_token, family_referer=family_referer,
        )

    def _runtime_refresh_token(self) -> str:
        return str(
            self._runtime_auth_state.get(CONF_REFRESH_TOKEN)
            or self.entry.data[CONF_REFRESH_TOKEN]
        )

    async def _async_setup_api(self) -> None:
        refresh_token = self._runtime_refresh_token()
        apply_patches(self.hass)
        await self.xbox_family.async_load()
        try:
            self.api = await FamilySafety.create(
                token=refresh_token,
                use_refresh_token=True,
                experimental=True,
            )
            self.web_api = FamilySafetyWebAPI(
                self.api.api.authenticator,
                session=async_get_clientsession(self.hass),
            )
            # Match the browser that created the session: Microsoft answers
            # (and names its pages) in this language.
            lang = self.hass.config.language or "en"
            base = lang.split("-")[0]
            self.web_api.accept_language = (
                f"{lang},{base};q=0.9,en;q=0.8" if base != "en" else f"{lang},en;q=0.9"
            )
            # FamilySafety.create(use_refresh_token=True) itself performs a token
            # refresh. Persist a rotated token immediately so even a restart
            # during the first data poll can recover with the newest credential.
            await self._async_persist_current_auth()
        except HttpException as err:
            text = str(err).lower()
            # A roster resolution 404 is NOT an auth problem. Microsoft returns
            # Family.UnableToFindTargetResource / RosterError when a device still
            # listed in the family roster can no longer be resolved on their side
            # (typically a reset or decommissioned machine). Surfacing this as an
            # auth failure sends the user into a pointless reauth/reinstall loop
            # (issue #38); point them at the real fix instead.
            if self._is_stale_roster_error(text):
                raise UpdateFailed(
                    "Microsoft cannot resolve a device still listed in your "
                    "family roster. Remove the old or reset device at "
                    "https://account.microsoft.com/family, then reload the "
                    "integration."
                ) from err
            if "401" in text or "403" in text or "authentication" in text:
                raise ConfigEntryAuthFailed(ERROR_AUTH_FAILED) from err
            raise UpdateFailed(f"Transient API error: {err}") from err
        except Exception as err:
            text = str(err).lower()
            if self._is_stale_roster_error(text):
                raise UpdateFailed(
                    "Microsoft cannot resolve a device still listed in your "
                    "family roster. Remove the old or reset device at "
                    "https://account.microsoft.com/family, then reload the "
                    "integration."
                ) from err
            if "auth" in text or "token" in text or "401" in text or "403" in text:
                raise ConfigEntryAuthFailed(ERROR_AUTH_FAILED) from err
            raise UpdateFailed(f"API setup error: {err}") from err

    @staticmethod
    def _is_stale_roster_error(text: str) -> bool:
        """Return True for Microsoft's "device not found in roster" 404.

        This is distinct from an authentication failure: the token is valid,
        but the roster references a device Microsoft can no longer resolve.
        Since 2.0.7 the per-member requests tolerate it (see
        _pyfamilysafety_compat._patch_account_roster_tolerance); this branch
        only remains for the roster request itself.
        """
        return is_stale_roster_error(text)

    async def _async_load_web_cookies(self) -> None:
        """Prefer cookies captured natively; retain the Playwright add-on fallback."""
        if self._mobile_only:
            return
        try:
            cookies: list[dict[str, Any]] | None = None
            if self._native_web_auth:
                raw = (
                    self._runtime_auth_state.get(CONF_WEB_COOKIES)
                    or self.entry.data.get(CONF_WEB_COOKIES)
                )
                if isinstance(raw, list):
                    cookies = raw
            else:
                cookies = await self._addon_client.load_cookies()

            if cookies and self.web_api:
                if self._runtime_auth_state.get(_FAMILY_TOKEN_REJECTED):
                    # Microsoft rejected the persisted token. Do not fall back
                    # to the sign-in copy in the config entry: it is the same
                    # value, and handing it back would skip the /account probe
                    # that decides between a rotated token and an expired
                    # session. The web API re-scrapes a token on its own.
                    family_token = family_referer = None
                else:
                    family_token = (
                        self._runtime_auth_state.get(CONF_WEB_FAMILY_TOKEN)
                        or self.entry.data.get(CONF_WEB_FAMILY_TOKEN)
                    )
                    family_referer = (
                        self._runtime_auth_state.get(CONF_WEB_FAMILY_REFERER)
                        or self.entry.data.get(CONF_WEB_FAMILY_REFERER)
                    )
                self.web_api.set_web_cookies(
                    cookies,
                    family_token=str(family_token) if family_token else None,
                    family_referer=str(family_referer) if family_referer else None,
                )
                _LOGGER.debug(
                    "Loaded Microsoft Family web credentials: cookies=%d "
                    "family_token_present=%s family_referer_path=%s",
                    len(cookies), bool(family_token),
                    URL(str(family_referer)).path if family_referer else None,
                )
                self._web_cookies_loaded = True
            else:
                if self._web_cookies_loaded:
                    await self._create_auth_notification()
                self._web_cookies_loaded = False
                if not cookies and not self.entry.data.get(CONF_AUTH_URL):
                    self._request_reauth("web_credentials_missing")
        except Exception as err:
            self._web_cookies_loaded = False
            _LOGGER.debug("Could not load web cookies: %s", err)
            if not self.entry.data.get(CONF_AUTH_URL):
                self._request_reauth("web_credentials_missing")

    def account_data(self, account_id: Any) -> dict[str, Any] | None:
        """A child's polled data, whether the id comes as int or str.

        The roster keys accounts by Microsoft's id as the mobile API gives it
        (an int), while service calls and the web API pass it as a str.
        """
        accounts = (self.data or {}).get("accounts") or {}
        if account_id in accounts:
            return accounts[account_id]
        key = self._policy_key(account_id)
        return next(
            (value for acc_id, value in accounts.items() if self._policy_key(acc_id) == key),
            None,
        )

    def get_account(self, account_id: Any) -> Account | None:
        """Get the raw pyfamilysafety Account object, whether the id is int or str."""
        account = self._accounts.get(account_id)
        if account is not None:
            return account
        key = self._policy_key(account_id)
        return next(
            (acc for acc_id, acc in self._accounts.items() if self._policy_key(acc_id) == key),
            None,
        )

    def get_device(self, device_id: str) -> Device | None:
        """Get the raw pyfamilysafety Device object."""
        return self._devices.get(device_id)

    def get_application(self, account_id: str, app_id: str) -> Application | None:
        """Get a raw pyfamilysafety Application object."""
        account = self.get_account(account_id)
        if account is None:
            return None
        try:
            return account.get_application(app_id)
        except (IndexError, ValueError):
            return None

    async def async_block_app(self, account_id: str, app_id: str) -> None:
        """Block an application."""
        await self._async_set_app_blocked(account_id, app_id, True)

    async def async_unblock_app(self, account_id: str, app_id: str) -> None:
        """Unblock an application."""
        await self._async_set_app_blocked(account_id, app_id, False)

    async def _async_set_app_blocked(
        self, account_id: str, app_id: str, blocked: bool
    ) -> None:
        """Block or unblock one app, failing as a HomeAssistantError.

        A raw pyfamilysafety HttpException escaped the switch and service
        before, so ``continue_on_error`` in an automation could not skip one
        rejected app (#54).
        """
        app = self.get_application(account_id, app_id)
        if app is None:
            raise HomeAssistantError(
                f"Application {app_id} not found for account {account_id}"
            )
        try:
            await (app.block_app() if blocked else app.unblock_app())
        except HttpException as err:
            raise HomeAssistantError(
                f"Microsoft rejected {'blocking' if blocked else 'unblocking'} "
                f"{app_id}: {err}"
            ) from err
        await self.async_request_refresh()

    #: Platforms whose lock state is read from the Family web API rather than
    #: from the mobile aggregator. The aggregator still accepts and stores an
    #: Xbox override, and reports it back, but the console no longer honours
    #: it, so its Xbox value is not evidence of anything. Windows keeps its
    #: existing behaviour.
    _WEB_LOCK_STATE_PLATFORMS = frozenset({"xbox"})

    def _merge_blocked_platforms(self, account: Account) -> list[str]:
        """Report which platforms are locked right now.

        account.blocked_platforms comes from the pyfamilysafety mobile
        aggregator. For Xbox that value is ignored -- a stale entry left there
        by the old mobile-API lock would make the switch read "locked" and stop
        anything from locking the console properly. Xbox is reported from the
        override this integration set (and, once the web data is in, from the
        Xbox policy Microsoft returns; see _apply_xbox_lock_state).
        """
        now = dt_util.utcnow()
        for key, until in list(self._platform_override_until.items()):
            if until <= now:
                self._platform_override_until.pop(key, None)

        blocked = [
            platform
            for platform in (str(p) for p in (account.blocked_platforms or []))
            if platform.lower() not in self._WEB_LOCK_STATE_PLATFORMS
        ]
        for (acct, platform), until in self._platform_override_until.items():
            if (
                platform in self._WEB_LOCK_STATE_PLATFORMS
                and acct == str(account.user_id)
                and until > now
            ):
                pretty = platform.capitalize()
                if pretty not in blocked:
                    blocked.append(pretty)
        return blocked

    def _apply_xbox_lock_state(
        self,
        account_id: str,
        account_data: dict[str, Any],
        policy: dict | None,
        *,
        no_xbox: bool = False,
    ) -> None:
        """Take the Xbox lock state from the Xbox policy Microsoft returns.

        That policy carries the override the console actually enforces, so the
        lock survives a Home Assistant restart and reflects a block or release
        made in the Family Safety app.

        Only an answer that can be read settles the state: a "cancel", a
        blockUntil with a readable expiry, or a recognisable policy carrying no
        override at all. A failed read, an unknown override type, or a
        blockUntil whose validUntil is missing or unreadable changes nothing --
        "unknown" never degrades to "not blocked".

        One exception: no_xbox (Microsoft answered 403/404, i.e. no Xbox data
        for this child) settles a child whose lock state has never been read as
        not blocked, so an account without an Xbox shows the lock switch as off
        rather than unknown. A child whose state is already known keeps it: a
        one-off 403 must not turn a real lock into "off".
        """
        key = (str(account_id), "xbox")
        if isinstance(policy, dict):
            data = policy.get("data") if isinstance(policy.get("data"), dict) else policy
            override = data.get("screenTimeOverride")
            override_type = (
                str(override.get("screenTimeOverrideType") or "").lower()
                if isinstance(override, dict)
                else None
            )
            if override_type == "blockuntil":
                block_until = FamilySafetyWebAPI.parse_block_until(
                    {"screenTimeOverrides": [override]}
                )
                if block_until is None:
                    _LOGGER.debug(
                        "Xbox policy for %s has a blockUntil override without a "
                        "readable validUntil; keeping the current lock state",
                        account_id,
                    )
                else:
                    if block_until > dt_util.utcnow():
                        self._platform_override_until[key] = block_until
                    else:
                        self._platform_override_until.pop(key, None)
                    self._xbox_lock_known.add(key[0])
            elif override_type in ("cancel", "allowuntil") or (
                override is None and "dailyRestrictions" in data
            ):
                # "allowUntil" (extra time granted) explicitly allows the
                # console, so it is a readable "not blocked", not an unknown.
                # Microsoft answered and is not blocking: a stale local entry goes.
                self._platform_override_until.pop(key, None)
                self._xbox_lock_known.add(key[0])
            elif override_type is not None:
                _LOGGER.debug(
                    "Xbox policy for %s has an unrecognised override type %r; "
                    "keeping the current lock state",
                    account_id, override_type,
                )
        elif no_xbox and key[0] not in self._xbox_lock_known:
            self._platform_override_until.pop(key, None)
            self._xbox_lock_known.add(key[0])
        blocked = [p for p in (account_data.get("blocked_platforms") or []) if p != "Xbox"]
        if key in self._platform_override_until:
            blocked.append("Xbox")
        account_data["blocked_platforms"] = blocked
        account_data["xbox_lock_known"] = key[0] in self._xbox_lock_known

    async def async_lock_platform(
        self, account_id: str, platform: str, valid_until: datetime | None = None
    ) -> None:
        account = self.get_account(account_id)
        if account is None:
            raise ValueError(f"Account {account_id} not found")
        until = valid_until or (dt_util.now() + timedelta(hours=24))

        if platform.lower() == "xbox" and (xuid := await self._xapprove_xuid_for(account_id)):
            try:
                result = await self.xbox_family.async_create_override(
                    xuid, OVERRIDE_BLOCK, until
                )
            except XboxFamilyError as err:
                _LOGGER.warning("xapprove Xbox lock failed, trying the web path: %s", err)
            else:
                if any(
                    o.get("overrideType") == OVERRIDE_BLOCK
                    for o in result.get("overrides") or []
                ):
                    self._platform_override_until[(str(account_id), "xbox")] = dt_util.as_utc(until)
                    self._xbox_lock_known.add(str(account_id))
                    await self.async_request_refresh()
                    return
                _LOGGER.warning(
                    "xapprove accepted the Xbox lock but reports no block; trying the web path"
                )

        # Windows AND Xbox both have a working web override endpoint; they are
        # separate paths, not one endpoint parameterised by platform (see
        # FamilySafetyWebAPI.supports_platform_override).
        #
        # Xbox previously fell straight through to the pyfamilysafety mobile
        # aggregator below.  That aggregator still ACCEPTS and STORES an Xbox
        # override -- and reports it back via blocked_platforms -- but Microsoft
        # stopped honouring it on the console, the same regression that forced
        # Xbox *usage* onto the web report-v3 endpoint around 2026-08-20.  The
        # result was a lock that read as applied and did nothing.
        if (
            FamilySafetyWebAPI.supports_platform_override(platform)
            and self._native_web_auth
            and self.web_api is not None
            and self.web_api.has_web_cookies
        ):
            until_utc = dt_util.as_utc(until)
            date_time = until_utc.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            try:
                await self.web_api.set_platform_time_override(
                    account_id, platform, "blockUntil", date_time
                )
            except Exception as err:
                if platform.lower() in self._WEB_LOCK_STATE_PLATFORMS:
                    raise HomeAssistantError(
                        f"Could not lock {platform}: the Family web request failed ({err})"
                    ) from err
                _LOGGER.debug(
                    "%s web override unavailable; falling back to mobile API: %s",
                    platform, err,
                )
            else:
                self._platform_override_until[(str(account_id), platform.lower())] = until_utc
                if platform.lower() in self._WEB_LOCK_STATE_PLATFORMS:
                    self._xbox_lock_known.add(str(account_id))
                await self.async_request_refresh()
                return

        # The mobile API still accepts an Xbox override but the console ignores
        # it, so reporting success here would tell automations the console is
        # locked when it is not.
        if platform.lower() in self._WEB_LOCK_STATE_PLATFORMS:
            raise HomeAssistantError(
                f"Cannot lock {platform}: it can only be locked through the Family "
                "web session, which is not available. Re-authenticate the integration."
            )
        # pyfamilysafety formats the value with a literal "Z": give it real UTC.
        await self._override_platform_mobile(
            account, account_id, platform, OverrideType.UNTIL,
            dt_util.as_utc(until).replace(tzinfo=None),
        )

    async def _override_platform_mobile(
        self,
        account: Account,
        account_id: str,
        platform: str,
        override: OverrideType,
        until: datetime | None = None,
    ) -> None:
        """Send a mobile override; judge a Microsoft 500 by the state it leaves."""
        try:
            await account.override_device(
                OverrideTarget.from_pretty(platform), override, until
            )
        except AggregatorException as err:
            # Seen on 2026-11-05: HTTP 500 "aggregator error", override applied anyway.
            await self.async_refresh()
            blocked = (self.account_data(account_id) or {}).get("blocked_platforms", [])
            if (platform in blocked) != (override == OverrideType.UNTIL):
                raise HomeAssistantError(
                    f"Microsoft answered an error and {platform} has not changed yet; "
                    "check its state after the next update before trying again."
                ) from err
            _LOGGER.info("Microsoft answered 500 but the %s override was applied", platform)
            return
        await self.async_request_refresh()

    async def async_unlock_platform(self, account_id: str, platform: str) -> None:
        account = self.get_account(account_id)
        if account is None:
            raise ValueError(f"Account {account_id} not found")

        if platform.lower() == "xbox" and (xuid := await self._xapprove_xuid_for(account_id)):
            # The Xbox app's "revoke" (type 100) is the only release captured so
            # far; the read-back decides whether it lifted a block too.
            try:
                result = await self.xbox_family.async_create_override(
                    xuid, OVERRIDE_REVOKE, dt_util.utcnow() + timedelta(minutes=10)
                )
            except XboxFamilyError as err:
                _LOGGER.warning("xapprove Xbox unlock failed, trying the web path: %s", err)
            else:
                if not any(
                    o.get("overrideType") == OVERRIDE_BLOCK
                    for o in result.get("overrides") or []
                ):
                    self._platform_override_until.pop((str(account_id), "xbox"), None)
                    self._xbox_lock_known.add(str(account_id))
                    await self.async_request_refresh()
                    return
                _LOGGER.warning(
                    "xapprove revoke left the Xbox block in place; trying the web path"
                )

        if (
            FamilySafetyWebAPI.supports_platform_override(platform)
            and self._native_web_auth
            and self.web_api is not None
            and self.web_api.has_web_cookies
        ):
            until = self._platform_override_until.get(
                (str(account_id), platform.lower()), dt_util.utcnow()
            )
            date_time = dt_util.as_utc(until).isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            )
            try:
                await self.web_api.set_platform_time_override(
                    account_id, platform, "cancel", date_time
                )
            except Exception as err:
                if platform.lower() in self._WEB_LOCK_STATE_PLATFORMS:
                    raise HomeAssistantError(
                        f"Could not unlock {platform}: the Family web request failed ({err})"
                    ) from err
                _LOGGER.debug(
                    "%s web cancel unavailable; falling back to mobile API: %s",
                    platform, err,
                )
            else:
                self._platform_override_until.pop((str(account_id), platform.lower()), None)
                if platform.lower() in self._WEB_LOCK_STATE_PLATFORMS:
                    self._xbox_lock_known.add(str(account_id))
                await self.async_request_refresh()
                return

        # As for the lock: a mobile-API Xbox cancel does nothing on the console,
        # so fail and keep the lock state as it is rather than report "unlocked".
        if platform.lower() in self._WEB_LOCK_STATE_PLATFORMS:
            raise HomeAssistantError(
                f"Cannot unlock {platform}: it can only be unlocked through the Family "
                "web session, which is not available. Re-authenticate the integration."
            )
        await self._override_platform_mobile(
            account, account_id, platform, OverrideType.CANCEL
        )
        self._platform_override_until.pop((str(account_id), platform.lower()), None)

    async def async_approve_request(
        self, request_id: str, extension_time: int = 3600
    ) -> bool:
        """Approve a pending screen time request.

        ``extension_time`` is expressed in seconds.

        Workaround for issue #20: pyfamilysafety 1.1.2 converts the value with
        ``extension_time * 100`` while commenting "seconds to ms" — but seconds
        to milliseconds is ``* 1000``. The library therefore grants only a tenth
        of the requested time (e.g. 60 min -> 6 min). We pre-multiply by 10 so
        the library's faulty ``* 100`` yields the correct milliseconds
        (seconds * 10 * 100 == seconds * 1000).
        """
        if self.api is None:
            return False
        try:
            return await self.api.approve_pending_request(
                request_id, extension_time * 10
            )
        except ValueError as err:
            await self._async_request_gone("approve", request_id, err)
        return False

    async def async_deny_request(self, request_id: str) -> bool:
        """Deny a pending screen time request."""
        if self.api is None:
            return False
        try:
            return await self.api.deny_pending_request(request_id)
        except ValueError as err:
            await self._async_request_gone("deny", request_id, err)
        return False

    async def _async_request_gone(
        self, action: str, request_id: str, err: ValueError
    ) -> None:
        """Turn pyfamilysafety's ``ValueError`` into a clear user-facing error.

        The library looks the id up in the list it fetched on the last poll and
        raises ``ValueError("Pending request not found")`` when it is gone. That
        happens when the request was already approved or denied elsewhere
        (typically from the Family Safety app) or expired, while the dashboard
        still showed it. Nothing was sent to Microsoft. Refresh so the stale
        request disappears from the entities, then explain what happened.
        """
        _LOGGER.info(
            "Cannot %s request %s: it is no longer pending (%s)",
            action, request_id, err,
        )
        await self.async_request_refresh()
        raise HomeAssistantError(
            f"Request {request_id} is no longer pending: it was probably already "
            "approved or denied from the Family Safety app, or it expired. "
            "The pending requests have been refreshed."
        ) from err

    async def _fetch_screentime_policy(
        self, account_id: str, *, require_child_match: bool = False
    ) -> dict | None:
        """Read a child's screen-time policy.

        Pass ``require_child_match`` when the result becomes a restore point or
        decides a destructive rewrite: the web payload may contain exactly one
        policy belonging to a different child, and silently accepting it would
        save the wrong schedule (issue #23).
        """
        if self._native_web_auth or self._mobile_only:
            if self.web_api is None:
                return None
            return await self.web_api.get_screentime_policy(
                account_id, require_child_match=require_child_match
            )
        screentime = await self._addon_client.fetch_screentime(account_id)
        if screentime is None and self.web_api is not None:
            screentime = await self.web_api.get_screentime_policy(
                account_id, require_child_match=require_child_match
            )
        return screentime

    async def _set_screentime_allowance(
        self, child_id: str, day_of_week: int, hours: int, minutes: int
    ) -> bool:
        if self.web_api is not None:
            try:
                return await self.web_api.set_mobile_screentime_allowance(
                    child_id, day_of_week, hours, minutes
                )
            except Exception as err:  # noqa: BLE001 - fall back to the web path
                _LOGGER.warning("Mobile screen-time allowance update failed, trying web: %s", err)
        if self._native_web_auth or self._mobile_only:
            if self.web_api is None:
                raise RuntimeError("Web API not initialized")
            return await self.web_api.set_screentime_allowance(
                child_id, day_of_week, hours, minutes
            )
        return await self._addon_client.set_screentime_allowance(
            child_id, day_of_week, hours, minutes
        )

    async def _set_screentime_intervals(
        self, child_id: str, day_of_week: int, allowed_intervals: list[bool]
    ) -> bool:
        if self.web_api is not None:
            try:
                return await self.web_api.set_mobile_screentime_intervals(
                    child_id, day_of_week, allowed_intervals
                )
            except Exception as err:  # noqa: BLE001 - fall back to the web path
                _LOGGER.warning("Mobile screen-time interval update failed, trying web: %s", err)
        if self._native_web_auth or self._mobile_only:
            if self.web_api is None:
                raise RuntimeError("Web API not initialized")
            return await self.web_api.set_screentime_intervals(
                child_id, day_of_week, allowed_intervals
            )
        return await self._addon_client.set_screentime_intervals(
            child_id, day_of_week, allowed_intervals
        )

    async def async_set_screentime_limit(
        self,
        child_id: str,
        day_of_week: int,
        hours: int,
        minutes: int,
        platform: str = "Windows",
    ) -> None:
        if platform == "Xbox":
            await self.async_set_xbox_screentime_limit(
                child_id, day_of_week, hours, minutes, require_xapprove=True
            )
            return
        if platform != "Windows":
            raise ValueError("platform must be Windows or Xbox")
        if not 0 <= day_of_week <= 6:
            raise ValueError("day_of_week must be between 0 and 6")
        if hours < 0 or minutes < 0 or minutes > 59 or hours * 60 + minutes > 1440:
            raise ValueError("screen-time allowance must be between 0 and 24 hours")
        await self._set_screentime_allowance(child_id, day_of_week, hours, minutes)
        self._update_cached_day(
            child_id, day_of_week, allowance=f"{hours:02d}:{minutes:02d}:00"
        )
        await self.async_request_refresh()

    async def async_set_screentime_intervals(
        self,
        child_id: str,
        day_of_week: int,
        start_hour: int,
        start_minute: int,
        end_hour: int,
        end_minute: int,
        platform: str = "Windows",
    ) -> None:
        if platform == "Xbox":
            await self.async_set_xbox_allowed_hours(
                child_id, day_of_week, start_hour, start_minute, end_hour, end_minute
            )
            return
        if platform != "Windows":
            raise ValueError("platform must be Windows or Xbox")
        if not 0 <= day_of_week <= 6:
            raise ValueError("day_of_week must be between 0 and 6")
        start_total = start_hour * 60 + start_minute
        end_total = end_hour * 60 + end_minute
        if not 0 <= start_total < end_total <= 1440:
            raise ValueError("screen-time interval end must be later than start")
        slots = _range_to_slots(start_hour, start_minute, end_hour, end_minute)
        await self._set_screentime_intervals(child_id, day_of_week, slots)
        self._update_cached_day(child_id, day_of_week, slots=slots)
        await self.async_request_refresh()

    def _update_cached_day(
        self,
        child_id: str,
        day_of_week: int,
        *,
        allowance: str | None = None,
        slots: list[bool] | None = None,
    ) -> None:
        """Mirror a write Microsoft accepted into the cached schedule.

        The refresh requested after a write is debounced. Until it runs, the
        number and time entities read the previous poll: setting a day's end
        right after its start would send the old start back to Microsoft and
        undo the first write (seen with a script setting both in a row).
        """
        account = self.account_data(child_id)
        policy = account.get("screentime_policy") if isinstance(account, dict) else None
        daily = policy.get("dailyRestrictions") if isinstance(policy, dict) else None
        if not isinstance(daily, dict):
            return
        day = daily.get(DAY_KEYS[day_of_week])
        if not isinstance(day, dict):
            return
        if allowance is not None:
            day["allowance"] = allowance
        if slots is not None:
            day["timeline"] = list(slots)
            day["allowedIntervals"] = list(slots)
            day.pop("allottedIntervals", None)
        self.async_update_listeners()

    async def async_set_app_time_limit(
        self,
        child_id: str,
        app_id: str,
        display_name: str,
        platform: str,
        allowance: str,
        start_time: str = "07:00:00",
        end_time: str = "22:00:00",
    ) -> None:
        if not self.web_api:
            raise RuntimeError("Web API not initialized")
        await self.web_api.set_app_time_limit(
            child_id, app_id, display_name, platform, allowance, start_time, end_time
        )
        await self.async_request_refresh()

    async def async_remove_app_time_limit(
        self, child_id: str, app_id: str, display_name: str, platform: str
    ) -> None:
        if not self.web_api:
            raise RuntimeError("Web API not initialized")
        await self.web_api.remove_app_time_limit(
            child_id, app_id, display_name, platform
        )
        await self.async_request_refresh()

    # --- Xbox extra time (add_time_bonus / cancel_time_bonus) -----------------
    #: How far the extra time Microsoft reads back may be from what was asked.
    _BONUS_TOLERANCE = timedelta(minutes=2)

    @staticmethod
    def _xapprove_until(value: Any) -> datetime | None:
        """An xapprove validUntil as UTC; one without an offset is UTC too."""
        parsed = dt_util.parse_datetime(str(value or ""))
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_util.UTC)
        return dt_util.as_utc(parsed)

    @classmethod
    def _xapprove_override_of(
        cls, answer: dict[str, Any] | None, override_type: int
    ) -> dict | None:
        """The override of override_type in force in an xapprove answer, if any.

        Entries whose validUntil has passed are ignored; among several, the
        one ending last is returned (one without a readable end counts as in
        force and ending last).
        """
        now = dt_util.utcnow()
        best: dict | None = None
        best_until: datetime | None = None
        for override in (answer or {}).get("overrides") or []:
            if not isinstance(override, dict) or override.get("overrideType") != override_type:
                continue
            until = cls._xapprove_until(override.get("validUntil"))
            if until is not None and until <= now:
                continue
            if best is None or (
                best_until is not None and (until is None or until > best_until)
            ):
                best, best_until = override, until
        return best

    async def _async_bonus_xuid(self, account_id: str, platform: str) -> str:
        """The child's Xbox user id for extra time, or a clear error.

        Extra time goes through the Xbox Family Settings service only; Windows
        has no request for it that could be confirmed.
        """
        if str(platform).lower() != "xbox":
            raise HomeAssistantError("Extra time can only be given on the Xbox.")
        if not any(str(key) == str(account_id) for key in self._accounts):
            raise ValueError(f"Account {account_id} not found")
        xuid = await self._xapprove_xuid_for(account_id)
        if xuid is None:
            raise HomeAssistantError(
                "Xbox extra time needs Xbox among the monitored platforms and the Xbox "
                "Family Settings sign-in linked (Connection sensor: xbox_family), and "
                "the child must be on the Xbox family."
            )
        return xuid

    async def _async_xbox_overrides(self, xuid: str, what: str) -> dict[str, Any]:
        try:
            return await self.xbox_family.async_get_overrides(xuid)
        except XboxFamilyError as err:
            raise HomeAssistantError(
                f"Could not read the Xbox overrides, so {what}: {err}"
            ) from err

    async def async_add_time_bonus(
        self,
        account_id: str,
        platform: str,
        until: datetime,
        replace_lock: bool = False,
    ) -> dict[str, Any]:
        """Give a child extra time on the Xbox until ``until``.

        The Xbox Family Settings app's own "add time" (xapprove override 1).
        Microsoft keeps one Xbox override, so extra time replaces a lock: while
        one is in force (ours or a parent's) this is refused unless
        ``replace_lock``. It succeeds only when Microsoft's answer lists the
        extra time ending when asked and no lock left in force.
        """
        xuid = await self._async_bonus_xuid(account_id, platform)
        until = dt_util.as_utc(until)
        current = await self._async_xbox_overrides(xuid, "no extra time was given")
        lock = self._xapprove_override_of(current, OVERRIDE_BLOCK)
        if lock is not None and not replace_lock:
            raise HomeAssistantError(
                f"The Xbox is locked (until {lock.get('validUntil')}); extra time would "
                "lift that lock. Pass replace_lock: true to replace it."
            )
        try:
            answer = await self.xbox_family.async_create_override(xuid, OVERRIDE_ALLOW, until)
        except XboxFamilyError as err:
            raise HomeAssistantError(f"Could not give Xbox extra time: {err}") from err
        allow = self._xapprove_override_of(answer, OVERRIDE_ALLOW)
        valid_until = self._xapprove_until((allow or {}).get("validUntil"))
        if valid_until is None or abs(valid_until - until) > self._BONUS_TOLERANCE:
            _LOGGER.debug("Xbox extra time not confirmed by xapprove: answer %s", answer)
            raise HomeAssistantError(
                "Microsoft did not confirm the Xbox extra time "
                f"(asked until {until.isoformat()}, answer: {allow or 'no extra time'})."
            )
        key = (str(account_id), "xbox")
        self._xbox_lock_known.add(key[0])
        if self._xapprove_override_of(answer, OVERRIDE_BLOCK) is not None:
            await self.async_request_refresh()
            raise HomeAssistantError(
                "Microsoft recorded the Xbox extra time but the Xbox is still locked; "
                "unlock it with unlock_platform first."
            )
        # The extra time replaced any lock.
        self._platform_override_until.pop(key, None)
        await self.async_request_refresh()
        return {
            "added": True,
            "until": dt_util.as_local(valid_until).isoformat(),
            "replaced_lock": lock is not None,
        }

    async def async_cancel_time_bonus(self, account_id: str, platform: str) -> dict[str, Any]:
        """Cancel the Xbox extra time in force, and nothing else.

        The Xbox Family Settings app's revoke (override 100) is sent only when
        extra time is in force and no lock is: the revoke would lift a lock too,
        and a lock (from this integration or from a parent) is never touched
        here. With a lock in force this fails; with no extra time in force it
        does nothing and says so.
        """
        xuid = await self._async_bonus_xuid(account_id, platform)
        current = await self._async_xbox_overrides(xuid, "nothing was cancelled")
        if self._xapprove_override_of(current, OVERRIDE_BLOCK) is not None:
            raise HomeAssistantError(
                "The Xbox is locked, so nothing was cancelled: the revoke would lift "
                "the lock as well. Use unlock_platform to end the lock."
            )
        if self._xapprove_override_of(current, OVERRIDE_ALLOW) is None:
            _LOGGER.info("No Xbox extra time in force for account %s; nothing to cancel", account_id)
            return {"cancelled": False, "reason": "no_extra_time"}
        try:
            answer = await self.xbox_family.async_create_override(
                xuid, OVERRIDE_REVOKE, dt_util.utcnow() + timedelta(minutes=10)
            )
        except XboxFamilyError as err:
            raise HomeAssistantError(f"Could not cancel the Xbox extra time: {err}") from err
        await self.async_request_refresh()
        if self._xapprove_override_of(answer, OVERRIDE_ALLOW) is not None:
            raise HomeAssistantError("Microsoft left the Xbox extra time in place.")
        lock = self._xapprove_override_of(answer, OVERRIDE_BLOCK)
        if lock is not None:
            _LOGGER.warning(
                "After cancelling the Xbox extra time of account %s, Microsoft lists a "
                "lock until %s",
                account_id, lock.get("validUntil"),
            )
        return {"cancelled": True, "lock_in_force": lock is not None}

    # --- Xbox Family Settings API (xapprove) ---------------------------------
    @property
    def _xapprove_active(self) -> bool:
        return self.xbox_features and self.xbox_family.linked

    async def _async_xbox_xuid(self, account: Account) -> str | None:
        """The child's Xbox user id, matched by name on the Xbox family roster."""
        key = str(account.user_id)
        if key in self._xbox_xuids:
            return self._xbox_xuids[key]
        xuid = await self.xbox_family.async_child_xuid(
            getattr(account, "first_name", None), getattr(account, "surname", None)
        )
        if xuid:
            self._xbox_xuids[key] = xuid
            _LOGGER.info("Xbox Family Settings: a child was matched to their Xbox account")
            _LOGGER.debug("Xbox Family Settings: account %s is Xbox user %s", key, xuid)
        else:
            self._xbox_unmatched.add(key)
        return xuid

    async def _xapprove_xuid_for(self, child_id: str) -> str | None:
        """xuid for a write, or None when xapprove is not in use for this child."""
        if not self._xapprove_active:
            return None
        # Roster ids are int, entity and service ids may be str: compare as str.
        account = next(
            (a for key, a in self._accounts.items() if str(key) == str(child_id)), None
        )
        if account is None:
            _LOGGER.warning(
                "Xbox Family Settings: no roster account %s; using the previous path",
                child_id,
            )
            return None
        try:
            return await self._async_xbox_xuid(account)
        except XboxFamilyError as err:
            _LOGGER.warning("Xbox Family Settings lookup failed: %s", err)
            return None

    async def _xapprove_xuid_required(self, child_id: str) -> str:
        """xuid for a write that must go through xapprove, or a clear refusal.

        Used where no other path reaches the console: the Xbox schedule on the
        Family Safety mobile API accepts writes the console does not enforce.
        """
        if not self.xbox_features:
            raise HomeAssistantError(
                "Xbox is not enabled for this Microsoft Family Safety entry: tick Xbox "
                "in the integration options and link the Xbox Family Settings sign-in"
            )
        if not self.xbox_family.linked:
            raise HomeAssistantError(
                "The Xbox Family Settings sign-in is not linked: link it from the "
                "integration options"
            )
        if not self.xbox_family.usable:
            raise HomeAssistantError(
                "Microsoft refused the Xbox Family Settings sign-in: link it again "
                "from the integration options"
            )
        account = next(
            (a for key, a in self._accounts.items() if str(key) == str(child_id)), None
        )
        if account is None:
            raise HomeAssistantError(f"No child account {child_id} in this family")
        try:
            xuid = await self._async_xbox_xuid(account)
        except XboxFamilyAuthError as err:
            raise HomeAssistantError(
                "Microsoft refused the Xbox Family Settings sign-in: link it again "
                f"from the integration options ({err})"
            ) from err
        except XboxFamilyError as err:
            raise HomeAssistantError(f"Could not look up the child's Xbox account: {err}") from err
        if xuid is None:
            raise HomeAssistantError(
                f"Child account {child_id} could not be matched to an Xbox account: the "
                "first and last name must match exactly one child in the Xbox family"
            )
        return xuid

    def _xbox_web_gates(self, xapprove: dict[str, Any] | None) -> tuple[bool, bool]:
        """Whether the web Xbox policy and the web usage report may be read.

        Only while the Family session is healthy and not backing off. With
        xapprove answering, the policy is not read from the web; the usage is,
        when only xapprove's usage report failed.
        """
        web_gate = bool(
            self.web_api is not None
            and self.web_api.has_web_cookies
            and self.web_api.family_context_state == "ready"
            and float(getattr(self.web_api, "_family_web_backoff_until", 0.0) or 0.0)
            <= time.monotonic()
        )
        return (
            web_gate and xapprove is None,
            web_gate and (xapprove is None or xapprove.get("usage") is None),
        )

    async def _async_fetch_xapprove(
        self, account: Account, account_data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Read the Xbox schedule, override and usage from xapprove.

        Puts the schedule on ``xbox_screentime_policy`` (what the Xbox numbers
        and switch read) and returns the usage and the override in the web
        policy shape, or None to let the web path run as before.
        """
        if not self._xapprove_active:
            return None
        try:
            xuid = await self._async_xbox_xuid(account)
            if xuid is None:
                return None
            schedule = await self.xbox_family.async_get_schedule(xuid)
            overrides = await self.xbox_family.async_get_overrides(xuid)
            try:
                usage = usage_today(
                    await self.xbox_family.async_get_usage(xuid),
                    dt_util.now().date().isoformat(),
                )
            except XboxFamilyError as err:
                _LOGGER.debug("Could not read Xbox usage from xapprove: %s", err)
                usage = None
        except XboxFamilyAuthError as err:
            if not self._xbox_auth_warned:
                self._xbox_auth_warned = True
                _LOGGER.warning(
                    "Xbox Family Settings sign-in no longer works (%s); Xbox falls back "
                    "to the web session. Link it again from the integration options.",
                    err,
                )
            return None
        except XboxFamilyError as err:
            _LOGGER.debug("xapprove read failed, using the web path this poll: %s", err)
            return None
        self._xbox_auth_warned = False
        account_data["xbox_screentime_policy"] = schedule_to_policy(schedule)
        account_data["xbox_source"] = "xapprove"
        return {"usage": usage, "lock_policy": overrides_to_policy(overrides)}

    def async_xbox_linked(self) -> None:
        """A manual link just stored a new token in this instance: use it now."""
        self._xbox_xuids.clear()
        self._xbox_unmatched.clear()
        self._xbox_auth_warned = False
        self.hass.async_create_task(self.async_request_refresh())

    async def _async_try_silent_xbox_link(self) -> None:
        """Link the Xbox Family Settings client from the web sign-in's cookies.

        Same idea as the mobile token after a web sign-in: with live Microsoft
        account cookies, oauth20_authorize.srf can answer with a single
        redirect carrying the code. Tried at most every 6 h; a failure only
        means the manual link in the options is needed.
        """
        if (
            not self.xbox_features
            or self.xbox_family.linked
            or self.web_api is None
            or not self.web_api.has_web_cookies
            or time.monotonic() < self._xbox_link_retry_at
        ):
            return
        self._xbox_link_retry_at = time.monotonic() + 6 * 3600
        from .auth.native_proxy import async_fetch_mobile_oauth_redirect

        url, verifier = build_authorize_url()
        try:
            result = await async_fetch_mobile_oauth_redirect(
                self.hass, url, self.web_api.export_web_cookies()
            )
            if result is None:
                _LOGGER.info(
                    "Xbox Family Settings could not be linked silently; link it from "
                    "the integration options"
                )
                return
            await self.xbox_family.async_link_from_redirect(result[0], verifier, "silent")
        except Exception as err:  # noqa: BLE001 - optional, retried later
            _LOGGER.info("Silent Xbox Family Settings link failed: %r", err)

    def _set_xbox_policy_field(self, child_id: str, update) -> None:
        account = self.account_data(child_id)
        policy = account.get("xbox_screentime_policy") if isinstance(account, dict) else None
        if isinstance(policy, dict):
            update(policy)
            self.async_update_listeners()

    async def async_set_xbox_screentime_limit(
        self,
        child_id: str,
        day_of_week: int,
        hours: int,
        minutes: int,
        *,
        require_xapprove: bool = False,
    ) -> None:
        """Set one day of the Xbox schedule (the one the console enforces).

        With ``require_xapprove`` the write is refused instead of falling back
        to the mobile Xbox schedule when xapprove is not available.
        """
        if not 0 <= day_of_week <= 6:
            raise ValueError("day_of_week must be between 0 and 6")
        if hours < 0 or minutes < 0 or minutes > 59 or hours * 60 + minutes > 1440:
            raise ValueError("screen-time allowance must be between 0 and 24 hours")
        if require_xapprove:
            xuid: str | None = await self._xapprove_xuid_required(child_id)
        else:
            if not self.web_api:
                raise RuntimeError("Web API not initialized")
            xuid = await self._xapprove_xuid_for(child_id)
        if xuid is not None:
            try:
                await self.xbox_family.async_set_day(
                    xuid, day_of_week, minutes=hours * 60 + minutes
                )
            except XboxFamilyError as err:
                raise HomeAssistantError(f"Could not set the Xbox limit: {err}") from err
        else:
            await self.web_api.set_mobile_xbox_screentime_allowance(
                child_id, day_of_week, hours, minutes
            )
        day = DAY_KEYS[day_of_week]

        def _update(policy: dict) -> None:
            entry = (policy.get("dailyRestrictions") or {}).get(day)
            if isinstance(entry, dict):
                entry["allowance"] = f"{hours:02d}:{minutes:02d}:00"

        self._set_xbox_policy_field(child_id, _update)
        await self.async_request_refresh()

    async def async_set_xbox_allowed_hours(
        self,
        child_id: str,
        day_of_week: int,
        start_hour: int,
        start_minute: int,
        end_hour: int,
        end_minute: int,
    ) -> None:
        """Set one day's Xbox allowed hours, keeping the day's allowance.

        Only through xapprove: the console does not enforce the Xbox schedule
        of the Family Safety mobile API.
        """
        if not 0 <= day_of_week <= 6:
            raise ValueError("day_of_week must be between 0 and 6")
        if start_minute not in (0, 30) or end_minute not in (0, 30):
            raise ValueError("Xbox allowed hours start and end on the hour or half hour")
        start_total = start_hour * 60 + start_minute
        end_total = end_hour * 60 + end_minute
        if not 0 <= start_total < end_total <= 1440:
            raise ValueError(
                "Xbox allowed hours must end later the same day than they start "
                "(24:00 at the latest)"
            )
        xuid = await self._xapprove_xuid_required(child_id)
        try:
            await self.xbox_family.async_set_day(
                xuid, day_of_week, intervals=[(start_total, end_total)]
            )
        except XboxFamilyError as err:
            raise HomeAssistantError(f"Could not set the Xbox allowed hours: {err}") from err
        day = DAY_KEYS[day_of_week]

        def _update(policy: dict) -> None:
            entry = (policy.get("dailyRestrictions") or {}).get(day)
            if isinstance(entry, dict):
                entry["allowedIntervals"] = [
                    {
                        "begin": f"{start_total // 60:02d}:{start_total % 60:02d}",
                        "end": f"{end_total // 60:02d}:{end_total % 60:02d}",
                    }
                ]

        self._set_xbox_policy_field(child_id, _update)
        await self.async_request_refresh()

    async def async_set_xbox_schedule_enabled(self, child_id: str, enabled: bool) -> None:
        """Turn the Xbox schedule on or off, keeping its days as they are."""
        if not self.web_api:
            raise RuntimeError("Web API not initialized")
        xuid = await self._xapprove_xuid_for(child_id)
        if xuid is not None:
            # xapprove takes isEnabled with one day; send today unchanged.
            today = (dt_util.now().weekday() + 1) % 7  # DAY_KEYS: 0 = Sunday
            try:
                await self.xbox_family.async_set_day(xuid, today, enabled=enabled)
            except XboxFamilyError as err:
                raise HomeAssistantError(f"Could not switch the Xbox limits: {err}") from err
        else:
            await self.web_api.set_mobile_schedule_enabled(child_id, enabled, platform="Xbox")

        def _update(policy: dict) -> None:
            policy["isEnabled"] = bool(enabled)

        self._set_xbox_policy_field(child_id, _update)
        await self.async_request_refresh()

    async def async_set_app_limits_enabled(self, child_id: str, enabled: bool) -> None:
        if not self.web_api:
            raise RuntimeError("Web API not initialized")
        await self.web_api.set_app_limits_enabled(child_id, enabled)
        # Microsoft accepted it: show the new state now. The refresh below is
        # debounced, and until it runs the switch would fall back to the value
        # of the previous poll.
        account = self.account_data(child_id)
        if isinstance(account, dict):
            account["app_limits_enabled"] = bool(enabled)
            self.async_update_listeners()
        await self.async_request_refresh()

    async def async_block_website(self, child_id: str, website: str) -> None:
        if not self.web_api:
            raise RuntimeError("Web API not initialized")
        await self.web_api.block_website(child_id, website)
        await self.async_request_refresh()

    async def async_remove_website(self, child_id: str, website: str) -> None:
        if not self.web_api:
            raise RuntimeError("Web API not initialized")
        await self.web_api.remove_website(child_id, website)
        await self.async_request_refresh()

    async def async_allow_website(self, child_id: str, website: str) -> None:
        if not self.web_api:
            raise RuntimeError("Web API not initialized")
        await self.web_api.allow_website(child_id, website)
        await self.async_request_refresh()

    async def async_toggle_web_filter(self, child_id: str, enabled: bool) -> None:
        if not self.web_api:
            raise RuntimeError("Web API not initialized")
        await self.web_api.toggle_web_filter(child_id, enabled)
        await self.async_request_refresh()

    async def async_set_age_rating(self, child_id: str, age: int) -> None:
        if not self.web_api:
            raise RuntimeError("Web API not initialized")
        await self.web_api.set_age_rating(child_id, age)
        await self.async_request_refresh()

    async def async_set_acquisition_policy(self, child_id: str, require_approval: bool) -> None:
        if not self.web_api:
            raise RuntimeError("Web API not initialized")
        await self.web_api.set_acquisition_policy(child_id, require_approval)
        await self.async_request_refresh()

    def is_account_locked(self, account_id: str) -> bool | None:
        account = self.account_data(account_id)
        if not account:
            return None
        policy = account.get("screentime_policy")
        if not isinstance(policy, dict):
            return None
        daily = policy.get("dailyRestrictions") or policy.get("DailyRestrictions")
        if not isinstance(daily, dict):
            return None
        for day_key in DAY_KEYS:
            day_data = daily.get(day_key) or daily.get(day_key.capitalize())
            if not isinstance(day_data, dict):
                return None
            allowance = day_data.get("allowance") or day_data.get("Allowance") or "00:00:00"
            if allowance != "00:00:00":
                return False
        return True

    def _web_failure_hint(self) -> str:
        """Explain a failed Family web read in terms the user can act on.

        A read can fail because Microsoft is unreachable (timeout, network
        error, backoff) or because the web session is no longer valid. Only the
        second case is fixed by re-authenticating; telling the user to
        re-authenticate during a transient outage sends them through the whole
        sign-in flow for nothing.
        """
        code = self.web_api.last_web_error_code if self.web_api else None
        if code in ("LOGIN_REDIRECT", "AUTH_ERROR", "FAMILY_CONTEXT_AUTH_REQUIRED"):
            return "The Microsoft Family web session has expired: reauthenticate the integration."
        if code in ("TIMEOUT", "NETWORK_ERROR", "FAMILY_CONTEXT_TIMEOUT", "FAMILY_CONTEXT_NETWORK_ERROR"):
            return "Microsoft did not answer (network error or timeout): try again in a few minutes."
        if self.web_api is not None and getattr(self.web_api, "screentime_policy_status", None) == "web_api_backoff":
            return "The Family web API is in a temporary backoff after repeated failures: try again in a few minutes."
        return "Check the Microsoft Family Safety connection sensor for details."

    def _is_transient_web_failure(self) -> bool:
        """True when the last Family web failure was network-level, not a rejection."""
        code = self.web_api.last_web_error_code if self.web_api else None
        return code in ("TIMEOUT", "NETWORK_ERROR", "FAMILY_CONTEXT_TIMEOUT", "FAMILY_CONTEXT_NETWORK_ERROR")

    def _account_lock(self, account_id: str) -> asyncio.Lock:
        return self._account_locks.setdefault(self._policy_key(account_id), asyncio.Lock())

    @staticmethod
    def _has_nonzero_allowance(policy: dict[str, Any]) -> bool:
        """True when at least one day of the policy grants time.

        Use `or` chains rather than dict.get defaults: Microsoft may send an
        explicit {"allowance": None}, and dict.get would then return None
        instead of the default, making an unusable policy look non-zero and
        saving it as the restore point.
        """
        daily = policy.get("dailyRestrictions") or policy.get("DailyRestrictions") or {}
        return any(
            (
                (daily.get(key) or daily.get(key.capitalize()) or {}).get("allowance")
                or (daily.get(key) or daily.get(key.capitalize()) or {}).get("Allowance")
                or "00:00:00"
            )
            != "00:00:00"
            for key in DAY_KEYS
        )

    async def _save_restore_point(self, account_id: str, operation: str) -> None:
        """Read the current schedule and keep it as the restore point.

        Refuses, before any write, when the schedule cannot be read and
        nothing was saved earlier, or when it is already all zeros: the
        restore point is the only way back from the rewrite that follows, and
        an invented or zeroed one would wipe the child's real schedule
        unrecoverably (issue #23).
        """
        key = self._policy_key(account_id)
        current_policy = await self._fetch_screentime_policy(
            account_id, require_child_match=True
        )
        # A restore point is only worth something if `_saved_week` can read
        # it back; otherwise the unlock would refuse and the child stay at 0.
        saved_ok = self._saved_week(self._saved_screentime.get(key) or {}) is not None
        if current_policy:
            if self._has_nonzero_allowance(current_policy):
                if self._saved_week(current_policy) is None:
                    raise UpdateFailed(
                        f"Cannot {operation} account {account_id}: the current schedule "
                        "has a day this integration cannot read back, so it could not "
                        "be restored afterwards."
                    )
                self._saved_screentime[key] = current_policy
                # A new restore point supersedes any earlier release.
                self._released_screentime.discard(key)
                await self._async_save_screentime()
            elif not saved_ok:
                raise UpdateFailed(
                    f"Account {account_id} already has every day set to zero and no "
                    "readable saved schedule exists, so there would be nothing to "
                    "restore later. Set the daily limits first, from Home Assistant "
                    "or at account.microsoft.com/family."
                )
        elif not saved_ok:
            raise UpdateFailed(
                f"Cannot {operation} account {account_id}: current schedule unreadable "
                f"and no readable saved policy exists. {self._web_failure_hint()}"
            )
        else:
            _LOGGER.warning(
                "%s account %s from a previously saved schedule: the current "
                "schedule could not be read, so the restore point may be stale",
                operation.capitalize(), account_id,
            )

    async def _write_week(
        self,
        account_id: str,
        days: list[tuple[int, int, list[bool]]],
        operation: str,
    ) -> None:
        """Write (hours, minutes, slots) for the 7 days, Sunday first.

        Each day is two web writes that can each wait out the full read
        timeout. When a failure is a network one, Microsoft is not answering
        at all: the remaining days would only burn minutes, so stop there.
        The caller keeps its restore point on failure so it can retry.
        """
        failures = 0
        for day_index, (hours, minutes, slots) in enumerate(days):
            try:
                await self._set_screentime_allowance(account_id, day_index, hours, minutes)
                await self._set_screentime_intervals(account_id, day_index, slots)
            except Exception as err:
                failures += 1
                _LOGGER.warning("Could not %s day %d: %s", operation, day_index, err)
                if self._is_transient_web_failure():
                    failures = 7 - day_index
                    _LOGGER.warning(
                        "Stopping %s for account %s after day %d: Microsoft is not "
                        "answering; the saved schedule is kept for retry",
                        operation, account_id, day_index,
                    )
                    break
        if failures:
            raise UpdateFailed(
                f"Screen-time {operation} updated {7 - failures}/7 weekdays; "
                "saved policy retained for retry"
            )

    async def _set_schedule_flag(self, account_id: str, enabled: bool) -> None:
        """Set the weekly schedule's own "enabled" flag when it differs.

        Microsoft only enforces the days while that flag is on, so a lock
        written while it is off would show as applied and do nothing; a
        restore puts the flag back as it was before the lock.
        """
        account = self.account_data(account_id) or {}
        policy = account.get("screentime_policy")
        current = policy.get("isEnabled") if isinstance(policy, dict) else None
        if current is enabled or self.web_api is None:
            return
        try:
            await self.web_api.set_mobile_schedule_enabled(account_id, enabled)
        except Exception as err:  # noqa: BLE001 - the week was written already
            _LOGGER.warning(
                "Schedule written for %s but its enabled flag could not be set to %s: %s",
                account_id, enabled, err,
            )

    async def async_lock_account(self, account_id: str) -> None:
        """Lock an account by zeroing every day's allowance.

        Microsoft exposes no real "lock" endpoint, so a lock is a destructive
        rewrite of the whole weekly schedule; see `_save_restore_point`.
        """
        async with self._account_lock(account_id):
            await self._save_restore_point(account_id, "lock")
            await self._write_week(account_id, [(0, 0, [False] * 48)] * 7, "account lock")
            await self._set_schedule_flag(account_id, True)
        await self.async_request_refresh()

    @staticmethod
    def _saved_week(saved: dict[str, Any]) -> list[tuple[int, int, list[bool]]] | None:
        """Parse a restore point into 7 (hours, minutes, slots); None if any day is unreadable."""
        daily = saved.get("dailyRestrictions") or saved.get("DailyRestrictions") or {}
        days = []
        for day_key in DAY_KEYS:
            day_data = daily.get(day_key) or daily.get(day_key.capitalize()) or {}
            if not isinstance(day_data, dict):
                return None
            allowance = day_data.get("allowance") or day_data.get("Allowance")
            slots = _policy_intervals_to_slots(day_data)
            if not isinstance(allowance, str) or slots is None:
                return None
            # "HH:MM:SS", or the .NET TimeSpan "D.HH:MM:SS" ("1.00:00:00" = 24 h).
            days_part = "0"
            if "." in allowance.split(":")[0]:
                days_part, _, allowance = allowance.partition(".")
            try:
                hours, minutes = (int(part) for part in allowance.split(":")[:2])
                hours += 24 * int(days_part)
            except ValueError:
                return None
            days.append((hours, minutes, slots))
        return days

    async def async_unlock_account(
        self, account_id: str, *, operation: str = "unlock"
    ) -> None:
        """Restore the saved schedule.

        ``operation`` only shapes the error message: the same code path
        serves the account-lock switch ("unlock") and the screen-time
        limits switch ("re-enable screen time limits for"), and telling a
        user they cannot "unlock" a toggle they never locked is confusing.
        """
        async with self._account_lock(account_id):
            await self._restore_saved(account_id, operation)
        await self.async_request_refresh()

    async def _restore_saved(
        self, account_id: str, operation: str, *, enabled: bool | None = None
    ) -> None:
        """Write the restore point back; ``enabled`` forces the flag, else the saved one."""
        key = self._policy_key(account_id)
        saved = self._saved_screentime.get(key)
        # No restore point, or one with a day this build cannot read: writing
        # a default in its place would silently replace a schedule nobody
        # asked to change, and the real one would be gone for good. That is
        # the same destructive write the lock guard refuses (issue #23).
        days = self._saved_week(saved) if saved else None
        if days is None:
            raise UpdateFailed(
                f"Cannot {operation} account {account_id}: "
                + ("the saved schedule is unreadable" if saved else "no saved schedule to restore")
                + ". Set the daily limits again from Home Assistant or at "
                "account.microsoft.com/family; the integration will not guess a "
                "schedule for you."
            )
        # Keeps the only copy of the pre-lock schedule after a partial restore.
        await self._write_week(account_id, days, "restore")
        self._saved_screentime.pop(key, None)
        self._released_screentime.add(key)
        await self._async_save_screentime()
        if enabled is None:
            enabled = saved.get("isEnabled") is not False
        await self._set_schedule_flag(account_id, enabled)

    def is_policy_enabled(self, account_id: str) -> bool | None:
        locked = self.is_account_locked(account_id)
        if locked is None:
            return None
        if locked:
            # All days at 0 → fully locked, which is still "limits enabled".
            return True
        account = self.account_data(account_id)
        policy = (account or {}).get("screentime_policy")
        if not isinstance(policy, dict):
            return None
        if policy.get("isEnabled") is False:
            return False
        daily = policy.get("dailyRestrictions") or policy.get("DailyRestrictions")
        if not isinstance(daily, dict):
            return None
        for day_key in DAY_KEYS:
            day_data = daily.get(day_key) or daily.get(day_key.capitalize()) or {}
            allowance = day_data.get("allowance") or day_data.get("Allowance") or "24:00:00"
            if allowance != "24:00:00":
                return True
        return False

    async def async_set_policy_enabled(self, account_id: str, enabled: bool) -> None:
        # The app's weekly switch is the schedule's own "enabled" flag: Microsoft
        # keeps every day as it is. The 24 h rewrite below is the web fallback,
        # and a restore point it left behind is still restored on enable.
        async with self._account_lock(account_id):
            mobile_ok = False
            if self.web_api is not None:
                try:
                    await self.web_api.set_mobile_schedule_enabled(account_id, enabled)
                    mobile_ok = True
                except Exception as err:  # noqa: BLE001 - fall back to the web path
                    _LOGGER.warning("Mobile schedule on/off failed, trying web: %s", err)
            has_saved = self._policy_key(account_id) in self._saved_screentime
            if enabled:
                # The flag is on; a restore point left by the web fallback
                # (or a lock) is restored too. Without one, the flag alone
                # was the whole job.
                if has_saved:
                    await self._restore_saved(
                        account_id, "re-enable screen time limits for", enabled=True
                    )
                elif not mobile_ok:
                    raise UpdateFailed(
                        f"Cannot re-enable screen time limits for account {account_id}: "
                        "Microsoft refused the mobile request and no saved schedule exists"
                    )
            elif not mobile_ok:
                await self._save_restore_point(account_id, "disable screen-time limits for")
                await self._write_week(
                    account_id, [(24, 0, [True] * 48)] * 7, "policy disable"
                )
        await self.async_request_refresh()

    def connection_state(self) -> dict[str, Any]:
        mobile_ok = self.api is not None and self.last_update_success
        has_cookies = bool(self.web_api and self.web_api.has_web_cookies)
        web_error = self.web_api.last_web_error_code if self.web_api else None
        if not has_cookies:
            web_state = "missing"
        elif self.web_api is None:
            web_state = "unknown"
        else:
            web_state = self.web_api.web_session_state

        # Only a successful authenticated Family-dashboard probe proves that the
        # browser session is healthy. Cookie presence alone is insufficient.
        # Do not report False for a timeout/network error: that means "unknown",
        # not "authentication rejected".
        if web_state == "authenticated":
            web_authenticated: bool | None = True
        elif web_state in ("missing", "expired"):
            web_authenticated = False
        else:
            web_authenticated = None
        # An authenticated /account session is not enough on its own: screen
        # time lives behind the Family SPA, and once that bootstrap lands on a
        # login page every schedule read fails while /account keeps answering
        # 200. Reporting "connected" there hides the exact state the user must
        # act on, so the Family context is part of the verdict.
        family_state = self.web_api.family_context_state if self.web_api else "unavailable"
        family_ok = family_state in ("ready", "unknown")
        web_ok = web_authenticated is True and family_ok
        state = "connected" if mobile_ok and web_ok else "degraded" if mobile_ok else "disconnected"
        if self._mobile_only or (self.xbox_features and self.xbox_family.usable):
            # No web session by design, or Xbox runs on its own sign-in:
            # the mobile API alone decides.
            state = "connected" if mobile_ok else "disconnected"
        reauth_recommended = bool(
            self._native_web_auth
            and (
                (web_state == "error" and web_error in ("TIMEOUT", "NETWORK_ERROR"))
                or family_state == "auth_required"
            )
        )
        return {
            "state": state,
            "mobile_api": "ok" if mobile_ok else "error",
            "roster_errors": self._roster_errors_by_account(),
            "web_session": web_state,
            "web_session_authenticated": web_authenticated,
            "web_session_last_checked": (
                self.web_api.web_session_last_checked if self.web_api else None
            ),
            "web_session_last_http_status": (
                self.web_api.web_session_last_http_status if self.web_api else None
            ),
            "web_last_error_code": web_error,
            "account_interrupt": (
                getattr(self.web_api, "account_interrupt", None) if self.web_api else None
            ),
            "web_api": self.web_api.web_api_state if self.web_api else "unavailable",
            "web_api_last_checked": (
                self.web_api.web_api_last_checked if self.web_api else None
            ),
            "web_api_last_http_status": (
                self.web_api.web_api_last_http_status if self.web_api else None
            ),
            "web_api_last_endpoint": (
                self.web_api.web_api_last_endpoint if self.web_api else None
            ),
            "family_context": (
                self.web_api.family_context_state if self.web_api else "unavailable"
            ),
            "family_context_last_checked": (
                self.web_api.family_context_last_checked if self.web_api else None
            ),
            "family_context_last_http_status": (
                self.web_api.family_context_last_http_status if self.web_api else None
            ),
            "family_context_last_path": (
                self.web_api.family_context_last_path if self.web_api else None
            ),
            "family_token_source": (
                self.web_api.family_token_source if self.web_api else None
            ),
            "screentime_policy_status": (
                self.web_api.screentime_policy_status if self.web_api else "unavailable"
            ),
            "screentime_policy_source": (
                self.web_api.screentime_policy_source if self.web_api else None
            ),
            "cookies_loaded": self._web_cookies_loaded,
            "native_web_auth": self._native_web_auth,
            "xbox_features": not self._mobile_only,
            **self.xbox_family.diagnostics(),
            "xbox_family_matched_children": len(self._xbox_xuids),
            "xbox_family_unmatched_children": len(self._xbox_unmatched - set(self._xbox_xuids)),
            "runtime_credentials_persisted": bool(self._runtime_auth_state),
            "reauth_requested": self._reauth_requested,
            "reauth_required": self._reauth_requested,
            "reauth_reason": self._reauth_reason,
            "reauth_recommended": reauth_recommended,
            "last_update_success": self.last_update_success,
        }

    async def _fetch_web_api_data(self, account_id: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "web_browsing": None,
            "screentime_policy": None,
        }
        if not self.web_api:
            return result
        try:
            result["web_browsing"] = await self.web_api.get_web_browsing_settings(account_id)
        except Exception as err:
            _LOGGER.debug("Could not fetch web browsing settings: %r", err)
        try:
            # ContentRestrictions is still an established pyfamilysafety 1.1.2
            # mobile endpoint. Fetch it separately so age-rating and
            # acquisition-policy state are visible instead of exposing write-only
            # services with no readback. Failure stays non-fatal because Microsoft
            # can gate individual private endpoints independently.
            result["content_settings"] = await self.web_api.get_content_settings(account_id)
        except Exception as err:
            _LOGGER.debug("Could not fetch content settings: %r", err)
        try:
            result["app_limits_enabled"] = await self.web_api.get_app_limits_enabled(
                account_id
            )
        except Exception as err:
            _LOGGER.debug("Could not fetch the app limits switch: %r", err)
        if time.monotonic() >= self._web_activity_next.get(account_id, 0.0):
            self._web_activity_next[account_id] = time.monotonic() + 1800
            try:
                result["web_activity"] = await self.web_api.get_web_activity(account_id)
            except Exception as err:
                _LOGGER.debug("Could not fetch web activity: %r", err)
        try:
            screentime = await self._fetch_screentime_policy(account_id)
            expired = (
                False
                if self._mobile_only
                else self.web_api.web_session_state == "expired"
                if self._native_web_auth
                else self._addon_client.last_error_code == "LOGIN_REDIRECT"
            )
            if screentime is None and expired:
                await self._create_auth_notification()
                if self._native_web_auth:
                    self._request_reauth("web_session_expired")
            elif screentime is not None:
                await self._dismiss_auth_notification()
            result["screentime_policy"] = screentime
        except Exception as err:
            _LOGGER.debug("Could not fetch screen time policy: %r", err)

        # Only an actual browser-login redirect or an independently expired
        # account session proves that reauthentication is required.  A private
        # Family API 401 can also be caused by a stale/wrong Family SPA
        # request-verification token, so do not create a reauth loop while the
        # /account session probe is still authenticated.
        if self._native_web_auth and (
            self.web_api.last_web_error_code == "LOGIN_REDIRECT"
            or self.web_api.web_session_state == "expired"
        ):
            await self._create_auth_notification()
            self._request_reauth("web_session_expired")
        elif (
            self._native_web_auth
            and self.web_api.last_web_error_code == "AUTH_ERROR"
            and self.web_api.web_session_state == "authenticated"
        ):
            _LOGGER.warning(
                "Microsoft Family private web API rejected a request while the "
                "Microsoft account session is authenticated; keeping reauth disabled "
                "and treating this as Family API context/authentication failure"
            )
        if self._mobile_only:
            return result
        try:
            result["xbox_screentime_policy"] = (
                await self.web_api.get_mobile_xbox_screentime_policy(account_id)
            )
        except Exception as err:
            _LOGGER.debug("Could not fetch the Xbox schedule: %r", err)
        return result

    def _request_reauth(self, reason: str) -> None:
        """Ask Home Assistant to start a linked reauthentication flow.

        ConfigEntry.async_start_reauth() de-duplicates active flows itself. Calling
        it again on later polls means a deliberately aborted/expired repair can be
        offered again without requiring a Home Assistant restart.
        """
        if not self._reauth_requested:
            _LOGGER.warning(
                "Microsoft Family Safety authentication requires renewal (%s)", reason
            )
        self._reauth_requested = True
        self._reauth_reason = reason
        self.entry.async_start_reauth(self.hass, data={"reason": reason})

    async def async_request_reauth(self) -> None:
        """Service entry point: open the Microsoft sign-in flow without waiting
        for the coordinator to notice an expired session on its own."""
        await self._create_auth_notification()
        self._request_reauth("manual")

    @property
    def _family_context_dead_end(self) -> bool:
        return self._family_auth_required_polls >= _FAMILY_AUTH_REQUIRED_POLLS

    async def _async_track_family_context(self) -> None:
        """Escalate a Family bootstrap that keeps landing on a login page.

        Microsoft drops the SSO cookies the Family SPA needs well before
        /account stops answering 200, and from then on every bootstrap in
        _warm_family_context redirects to login.microsoftonline.com. Nothing
        server-side can rebuild that context, so without this the entry stayed
        "authenticated" with every schedule unknown and no reauth offered.
        A single poll cannot tell that from a Microsoft hiccup; consecutive
        ones can.
        """
        if not self._native_web_auth or self.web_api is None:
            return
        if self.web_api.family_context_state != "auth_required":
            self._family_auth_required_polls = 0
            if self._reauth_reason == "family_context_auth_required":
                self._reauth_requested = False
                self._reauth_reason = None
            return
        self._family_auth_required_polls += 1
        if not self._family_context_dead_end:
            return
        if getattr(self.web_api, "mobile_schedule_ok", False):
            # Schedules run on the mobile API; the expired web context only
            # affects web-only extras, so a daily reauth prompt is not worth it.
            if self._family_auth_required_polls == _FAMILY_AUTH_REQUIRED_POLLS:
                _LOGGER.info(
                    "Family web context needs an interactive sign-in; schedules keep "
                    "working through the mobile API%s, so no reauthentication is requested",
                    " and Xbox through the Xbox Family Settings sign-in"
                    if self.xbox_features and self.xbox_family.linked
                    else "",
                )
            return
        await self._create_auth_notification()
        self._request_reauth("family_context_auth_required")

    def _transform_account_data(self, account: Account) -> tuple[str, dict[str, Any]]:
        account_id = account.user_id
        today = dt_util.now().date().isoformat()
        raw_ms = int(account.today_screentime_usage or 0)
        raw_minutes = _ms_to_minutes(raw_ms)
        polled_at = dt_util.now().isoformat()

        # Expose Microsoft's current aggregate directly.  Do not clamp the
        # value to a locally remembered maximum: Home Assistant's recorder and
        # history UI are responsible for historical presentation, while the
        # entity state should reflect the latest API response verbatim.
        data = {
            "user_id": account.user_id,
            "first_name": account.first_name,
            "surname": account.surname,
            "profile_picture": account.profile_picture,
            "today_screentime_usage": raw_minutes,
            "raw_today_screentime_usage": raw_minutes,
            "raw_today_screentime_ms": raw_ms,
            "screen_time_polled_at": polled_at,
            "screen_time_date": today,
            "average_screentime_usage": _ms_to_minutes(account.average_screentime_usage),
            "account_balance": account.account_balance,
            "account_currency": account.account_currency,
            "blocked_platforms": self._merge_blocked_platforms(account),
            # Endpoints Microsoft could not resolve for this member on the
            # last poll (see _pyfamilysafety_compat._patch_account_roster_tolerance).
            "roster_errors": dict(getattr(account, "roster_errors", None) or {}),
            "devices": [],
            "applications": [
                {
                    "app_id": app.app_id,
                    "app_name": app.name,
                    "blocked": app.blocked,
                    "icon": app.icon,
                    "usage_minutes": round(app.usage, 1) if app.usage else 0,
                }
                for app in account.applications
            ],
        }
        return account_id, data

    def _transform_device_data(self, device: Device, account_id: str) -> tuple[str, dict[str, Any]]:
        return device.device_id, {
            "device_id": device.device_id,
            "device_name": device.device_name,
            "device_class": device.device_class,
            "device_make": device.device_make,
            "device_model": device.device_model,
            "os_name": device.os_name,
            "today_time_used": _ms_to_minutes(device.today_time_used),
            "last_seen": device.last_seen,
            "blocked": device.blocked,
            "account_id": account_id,
        }

    async def _async_update_data(self) -> dict[str, Any]:
        if self.api is None:
            await self._async_setup_api()
        await self._async_load_web_cookies()
        assert self.api is not None
        try:
            # Verify the browser session independently from individual private
            # API endpoints. This makes the Connection sensor authoritative and
            # ensures an expired web login starts HA reauthentication even when
            # the mobile API is still healthy. Network errors are reported as
            # such and do not trigger a needless login flow.
            if self._native_web_auth and self.web_api is not None:
                web_authenticated = await self.web_api.async_check_web_session()
                # A live /account session does not clear a reauth raised for a
                # dead Family context: that is precisely the state where
                # /account still answers 200. _async_track_family_context
                # owns that flag and clears it once the context is back.
                if web_authenticated and not self._family_context_dead_end:
                    await self._dismiss_auth_notification()
                    self._reauth_requested = False
                    self._reauth_reason = None
                elif self.web_api.web_session_state in ("missing", "expired"):
                    await self._create_auth_notification()
                    self._request_reauth(
                        "web_credentials_missing"
                        if self.web_api.web_session_state == "missing"
                        else "web_session_expired"
                    )
            await self._async_try_silent_xbox_link()
            await self.api.update()
            if not getattr(self.api, "accounts", None):
                self.api.accounts = []
            accounts_data: dict[str, Any] = {}
            devices_data: dict[str, Any] = {}
            new_accounts: dict[str, Account] = {}
            new_devices: dict[str, Device] = {}
            for account in self.api.accounts:
                account_id, account_data = self._transform_account_data(account)
                accounts_data[account_id] = account_data
                new_accounts[account_id] = account
                for device in account.devices:
                    device_id, device_data = self._transform_device_data(device, account_id)
                    devices_data[device_id] = device_data
                    accounts_data[account_id]["devices"].append(device_id)
                    new_devices[device_id] = device
                web_data = await self._fetch_web_api_data(account_id)
                # A transient failure of one private web endpoint must not turn
                # an otherwise healthy entity into `unknown` for the next poll.
                # Keep the previous successful value in memory until Microsoft
                # returns a replacement.  This is not a screen-time usage guard:
                # it applies only to configuration/policy payloads and never
                # modifies measured usage values.
                previous = ((self.data or {}).get("accounts", {}).get(account_id) or {})
                for key in ("web_browsing", "content_settings", "screentime_policy", "app_limits_enabled", "xbox_screentime_policy", "web_activity"):
                    if web_data.get(key) is None and previous.get(key) is not None:
                        web_data[key] = previous.get(key)
                accounts_data[account_id].update(web_data)
                # Both Xbox reads (usage report, then policy) only run while the
                # Family session is healthy and not backing off, so a throttled
                # session never gets another request piled onto it. Not gated
                # on the /st (Windows schedule) read: a child with no Windows
                # schedule still has an Xbox. Per-child intervals and the shared
                # "no Xbox" backoff live in the api_client methods.
                # With the Xbox Family Settings sign-in linked, the
                # schedule, lock state and usage come from xapprove, and the web
                # reads below only run when that fails.
                xapprove = await self._async_fetch_xapprove(account, accounts_data[account_id])
                xbox_gate, usage_gate = self._xbox_web_gates(xapprove)
                # Xbox usage: since about 2026-08-20 the mobile aggregator
                # reports 0 (or a stale figure) for Xbox play on some accounts,
                # so read the Xbox activity report from the web API as well.
                today_iso = dt_util.now().date().isoformat()
                xbox_usage: int | None = xapprove.get("usage") if xapprove else None
                if usage_gate:
                    try:
                        xbox_usage = await self.web_api.get_xbox_screentime_usage(
                            account_id,
                            self.hass.config.time_zone or "UTC",
                            today_iso,
                        )
                    except Exception as err:
                        _LOGGER.debug("Could not fetch Xbox activity usage: %r", err)
                if xbox_usage is None:
                    # Keep the last known Xbox figure across a transient miss,
                    # but only within the same day (avoid carrying yesterday's
                    # total past midnight before today's row appears).
                    prev_xbox = previous.get("xbox_today_usage")
                    if (
                        isinstance(prev_xbox, int)
                        and previous.get("screen_time_date") == today_iso
                    ):
                        xbox_usage = prev_xbox
                accounts_data[account_id]["xbox_today_usage"] = xbox_usage
                # The account figure covers every platform, the report only
                # Xbox, so it replaces the account figure only when it is
                # higher: a child who also uses Windows keeps the mobile total,
                # and an account the aggregator now reports as 0 gets its Xbox
                # time back. Never lower a reported usage figure. The raw_*
                # fields keep exactly what the mobile aggregator returned.
                mobile_usage = accounts_data[account_id].get("today_screentime_usage")
                if xbox_usage is not None and xbox_usage > (mobile_usage or 0):
                    accounts_data[account_id]["today_screentime_usage"] = xbox_usage
                # The policy is read after the usage report: for a child without
                # an Xbox the report answers 403 and sets the shared "no Xbox"
                # backoff, which then skips this read in the same poll (the
                # policy itself answers 200 for such a child).
                xbox_policy: dict | None = xapprove.get("lock_policy") if xapprove else None
                if xbox_gate:
                    try:
                        xbox_policy = await self.web_api.get_xbox_screentime_policy(
                            account_id
                        )
                    except Exception as err:
                        _LOGGER.debug("Could not fetch Xbox screen-time policy: %r", err)
                # The lock state comes from a fresh read only. The full payload
                # is kept on the account data (last good one on a miss) for
                # anything else that needs the Xbox schedule.
                self._apply_xbox_lock_state(
                    account_id,
                    accounts_data[account_id],
                    xbox_policy,
                    no_xbox=bool(
                        self.web_api is not None
                        and self.web_api.has_no_xbox_data(account_id)
                    ),
                )
                accounts_data[account_id]["xbox_policy"] = (
                    xbox_policy if xbox_policy is not None else previous.get("xbox_policy")
                )
            await self._async_track_family_context()
            await self._async_sync_roster_notification(accounts_data)
            self._accounts = new_accounts
            self._devices = new_devices
            pending = getattr(self.api, "pending_requests", None) or []
            self._is_retrying_auth = False
            return {"accounts": accounts_data, "devices": devices_data, "pending_requests": pending}
        except HttpException as err:
            if "401" in str(err) or "authentication" in str(err).lower():
                if not self._is_retrying_auth:
                    self._is_retrying_auth = True
                    self.web_api = None
                    raise ConfigEntryAuthFailed(ERROR_TOKEN_EXPIRED) from err
                raise UpdateFailed(f"Authentication failed: {err}") from err
            raise UpdateFailed(f"Error communicating with API: {err}") from err
        except ConfigEntryAuthFailed:
            raise
        except Exception as err:
            _LOGGER.exception("Unexpected error fetching Family Safety data")
            raise UpdateFailed(f"Unexpected error: {err}") from err
        finally:
            try:
                await self._async_persist_current_auth()
            except Exception as persist_err:
                _LOGGER.debug(
                    "Could not persist rotated Family Safety credentials: %s",
                    persist_err,
                )

    async def _create_auth_notification(self) -> None:
        if self._auth_notification_sent:
            return
        message = (
            "Microsoft asks you to accept its updated Terms of Use before the "
            "Family Safety session can be renewed.\n\n"
            "Home Assistant has started a reauthentication flow. Open the integration "
            "or the Repairs page, complete the Microsoft sign-in and accept the terms "
            "when Microsoft shows them. Accepting them directly at "
            "https://account.microsoft.com works too: the integration then picks the "
            "session up again on its own within a few minutes. This is a one-time "
            "step."
            if self._native_web_auth
            and getattr(self.web_api, "account_interrupt", None) == "terms_of_use"
            else
            "Microsoft asks you to confirm your account's security info (\"Help us "
            "protect your account\") before the Family Safety session can be "
            "renewed.\n\n"
            "Sign in at https://account.microsoft.com with the parent account and "
            "confirm the security info Microsoft shows, then wait a few minutes: the "
            "integration picks the session up again on its own. Completing the "
            "reauthentication Home Assistant has started works too."
            if self._native_web_auth
            and getattr(self.web_api, "account_interrupt", None) == "proof_confirm"
            else
            "Microsoft asks for a new interactive sign-in before the Family Safety "
            "session can be renewed. On some accounts Microsoft requires this every "
            "24 hours for Family Safety, even with \"Stay signed in?\" answered Yes.\n\n"
            "Home Assistant has started a reauthentication flow. Open the integration "
            "or the Repairs page and complete the Microsoft sign-in."
            if self._native_web_auth
            and getattr(self.web_api, "account_interrupt", None) == "signin_required"
            else
            "Your Microsoft Family Safety web session is missing or has expired.\n\n"
            "Home Assistant has started a reauthentication flow. Open the integration "
            "or the Repairs page and complete the Microsoft sign-in to renew both the "
            "mobile token and Family web session."
            if self._native_web_auth
            else
            "Your Microsoft Family Safety web session has expired.\n\n"
            "Open the integration or the Repairs page and complete the Microsoft "
            "sign-in. This also moves the entry from the auth add-on to the native "
            "sign-in: answer Yes to \"Stay signed in?\" and the add-on is no longer "
            "needed."
        )
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {"title": "Microsoft Family Safety - Authentication Required", "message": message,
             "notification_id": AUTH_NOTIFICATION_ID},
        )
        self._auth_notification_sent = True

    async def _dismiss_auth_notification(self) -> None:
        if not self._auth_notification_sent:
            return
        await self.hass.services.async_call(
            "persistent_notification",
            "dismiss",
            {"notification_id": AUTH_NOTIFICATION_ID},
        )
        self._auth_notification_sent = False

    def _roster_errors_by_account(self) -> dict[str, list[str]]:
        """Return {account_id: [endpoint, ...]} for members Microsoft cannot fully resolve."""
        accounts = (self.data or {}).get("accounts", {}) if self.data else {}
        return {
            str(account_id): sorted((account.get("roster_errors") or {}).keys())
            for account_id, account in accounts.items()
            if account.get("roster_errors")
        }

    async def _async_sync_roster_notification(self, accounts_data: dict[str, Any]) -> None:
        """Raise one persistent notification while a member is partially unresolvable.

        The tolerance patch keeps the integration running with empty data for
        the failing endpoint; the notification tells the user why some values
        stay empty and what usually fixes it.  It is dismissed as soon as
        Microsoft resolves the member again.
        """
        affected = [
            (account.get("first_name") or str(account_id), sorted(account["roster_errors"]))
            for account_id, account in accounts_data.items()
            if account.get("roster_errors")
        ]
        if not affected:
            if self._roster_notification_sent:
                await self.hass.services.async_call(
                    "persistent_notification",
                    "dismiss",
                    {"notification_id": ROSTER_NOTIFICATION_ID},
                )
                self._roster_notification_sent = False
            return
        if self._roster_notification_sent:
            return
        lines = "\n".join(
            f"- {name}: {', '.join(endpoints)}" for name, endpoints in affected
        )
        message = (
            "Microsoft cannot resolve part of the family roster, so some values "
            "stay empty for:\n\n"
            f"{lines}\n\n"
            "The integration keeps working with everything Microsoft still "
            "returns. This usually happens when a reset or decommissioned device "
            "is still listed at https://account.microsoft.com/family, or when a "
            "school/work (Entra ID) account is enrolled on the child's device. "
            "Removing the stale device from the family, when that is possible, "
            "clears it."
        )
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "Microsoft Family Safety - Roster partially unavailable",
                "message": message,
                "notification_id": ROSTER_NOTIFICATION_ID,
            },
        )
        self._roster_notification_sent = True

    async def async_cleanup(self) -> None:
        self._accounts.clear()
        self._devices.clear()
        if self.web_api:
            await self.web_api.close()
        self.web_api = None
        self.api = None
