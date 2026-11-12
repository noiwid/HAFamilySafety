"""Pure screen-time rules: platform overrides (locks and extra time) as
Microsoft reports them, and whether a platform is allowed now, for how long,
and downtime.

No Home Assistant state and no I/O here, so every rule can be unit tested.
The allowance inputs are what the coordinator already polls: a platform's
schedule (``screentime_policy`` for Windows, ``xbox_screentime_policy`` for
the Xbox), today's usage, and the override in force.

An override is kept as ``{"type": ..., "valid_until": ...}``:

* ``blockUntil`` -- the platform is locked until ``valid_until`` (UTC ISO, or
  None when the end is not known).
* ``allowUntil`` -- extra time granted until ``valid_until``.
* ``none`` -- Microsoft answered and nothing is in force (a cancel, an
  expired override, or no override at all).
* ``unknown`` -- only used for a saved prior state that could not be read.

``None`` (not a dict) means the answer could not be read; callers treat that
as unknown, never as "not blocked".
"""
from __future__ import annotations

from datetime import datetime, timedelta
import re
from typing import Any

from homeassistant.util import dt as dt_util

from .mobile_overrides import mobile_platform_name
from .xbox_family_api import OVERRIDE_ALLOW, OVERRIDE_BLOCK, OVERRIDE_REVOKE

BLOCK_UNTIL = "blockUntil"
ALLOW_UNTIL = "allowUntil"
NO_OVERRIDE = "none"
UNKNOWN = "unknown"

#: Override types of the mobile API (pyfamilysafety writes "BlockUntil" and
#: "Cancel"), lower-cased.
_MOBILE_TYPES = {"blockuntil": BLOCK_UNTIL, "allowuntil": ALLOW_UNTIL, "cancel": NO_OVERRIDE}
_MOBILE_TYPE_KEYS = ("overrideType", "screenTimeOverrideType", "type")
#: Override types of the Xbox Family Settings service (xapprove).
_XBOX_TYPES = {OVERRIDE_BLOCK: BLOCK_UNTIL, OVERRIDE_ALLOW: ALLOW_UNTIL, OVERRIDE_REVOKE: NO_OVERRIDE}

_DAY_KEYS = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")

REASON_LOCKED = "locked"
REASON_EXTRA_TIME = "extra_time"
REASON_NO_LIMITS = "no_limits"
REASON_OUTSIDE_HOURS = "outside_allowed_hours"
REASON_ALLOWANCE_USED = "allowance_used"
REASON_WITHIN_LIMITS = "within_limits"
REASON_UNKNOWN = "unknown"


def parse_utc(value: Any) -> datetime | None:
    """An ISO date-time as aware UTC; None when missing or unreadable."""
    if isinstance(value, datetime):
        parsed: datetime | None = value
    elif isinstance(value, str) and value:
        parsed = dt_util.parse_datetime(value.replace("Z", "+00:00"))
    else:
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.UTC)
    return dt_util.as_utc(parsed)


def iso_utc(value: datetime) -> str:
    """UTC ISO text as Microsoft writes it (milliseconds, trailing Z)."""
    return dt_util.as_utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def override(kind: str, valid_until: datetime | None = None) -> dict[str, Any]:
    """An override record."""
    return {
        "type": kind,
        "valid_until": iso_utc(valid_until) if valid_until is not None else None,
    }


def _entry(kind: str | None, valid_until: Any, now: datetime) -> dict[str, Any] | None:
    """One override, normalised; None if it cannot be read."""
    if kind is None:
        return None
    if kind == NO_OVERRIDE:
        return override(NO_OVERRIDE)
    until = parse_utc(valid_until)
    if until is None:
        return None
    if until <= now:
        return override(NO_OVERRIDE)
    return override(kind, until)


_RANK = {NO_OVERRIDE: 0, ALLOW_UNTIL: 1, BLOCK_UNTIL: 2}
_NEVER = datetime.min.replace(tzinfo=dt_util.UTC)


def _weight(entry: dict[str, Any]) -> tuple[int, datetime]:
    return _RANK[entry["type"]], parse_utc(entry["valid_until"]) or _NEVER


def _strongest(entries: list[dict[str, Any] | None]) -> dict[str, Any] | None:
    """The override in force among several.

    A lock wins over extra time, extra time over nothing, and of two of a
    kind the one ending last; any unreadable entry makes it unknown.
    """
    if any(entry is None for entry in entries):
        return None
    return max(entries, key=_weight, default=override(NO_OVERRIDE))


def parse_mobile_override(
    payload: Any, platform: str, now: datetime | None = None
) -> dict[str, Any] | None:
    """The override in force for ``platform`` in the mobile API's answer.

    ``GET /v4/devicelimits/{id}/overrides`` answers
    ``{"lockablePlatforms": [{"appliesTo": "Windows", "overrides": [...]}]}``;
    a platform with an empty ``overrides`` list has nothing in force. Windows
    may be listed as "Desktop" (see mobile_overrides.mobile_platform_name).
    """
    now = now or dt_util.utcnow()
    if not isinstance(payload, dict) or not isinstance(payload.get("lockablePlatforms"), list):
        return None
    entries: list[dict[str, Any] | None] = []
    for item in payload["lockablePlatforms"]:
        if (
            not isinstance(item, dict)
            or mobile_platform_name(item.get("appliesTo")).lower() != platform.lower()
        ):
            continue
        for raw in item.get("overrides") or []:
            if not isinstance(raw, dict):
                entries.append(None)
                continue
            raw_type = next((raw[k] for k in _MOBILE_TYPE_KEYS if raw.get(k) is not None), None)
            entries.append(_entry(_MOBILE_TYPES.get(str(raw_type or "").lower()), raw.get("validUntil"), now))
    return _strongest(entries)


def parse_xbox_override(payload: Any, now: datetime | None = None) -> dict[str, Any] | None:
    """The override in force in the Xbox Family Settings answer.

    ``GET .../settings/screentime/overrides`` answers
    ``{"overrides": [{"overrideType": 2, "validUntil": ...}]}`` (2 = block,
    1 = extra time, 100 = a release).
    """
    now = now or dt_util.utcnow()
    if not isinstance(payload, dict):
        return None
    raw_entries = payload.get("overrides")
    if raw_entries is None:
        raw_entries = []
    if not isinstance(raw_entries, list):
        return None
    return _strongest([
        _entry(_XBOX_TYPES.get(raw.get("overrideType")), raw.get("validUntil"), now)
        if isinstance(raw, dict) else None
        for raw in raw_entries
    ])


def xbox_policy_override(policy: Any) -> dict[str, Any] | None:
    """The blockUntil / allowUntil in an Xbox policy, if any.

    Reads ``screenTimeOverride`` as the coordinator keeps it on
    ``xbox_policy`` (also wrapped in ``data``). None when there is no
    override, or one of another kind.
    """
    if not isinstance(policy, dict):
        return None
    data = policy.get("data") if isinstance(policy.get("data"), dict) else policy
    entry = data.get("screenTimeOverride")
    if not isinstance(entry, dict):
        return None
    kind = {"blockuntil": BLOCK_UNTIL, "allowuntil": ALLOW_UNTIL}.get(
        str(entry.get("screenTimeOverrideType") or "").lower()
    )
    if kind is None:
        return None
    return override(kind, parse_utc(entry.get("validUntil")))


def active_until(record: dict[str, Any] | None, now: datetime | None = None) -> datetime | None:
    """The expiry of a blockUntil/allowUntil record still in the future, else None."""
    if not isinstance(record, dict) or record.get("type") not in (BLOCK_UNTIL, ALLOW_UNTIL):
        return None
    until = parse_utc(record.get("valid_until"))
    if until is None or until <= (now or dt_util.utcnow()):
        return None
    return until


def parse_allowance_minutes(allowance: str | int | float | None) -> int:
    """Microsoft allowance values ("HH:MM:SS", "1.00:00:00", "PT2H30M") in minutes."""
    if allowance is None:
        return 0
    if isinstance(allowance, (int, float)):
        return int(allowance)
    try:
        text = str(allowance).strip()
        if text.upper().startswith("PT"):
            match = re.fullmatch(
                r"PT(?:(\d+)H)?(?:(\d+)M)?(?:\d+(?:\.\d+)?S)?",
                text.upper(),
            )
            if not match:
                return 0
            return int(match.group(1) or 0) * 60 + int(match.group(2) or 0)
        days = 0
        head, _, rest = text.partition(":")
        if "." in head:
            # .NET TimeSpan "d.hh:mm:ss": "1.00:00:00" is a whole day.
            day_part, head = head.split(".", 1)
            days = int(day_part)
        parts = [head, *rest.split(":")] if rest else [head]
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0
        return days * 1440 + hours * 60 + minutes
    except (TypeError, ValueError, IndexError):
        return 0


def _clock_minutes(value: Any) -> int | None:
    """"HH:MM[:SS]" or .NET "1.00:00:00" (midnight at the end) to minutes."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    days = 0
    head = text.split(":")[0]
    if "." in head:
        day_part, text = text.split(".", 1)
        try:
            days = int(day_part or 0)
        except ValueError:
            return None
    try:
        parts = [int(float(p)) for p in text.split(":")[:2]] + [0]
    except ValueError:
        return None
    total = days * 1440 + parts[0] * 60 + parts[1]
    return total if 0 <= total <= 1440 else None


def day_slots(day: dict[str, Any]) -> list[bool] | None:
    """The day's 48 half-hour slots; None when the day carries no windows."""
    for key in ("timeline", "allowedIntervals", "AllowedIntervals"):
        value = day.get(key)
        if isinstance(value, list) and len(value) == 48 and all(
            isinstance(item, bool) for item in value
        ):
            return list(value)
    for key in ("allowedIntervals", "AllowedIntervals", "allottedIntervals"):
        value = day.get(key)
        if not isinstance(value, list):
            continue
        slots = [False] * 48
        for item in value:
            if not isinstance(item, dict):
                continue
            begin = _clock_minutes(
                item.get("begin") or item.get("Begin") or item.get("start") or item.get("Start")
            )
            end = _clock_minutes(item.get("end") or item.get("End"))
            if begin is None or end is None or end <= begin:
                continue
            for slot in range(begin // 30, min(48, -(-end // 30))):
                slots[slot] = True
        return slots
    return None


def _day_of(policy: dict[str, Any], day: datetime) -> dict[str, Any] | None:
    """The schedule of ``day``'s weekday; None when the policy has none."""
    daily = policy.get("dailyRestrictions") or policy.get("DailyRestrictions")
    if not isinstance(daily, dict):
        return None
    key = _DAY_KEYS[(day.weekday() + 1) % 7]
    value = daily.get(key) or daily.get(key.capitalize())
    return value if isinstance(value, dict) else None


def _limits_off(policy: dict[str, Any]) -> bool:
    return policy.get("isEnabled", policy.get("IsEnabled")) is False


def platform_allowance(
    policy: dict[str, Any] | None,
    used_minutes: int | None,
    now: datetime,
    current_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Whether a platform may be used at ``now`` (local time), and for how long.

    Takes, in this order: an active override (a blockUntil locks, an
    allowUntil grants time until it ends), limits turned off, today's allowed
    hours, then today's allowance against today's usage. The minutes left are
    the smaller of the allowance left and the time to the end of the current
    allowed window (midnight at the latest); 0 whenever not allowed.

    Returns ``allowed`` (None when unknown), ``remaining_minutes`` (None when
    unknown), ``reason`` and the inputs used, for the entities' attributes.
    """
    minute_of_day = now.hour * 60 + now.minute + now.second / 60
    to_midnight = int(1440 - minute_of_day)
    result: dict[str, Any] = {
        "allowed": None,
        "remaining_minutes": None,
        "reason": REASON_UNKNOWN,
        "allowance_minutes": None,
        "used_minutes": used_minutes,
        "in_allowed_hours": None,
        "override": current_override,
    }

    kind = (current_override or {}).get("type")
    if kind == BLOCK_UNTIL and (
        current_override.get("valid_until") is None
        or active_until(current_override, dt_util.as_utc(now)) is not None
    ):
        return {**result, "allowed": False, "remaining_minutes": 0, "reason": REASON_LOCKED}
    if kind == ALLOW_UNTIL:
        until = active_until(current_override, dt_util.as_utc(now))
        if until is not None:
            left = int((until - dt_util.as_utc(now)).total_seconds() // 60)
            return {
                **result, "allowed": True, "remaining_minutes": min(left, to_midnight),
                "reason": REASON_EXTRA_TIME,
            }

    if not isinstance(policy, dict):
        return result
    if _limits_off(policy):
        return {
            **result, "allowed": True, "remaining_minutes": to_midnight,
            "reason": REASON_NO_LIMITS,
        }
    day = _day_of(policy, now)
    if day is None:
        return result

    allowance = parse_allowance_minutes(day.get("allowance", day.get("Allowance")))
    result["allowance_minutes"] = allowance
    slots = day_slots(day) or [True] * 48
    slot = min(int(minute_of_day // 30), 47)
    in_hours = slots[slot]
    result["in_allowed_hours"] = in_hours
    if not in_hours:
        return {**result, "allowed": False, "remaining_minutes": 0, "reason": REASON_OUTSIDE_HOURS}
    end_slot = slot
    while end_slot < 48 and slots[end_slot]:
        end_slot += 1
    to_window_end = int(end_slot * 30 - minute_of_day)

    if allowance >= 1440:
        left = to_window_end
    elif used_minutes is None:
        return result
    else:
        left = max(allowance - int(used_minutes), 0)
    if left <= 0:
        return {**result, "allowed": False, "remaining_minutes": 0, "reason": REASON_ALLOWANCE_USED}
    return {
        **result, "allowed": True, "remaining_minutes": min(left, to_window_end),
        "reason": REASON_WITHIN_LIMITS,
    }


def downtime_window(policy: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    """Downtime (outside the allowed hours) now, or the next one.

    Returns ``active`` (None when the schedule is unknown) and ``start`` /
    ``end`` (local, aware) of the current downtime, or of the next one within
    the next day; both None when there is none. With limits off there is no
    downtime. A day without allowed-hour information counts as allowed.
    """
    result: dict[str, Any] = {"active": None, "start": None, "end": None}
    if not isinstance(policy, dict):
        return result
    if _limits_off(policy):
        return {**result, "active": False}
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    days = [midnight + timedelta(days=offset) for offset in (-1, 0, 1)]
    if _day_of(policy, days[1]) is None:
        return result
    timeline: list[bool] = []
    for day in days:
        data = _day_of(policy, day)
        timeline.extend((day_slots(data) if data else None) or [True] * 48)
    index = 48 + min(int((now.hour * 60 + now.minute) // 30), 47)

    def at(slot: int) -> datetime:
        return days[0] + timedelta(minutes=30 * slot)

    if not timeline[index]:
        start = index
        while start > 0 and not timeline[start - 1]:
            start -= 1
        end = index
        while end < len(timeline) and not timeline[end]:
            end += 1
        return {"active": True, "start": at(start), "end": at(end)}
    start = index
    while start < len(timeline) and timeline[start]:
        start += 1
    if start >= len(timeline):
        return {**result, "active": False}
    end = start
    while end < len(timeline) and not timeline[end]:
        end += 1
    return {"active": False, "start": at(start), "end": at(end)}
