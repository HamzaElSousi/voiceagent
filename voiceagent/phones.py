"""Phone numbers: E.164 normalization and the callee's timezone(s).

The timezone drives the calling-hours check, which must use the CALLEE's local time (TCPA:
8am to 9pm local). Some numbers map to several zones (e.g. an area code spanning a border); the
caller then has to be inside the allowed window in every one of them, the conservative choice.

phonenumbers loads its metadata lazily on first use, which is slow on cold disks; call
`warm_up()` at startup (in a thread) so the first API request does not stall the event loop.
"""

from __future__ import annotations

from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import phonenumbers
from phonenumbers import timezone as pn_timezone

_UNKNOWN_TZ = "Etc/Unknown"


def normalize(raw: str, default_region: str = "US") -> Optional[str]:
    """E.164 (+15555550100), or None when the number is not a valid phone number."""

    if not raw or not raw.strip():
        return None
    try:
        num = phonenumbers.parse(raw.strip(), default_region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(num):
        return None
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)


def timezones_for(e164: str) -> list[str]:
    """IANA zones a number may be in. Empty when unknown."""

    try:
        num = phonenumbers.parse(e164, None)
    except phonenumbers.NumberParseException:
        return []
    zones = []
    for tz in pn_timezone.time_zones_for_number(num):
        if tz == _UNKNOWN_TZ:
            continue
        try:
            ZoneInfo(tz)
        except ZoneInfoNotFoundError:
            continue
        zones.append(tz)
    return zones


def mask(e164: str) -> str:
    """For logs: +1555•••0100."""

    return e164[:5] + "•••" + e164[-4:] if len(e164) > 9 else "•••"


def warm_up() -> None:
    timezones_for("+12125550100")
    normalize("+12125550100")
