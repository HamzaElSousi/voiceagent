"""Pure helpers for offering calendar slots by voice: choosing which slots to offer, saying them
the way a person would ("tomorrow at 2:30 PM"), and working out which one the contact picked from
what they said ("the first one", "Tuesday", "ten works").

No I/O here, so it is cheap to call on the call path and fully unit-testable.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

from ..contracts import Slot

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "noon": 12,
}

_ORDINALS = [
    (re.compile(r"\b(first|1st|earlier|earliest|former|sooner|soonest)\b"), 0),
    (re.compile(r"\b(second|2nd|latter|later one|the later)\b"), 1),
    (re.compile(r"\b(third|3rd|last one)\b"), 2),
]

# "Neither of those works" style replies: offer different times instead.
_REJECT_RE = re.compile(
    r"\b(neither|none of (?:those|them|these)|(?:doesn't|does not|don't|do not|won't|wouldn't) work|"
    r"something else|another (?:time|day)|other times?|different (?:time|day)|"
    r"not (?:those|then|that day)|busy (?:then|both))\b"
)

_TIME_RE = re.compile(
    r"\b(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|noon)"
    r"(?::(\d{2})|\s+(thirty|fifteen|forty[- ]five))?\s*(a\.?m\.?|p\.?m\.?)?\b(?!\s*min)"
)

# "the first one", "that one", "either one": the word "one" there is not 1 o'clock.
_PRONOUN_ONE_RE = re.compile(
    r"(?<!at )\bone\b(?!\s*(?:a\.?m|p\.?m|o'?clock|thirty|fifteen|forty))"
)


def choose_offer(
    slots: Sequence[Slot], count: int, exclude: Sequence[Slot] = (), min_gap_hours: float = 3.0
) -> list[Slot]:
    """Pick `count` slots to offer, earliest first but spread out (a different day or at least
    `min_gap_hours` apart), so the two options are genuinely different choices."""

    taken = {s.start for s in exclude}
    pool = [s for s in sorted(slots, key=lambda s: s.start) if s.start not in taken]
    picked: list[Slot] = []
    for slot in pool:
        if len(picked) >= count:
            break
        if all(
            slot.start.date() != p.start.date()
            or abs((slot.start - p.start).total_seconds()) >= min_gap_hours * 3600
            for p in picked
        ):
            picked.append(slot)
    # Not enough spread-out options: fill with whatever is left.
    for slot in pool:
        if len(picked) >= count:
            break
        if slot not in picked:
            picked.append(slot)
    return sorted(picked, key=lambda s: s.start)


def _clock(dt: datetime) -> str:
    hour = dt.hour % 12 or 12
    suffix = "AM" if dt.hour < 12 else "PM"
    if dt.hour == 12 and dt.minute == 0:
        return "noon"
    return f"{hour}:{dt.minute:02d} {suffix}" if dt.minute else f"{hour} {suffix}"


def speak_slot(slot: Slot, timezone: str, now: Optional[datetime] = None) -> str:
    """Say a slot like a person would, in the listener's timezone: "today at 3 PM",
    "tomorrow at 10:30 AM", "Thursday at 2 PM", or "Monday the 3rd at 9 AM" when over a week out."""

    tz = ZoneInfo(timezone)
    local = slot.start.astimezone(tz)
    today = (now or datetime.now(tz)).astimezone(tz).date()
    days = (local.date() - today).days
    if days == 0:
        day = "today"
    elif days == 1:
        day = "tomorrow"
    elif 1 < days < 7:
        day = local.strftime("%A")
    else:
        day = f"{local.strftime('%A')} the {_ordinal(local.day)}"
    return f"{day} at {_clock(local)}"


def _ordinal(n: int) -> str:
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def speak_options(slots: Sequence[Slot], timezone: str, now: Optional[datetime] = None) -> str:
    spoken = [speak_slot(s, timezone, now) for s in slots]
    if len(spoken) == 1:
        return spoken[0]
    return ", ".join(spoken[:-1]) + " or " + spoken[-1]


def is_rejection(text: str) -> bool:
    return bool(_REJECT_RE.search(text.lower()))


def resolve_choice(
    text: str, offered: Sequence[Slot], timezone: str, now: Optional[datetime] = None
) -> Optional[int]:
    """Index into `offered` of the slot the contact picked, or None when it is not clear.

    Deterministic and conservative: it only answers when exactly one offered slot matches, so an
    ambiguous reply falls through to the LLM instead of booking the wrong time."""

    low = text.lower()
    if not offered or is_rejection(low):
        return None
    low = _PRONOUN_ONE_RE.sub("", low)

    tz = ZoneInfo(timezone)
    local = [s.start.astimezone(tz) for s in offered]
    today = (now or datetime.now(tz)).astimezone(tz).date()

    candidates = set(range(len(offered)))
    matched_any = False

    # Day words: "today", "tomorrow", weekday names.
    day_hits: set[int] = set()
    for i, dt in enumerate(local):
        days = (dt.date() - today).days
        if ("today" in low and days == 0) or ("tomorrow" in low and days == 1):
            day_hits.add(i)
        if _WEEKDAYS[dt.weekday()] in low:
            day_hits.add(i)
    if day_hits:
        candidates &= day_hits
        matched_any = True

    # Times: "10", "ten", "2:30", "two thirty", "noon", with optional am/pm.
    time_hits: set[int] = set()
    for m in _TIME_RE.finditer(low):
        hour_word, minute_digits, minute_word, ampm = m.groups()
        hour = int(hour_word) if hour_word.isdigit() else _NUMBER_WORDS[hour_word]
        if hour > 23:
            continue
        minute = int(minute_digits) if minute_digits else _minute_word(minute_word)
        for i, dt in enumerate(local):
            if dt.minute != minute and (minute_digits or minute_word):
                continue
            if _hour_matches(hour, ampm, dt.hour):
                time_hits.add(i)
    if time_hits:
        candidates &= time_hits
        matched_any = True

    if not matched_any:
        for pattern, index in _ORDINALS:
            if pattern.search(low) and index < len(offered):
                return index
        return None

    return candidates.pop() if len(candidates) == 1 else None


def _minute_word(word: Optional[str]) -> int:
    if not word:
        return 0
    return {"fifteen": 15, "thirty": 30}.get(word, 45)


def _hour_matches(spoken: int, ampm: Optional[str], hour24: int) -> bool:
    if ampm:
        pm = ampm.startswith("p")
        h = spoken % 12 + (12 if pm else 0)
        return h == hour24
    if spoken > 12:
        return spoken == hour24
    return spoken % 12 == hour24 % 12


def window(now: datetime, lookahead_days: int, min_notice_hours: float) -> tuple[datetime, datetime]:
    """The [start, end) range to ask the calendar for."""

    start = now + timedelta(hours=min_notice_hours)
    return start, now + timedelta(days=lookahead_days)
