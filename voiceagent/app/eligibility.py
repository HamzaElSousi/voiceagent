"""Who may be dialed right now. Pure function, no I/O, so it is easy to test and reason about.

Hard rules (no workspace setting turns these off):
  - outbound needs recorded call consent (TCPA "prior express consent" for AI voices, CRTC rules)
  - never dial a number on the workspace DNC list
  - only inside the callee's local calling window, intersected with the TCPA 8am-9pm bounds
  - stop after max_attempts tries
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from .. import phones

TCPA_EARLIEST = time(8, 0)
TCPA_LATEST = time(21, 0)


@dataclass(frozen=True)
class Eligibility:
    ok: bool
    reason: str = ""          # no_consent | dnc | invalid_phone | outside_hours | max_attempts
    detail: str = ""


def callee_zones(phone: str, lead_timezone: str = "", fallback_tz: str = "") -> list[str]:
    if lead_timezone:
        try:
            ZoneInfo(lead_timezone)
            return [lead_timezone]
        except Exception:
            pass
    zones = phones.timezones_for(phone)
    if zones:
        return zones
    return [fallback_tz] if fallback_tz else []


def within_hours(now: datetime, zones: Iterable[str], start: time, end: time) -> tuple[bool, str]:
    """Inside [max(start, 8am), min(end, 9pm)) local time in EVERY candidate zone."""

    lo = max(start, TCPA_EARLIEST)
    hi = min(end, TCPA_LATEST)
    zones = list(zones)
    if not zones:
        return False, "callee timezone unknown"
    for tz in zones:
        local = now.astimezone(ZoneInfo(tz)).time()
        if not (lo <= local < hi):
            return False, f"{local.strftime('%H:%M')} in {tz}"
    return True, ""


def check(
    *,
    phone: str,
    consent_at: Optional[datetime],
    dnc: set[str],
    now: datetime,
    hours_start: time,
    hours_end: time,
    lead_timezone: str = "",
    fallback_tz: str = "",
    attempts: int = 0,
    max_attempts: int = 3,
) -> Eligibility:
    e164 = phones.normalize(phone) if phone else None
    if e164 is None:
        return Eligibility(False, "invalid_phone")
    if e164 in dnc:
        return Eligibility(False, "dnc")
    if consent_at is None:
        return Eligibility(False, "no_consent")
    if attempts >= max_attempts:
        return Eligibility(False, "max_attempts")
    ok, detail = within_hours(now, callee_zones(e164, lead_timezone, fallback_tz), hours_start, hours_end)
    if not ok:
        return Eligibility(False, "outside_hours", detail)
    return Eligibility(True)
