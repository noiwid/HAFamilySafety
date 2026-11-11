"""Xbox parental controls through the Xbox Family Settings app's API.

Microsoft moved the Xbox settings out of the Family Safety app into the Xbox
Family Settings app, which talks to ``xapprove.xboxlive.com``. Its per-day
limits are the ones the console enforces; the Xbox schedule on the Family
Safety mobile API is a separate setting that no longer matches them.

Authentication runs on a refresh token of the Xbox Family Settings client
(``00000000482c8f49``), so it does not depend on the Family web session that
Microsoft makes some accounts renew with an interactive sign-in every 24 h.
From that one token:

- an MSA token for ``service::user.auth.xboxlive.com::MBI_SSL`` becomes an Xbox
  Live user token (``user.auth.xboxlive.com``, ``RpsTicket: t=``), then an XSTS
  token for ``http://xboxlive.com`` (``Authorization: XBL3.0 x=uhs;token``);
- an MSA token for ``service::xapprove.xboxlive.com::MBI_SSL`` is sent as
  ``x-ms-rps-ticket``; xapprove answers 401 without it.

No device token, title token or request signing is needed on this path
(verified against a real account, also 40 h after the sign-in).

Override types seen on ``/settings/screentime/overrides``: 1 = extra time
until ``validUntil`` (the app's "add time"), 2 = blocked until ``validUntil``
(what the web blockUntil lock stores), 100 = sent by the app's "revoke time
extension" and leaves no override behind.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta
import hashlib
import json
import logging
import secrets
from typing import Any
from urllib.parse import urlencode

import aiohttp
from yarl import URL

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DAY_KEYS, DOMAIN

_LOGGER = logging.getLogger(__name__)

XBOX_CLIENT_ID = "00000000482c8f49"
XBOX_REDIRECT_URI = "https://login.live.com/oauth20_desktop.srf"
USER_AUTH_SCOPE = "service::user.auth.xboxlive.com::MBI_SSL"
XAPPROVE_SCOPE = "service::xapprove.xboxlive.com::MBI_SSL"
_TOKEN_URL = "https://login.live.com/oauth20_token.srf"
_AUTHORIZE_URL = "https://login.live.com/oauth20_authorize.srf"
_USER_AUTH_URL = "https://user.auth.xboxlive.com/user/authenticate"
_XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
_XAPPROVE = "https://xapprove.xboxlive.com/api"
_FAMILY_URL = "https://accounts.xboxlive.com/family/memberXuid({xuid})"

OVERRIDE_ALLOW = 1
OVERRIDE_BLOCK = 2
OVERRIDE_REVOKE = 100

_STORE_VERSION = 1
#: Renew a token this long before Microsoft says it expires.
_MARGIN = timedelta(minutes=5)
#: The family roster (Xbox user ids) rarely changes.
_FAMILY_TTL = timedelta(hours=6)
_TIMEOUT = aiohttp.ClientTimeout(total=30)


class XboxFamilyError(Exception):
    """An xapprove or Xbox Live request failed."""


class XboxFamilyAuthError(XboxFamilyError):
    """The stored refresh token no longer works: the Xbox sign-in must be linked again."""


def build_authorize_url() -> tuple[str, str]:
    """Return (sign-in URL, PKCE verifier) for linking the Xbox Family Settings client."""
    verifier = secrets.token_urlsafe(64)[:86]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    params = {
        "client_id": XBOX_CLIENT_ID.upper(),
        "response_type": "code",
        "redirect_uri": XBOX_REDIRECT_URI,
        "scope": USER_AUTH_SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "lw": "1",
        "state": secrets.token_urlsafe(16),
    }
    return f"{_AUTHORIZE_URL}?{urlencode(params)}", verifier


def _utc_offset() -> str:
    """Home Assistant's current UTC offset as ``+HH:MM`` (xapprove's x-timezone-offset)."""
    offset = dt_util.now().strftime("%z") or "+0000"
    return f"{offset[:3]}:{offset[3:]}"


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = dt_util.parse_datetime(value.replace("Z", "+00:00"))
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.UTC)
    return parsed


def _hm(value: Any) -> int:
    """Minutes in an xapprove ``{"days", "hour", "minute"}`` object."""
    if not isinstance(value, dict):
        return 0
    return (
        int(value.get("days") or 0) * 1440
        + int(value.get("hour") or 0) * 60
        + int(value.get("minute") or 0)
    )


def schedule_to_policy(schedule: dict[str, Any]) -> dict[str, Any]:
    """Convert the xapprove schedule to the shape the Xbox entities read.

    The Xbox numbers and switch read ``xbox_screentime_policy`` as
    ``{"isEnabled", "dailyRestrictions": {day_key: {"allowance": "HH:MM:SS"}}}``.
    """
    daily: dict[str, Any] = {}
    for day in schedule.get("dailyRestrictions") or []:
        index = day.get("dayOfWeek")
        if not isinstance(index, int) or not 0 <= index <= 6:
            continue
        minutes = _hm(day.get("allowance"))
        daily[DAY_KEYS[index]] = {
            "allowance": f"{minutes // 60:02d}:{minutes % 60:02d}:00",
            "allowedIntervals": [
                {
                    "begin": f"{_hm(i.get('begin')) // 60:02d}:{_hm(i.get('begin')) % 60:02d}",
                    "end": f"{_hm(i.get('end')) // 60:02d}:{_hm(i.get('end')) % 60:02d}",
                }
                for i in day.get("allowedIntervals") or []
                if isinstance(i, dict)
            ],
        }
    return {
        "isEnabled": schedule.get("isEnabled"),
        "dailyRestrictions": daily,
        "source": "xapprove",
    }


def overrides_to_policy(overrides: dict[str, Any]) -> dict[str, Any]:
    """Express the active xapprove override in the web Xbox policy shape.

    ``coordinator._apply_xbox_lock_state`` reads ``screenTimeOverride`` with a
    ``screenTimeOverrideType`` of blockUntil / allowUntil, or no override plus
    ``dailyRestrictions`` for "Microsoft answered, not blocked".
    """
    active = [o for o in overrides.get("overrides") or [] if isinstance(o, dict)]
    for override_type, name in ((OVERRIDE_BLOCK, "blockUntil"), (OVERRIDE_ALLOW, "allowUntil")):
        for override in active:
            if override.get("overrideType") == override_type:
                return {
                    "screenTimeOverride": {
                        "screenTimeOverrideType": name,
                        "validUntil": override.get("validUntil"),
                    }
                }
    if active:
        # Unknown type: leave the lock state as it is.
        return {
            "screenTimeOverride": {
                "screenTimeOverrideType": f"xapprove_{active[0].get('overrideType')}"
            }
        }
    return {"dailyRestrictions": {}}


def usage_today(report: dict[str, Any], today: str) -> int | None:
    """Minutes of Xbox play today from ``activityreport/screenTimeUsage``."""
    if not isinstance(report, dict) or "dailyScreenTimeEvents" not in report:
        return None
    for event in report.get("dailyScreenTimeEvents") or []:
        if str(event.get("date", ""))[:10] == today:
            hours, minutes, seconds = (str(event.get("timeUsed") or "0:0:0").split(":") + ["0", "0"])[:3]
            return int(hours) * 60 + int(minutes) + (1 if float(seconds or 0) >= 30 else 0)
    return 0


class XboxFamilyAPI:
    """Token chain and xapprove calls for one config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._store: Store = Store(hass, _STORE_VERSION, f"{DOMAIN}.xbox.{entry_id}")
        self._refresh_token: str | None = None
        self._msa: dict[str, tuple[str, datetime]] = {}
        self._xsts: tuple[str, str, datetime] | None = None
        self._user_token: tuple[str, datetime] | None = None
        self._parent_xuid: str | None = None
        self._family: list[dict[str, Any]] | None = None
        self._family_at: datetime | None = None
        self._lock = asyncio.Lock()
        #: Day writes read the day, change it and send it back: one at a time,
        #: so a second write does not send back the day the first one changed.
        self._day_lock = asyncio.Lock()
        self.state = "not_linked"
        self.last_error: str | None = None
        self.last_success: str | None = None
        self.linked_via: str | None = None

    @property
    def linked(self) -> bool:
        return bool(self._refresh_token)

    @property
    def ready(self) -> bool:
        """Linked and the last call to Microsoft worked."""
        return self.linked and self.state == "ok"

    @property
    def usable(self) -> bool:
        """Linked and the token not refused: a transient error does not count."""
        return self.linked and self.state != "auth_failed"

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self._refresh_token = data.get("refresh_token")
        self.linked_via = data.get("linked_via")
        if self._refresh_token:
            self.state = "linked"

    async def _async_save(self) -> None:
        await self._store.async_save(
            {"refresh_token": self._refresh_token, "linked_via": self.linked_via}
        )

    async def async_unlink(self) -> None:
        self._refresh_token = None
        self._msa.clear()
        self._xsts = None
        self._user_token = None
        self.state = "not_linked"
        await self._store.async_remove()

    # --- linking -----------------------------------------------------------
    async def async_link_from_redirect(self, redirect_url: str, verifier: str, via: str) -> None:
        """Exchange the ``oauth20_desktop.srf?code=...`` redirect for a refresh token."""
        query = URL(redirect_url.strip()).query
        if "error" in query:
            raise XboxFamilyAuthError(
                f"Microsoft refused the sign-in: {query.get('error')} "
                f"{query.get('error_description', '')}".strip()
            )
        code = query.get("code")
        if not code:
            raise XboxFamilyAuthError("The address has no sign-in code")
        body = await self._token_request(
            {
                "client_id": XBOX_CLIENT_ID,
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": XBOX_REDIRECT_URI,
                "scope": USER_AUTH_SCOPE,
            }
        )
        # A relink may be another parent account: forget everything derived
        # from the previous token.
        self._msa.clear()
        self._xsts = None
        self._user_token = None
        self._parent_xuid = None
        self._family = None
        self._refresh_token = body["refresh_token"]
        self._remember_msa(USER_AUTH_SCOPE, body)
        self.linked_via = via
        self.state = "linked"
        self.last_error = None
        await self._async_save()
        _LOGGER.info("Xbox Family Settings sign-in linked (%s)", via)

    # --- tokens -------------------------------------------------------------
    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        session = async_get_clientsession(self._hass)
        try:
            async with session.post(_TOKEN_URL, data=data, timeout=_TIMEOUT) as resp:
                body = await resp.json(content_type=None)
                status = resp.status
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            raise XboxFamilyError(f"Microsoft token endpoint unreachable: {err!r}") from err
        if status != 200 or not isinstance(body, dict) or "access_token" not in body:
            error = body.get("error") if isinstance(body, dict) else None
            if error in ("invalid_grant", "interaction_required", "consent_required"):
                raise XboxFamilyAuthError(f"Xbox sign-in no longer valid ({error})")
            raise XboxFamilyError(f"Microsoft token endpoint answered {status} ({error})")
        return body

    def _remember_msa(self, scope: str, body: dict[str, Any]) -> None:
        expires = dt_util.utcnow() + timedelta(seconds=int(body.get("expires_in") or 3600))
        self._msa[scope] = (body["access_token"], expires)

    async def _msa_token(self, scope: str) -> str:
        cached = self._msa.get(scope)
        if cached and cached[1] - _MARGIN > dt_util.utcnow():
            return cached[0]
        if not self._refresh_token:
            raise XboxFamilyAuthError("Xbox Family Settings sign-in not linked")
        body = await self._token_request(
            {
                "client_id": XBOX_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "scope": scope,
            }
        )
        self._remember_msa(scope, body)
        rotated = body.get("refresh_token")
        if rotated and rotated != self._refresh_token:
            self._refresh_token = rotated
            await self._async_save()
        return body["access_token"]

    async def _xbl_post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        session = async_get_clientsession(self._hass)
        headers = {"x-xbl-contract-version": "1", "Content-Type": "application/json"}
        try:
            async with session.post(url, json=payload, headers=headers, timeout=_TIMEOUT) as resp:
                status = resp.status
                text = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise XboxFamilyError(f"Xbox Live auth unreachable: {err!r}") from err
        try:
            body = json.loads(text) if text else {}
        except ValueError:
            body = {}
        if status != 200 or not isinstance(body, dict) or "Token" not in body:
            raise XboxFamilyError(f"Xbox Live auth answered {status} (XErr {body.get('XErr')})")
        return body

    async def _xbl_auth(self) -> tuple[str, str]:
        """Return (user hash, XSTS token) for http://xboxlive.com."""
        now = dt_util.utcnow()
        if self._xsts and self._xsts[2] - _MARGIN > now:
            return self._xsts[0], self._xsts[1]
        if not self._user_token or self._user_token[1] - _MARGIN <= now:
            access = await self._msa_token(USER_AUTH_SCOPE)
            body = await self._xbl_post(
                _USER_AUTH_URL,
                {
                    "RelyingParty": "http://auth.xboxlive.com",
                    "TokenType": "JWT",
                    "Properties": {
                        "AuthMethod": "RPS",
                        "SiteName": "user.auth.xboxlive.com",
                        "RpsTicket": f"t={access}",
                    },
                },
            )
            self._user_token = (
                body["Token"],
                _parse_time(body.get("NotAfter")) or now + timedelta(hours=1),
            )
        body = await self._xbl_post(
            _XSTS_URL,
            {
                "RelyingParty": "http://xboxlive.com",
                "TokenType": "JWT",
                "Properties": {"SandboxId": "RETAIL", "UserTokens": [self._user_token[0]]},
            },
        )
        claims = (body.get("DisplayClaims", {}).get("xui") or [{}])[0]
        self._parent_xuid = claims.get("xid") or self._parent_xuid
        self._xsts = (
            claims["uhs"],
            body["Token"],
            _parse_time(body.get("NotAfter")) or now + timedelta(hours=1),
        )
        return self._xsts[0], self._xsts[1]

    # --- requests -----------------------------------------------------------
    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        rps: bool = True,
        contract: str | None = None,
    ) -> Any:
        async with self._lock:
            try:
                uhs, xsts = await self._xbl_auth()
                headers = {
                    "Authorization": f"XBL3.0 x={uhs};{xsts}",
                    "Content-Type": "application/json",
                    "Accept-Language": self._hass.config.language or "en",
                    "x-timezone-offset": _utc_offset(),
                }
                if contract:
                    headers["x-xbl-contract-version"] = contract
                if rps:
                    headers["x-ms-rps-ticket"] = await self._msa_token(XAPPROVE_SCOPE)
            except XboxFamilyAuthError as err:
                self.state = "auth_failed"
                self.last_error = str(err)
                raise
            except XboxFamilyError as err:
                self.state = "error"
                self.last_error = str(err)
                raise
        session = async_get_clientsession(self._hass)
        try:
            async with session.request(
                method, url, json=json_body, headers=headers, timeout=_TIMEOUT
            ) as resp:
                status = resp.status
                text = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            self.state = "error"
            self.last_error = f"{method} {url.rsplit('/api', 1)[-1]}: {err!r}"
            raise XboxFamilyError(self.last_error) from err
        if status == 401:
            # Drop the cached tokens so the next call starts from the refresh token.
            self._xsts = None
            self._msa.pop(XAPPROVE_SCOPE, None)
        if status >= 400:
            self.state = "error"
            self.last_error = f"{method} {url.split('.com', 1)[-1]} answered {status}"
            raise XboxFamilyError(self.last_error)
        self.state = "ok"
        self.last_error = None
        self.last_success = dt_util.utcnow().isoformat()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError as err:
            raise XboxFamilyError(f"{method} {url} answered non-JSON") from err

    async def async_family(self) -> list[dict[str, Any]]:
        now = dt_util.utcnow()
        if self._family is not None and self._family_at and now - self._family_at < _FAMILY_TTL:
            return self._family
        await self._xbl_auth_locked()
        if not self._parent_xuid:
            raise XboxFamilyError("Xbox Live did not return the signed-in user's xuid")
        body = await self._request(
            "GET", _FAMILY_URL.format(xuid=self._parent_xuid), rps=False, contract="2"
        )
        self._family = [u for u in (body or {}).get("familyUsers") or [] if isinstance(u, dict)]
        self._family_at = now
        return self._family

    async def _xbl_auth_locked(self) -> None:
        async with self._lock:
            try:
                await self._xbl_auth()
            except XboxFamilyAuthError as err:
                self.state = "auth_failed"
                self.last_error = str(err)
                raise
            except XboxFamilyError as err:
                self.state = "error"
                self.last_error = str(err)
                raise

    async def async_child_xuid(self, first_name: str | None, surname: str | None) -> str | None:
        """Find a child's Xbox user id by the names on the Family Safety roster."""
        first = (first_name or "").strip().casefold()
        last = (surname or "").strip().casefold()
        if not first:
            return None
        matches = [
            u
            for u in await self.async_family()
            if str(u.get("role", "")).lower() == "child"
            and str(u.get("firstName", "")).strip().casefold() == first
            and (not last or str(u.get("lastName", "")).strip().casefold() == last)
        ]
        if len(matches) != 1:
            _LOGGER.debug(
                "Xbox family lookup for %s found %d matching children", first_name, len(matches)
            )
            return None
        return str(matches[0].get("xuid")) if matches[0].get("xuid") else None

    async def async_get_schedule(self, xuid: str) -> dict[str, Any]:
        return await self._request("GET", f"{_XAPPROVE}/v1/user/{xuid}/settings/screentime") or {}

    async def async_get_overrides(self, xuid: str) -> dict[str, Any]:
        return (
            await self._request("GET", f"{_XAPPROVE}/v1/user/{xuid}/settings/screentime/overrides")
            or {}
        )

    async def async_get_usage(self, xuid: str) -> dict[str, Any]:
        return (
            await self._request("GET", f"{_XAPPROVE}/v1/user/{xuid}/activityreport/screenTimeUsage")
            or {}
        )

    async def async_set_day(
        self,
        xuid: str,
        day_of_week: int,
        *,
        minutes: int | None = None,
        intervals: list[tuple[int, int]] | None = None,
        enabled: bool | None = None,
    ) -> None:
        """PATCH one day, keeping what is not given from the current schedule.

        ``minutes`` is the day's allowance, ``intervals`` its allowed time
        windows as (begin, end) minutes after midnight (end up to 1440). The
        app sends one whole day per PATCH with ``isEnabled``; the same body
        with another ``isEnabled`` turns the whole Xbox schedule on or off.
        """
        if intervals is not None:
            for begin, end in intervals:
                if not 0 <= begin < end <= 1440:
                    raise ValueError(f"invalid Xbox time window {begin}-{end}")
        async with self._day_lock:
            current = await self.async_get_schedule(xuid)
            day = next(
                (
                    d
                    for d in current.get("dailyRestrictions") or []
                    if d.get("dayOfWeek") == day_of_week
                ),
                None,
            )
            if day is None:
                raise XboxFamilyError(f"xapprove schedule has no day {day_of_week}")
            allowance = _hm(day.get("allowance")) if minutes is None else int(minutes)
            if intervals is None:
                intervals = [
                    (_hm(i.get("begin")), _hm(i.get("end")))
                    for i in day.get("allowedIntervals") or []
                    if isinstance(i, dict)
                ]

            def _hm_obj(total: int, *, days: bool) -> dict[str, int]:
                obj = {"hour": total // 60 % 24 if days else total // 60, "minute": total % 60}
                if days:
                    obj = {"days": total // 1440, **obj}
                return obj

            body = {
                "isEnabled": current.get("isEnabled", True) if enabled is None else bool(enabled),
                "dailyRestrictions": [
                    {
                        "dayOfWeek": day_of_week,
                        "allowance": _hm_obj(allowance, days=False),
                        "allowedIntervals": [
                            {
                                "begin": _hm_obj(begin, days=True),
                                "end": _hm_obj(end, days=True),
                            }
                            for begin, end in intervals
                        ],
                    }
                ],
            }
            await self._request(
                "PATCH", f"{_XAPPROVE}/v1/user/{xuid}/settings/screentime", json_body=body
            )

    async def async_create_override(
        self, xuid: str, override_type: int, valid_until: datetime
    ) -> dict[str, Any]:
        until = dt_util.as_utc(valid_until).isoformat(timespec="milliseconds")
        return (
            await self._request(
                "POST",
                f"{_XAPPROVE}/v1/user/{xuid}/settings/screentime/overrides/create",
                json_body={"overrideType": override_type, "target": 1, "validUntil": until},
            )
            or {}
        )

    def diagnostics(self) -> dict[str, Any]:
        return {
            "xbox_family": "not_linked" if not self.linked else self.state,
            "xbox_family_linked_via": self.linked_via,
            "xbox_family_last_error": self.last_error,
            "xbox_family_last_success": self.last_success,
        }

