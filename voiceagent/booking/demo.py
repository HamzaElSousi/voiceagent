"""An in-memory booker with synthetic slots, for mocked runs (`test-voice --book`) and demos.
No network. Slots are generated on the hour, inside working hours, over the next few days."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..contracts import BookingRequest, BookingResult, Slot


class DemoBooker:
    def __init__(self, timezone: str = "America/New_York", hours: tuple[int, ...] = (10, 14, 16)):
        self._tz = ZoneInfo(timezone)
        self._hours = hours
        self.bookings: list[BookingRequest] = []
        self.taken: set[datetime] = set()

    async def get_slots(self, start: datetime, end: datetime, timezone: str) -> list[Slot]:
        slots: list[Slot] = []
        day = start.astimezone(self._tz).replace(hour=0, minute=0, second=0, microsecond=0)
        while day < end:
            if day.weekday() < 5:
                for h in self._hours:
                    s = day.replace(hour=h)
                    if start <= s < end and s not in self.taken:
                        slots.append(Slot(start=s, end=s + timedelta(minutes=15)))
            day += timedelta(days=1)
        return slots

    async def create_booking(self, request: BookingRequest) -> BookingResult:
        if request.slot.start in self.taken:
            return BookingResult(ok=False, error="slot taken", slot_taken=True)
        self.taken.add(request.slot.start)
        self.bookings.append(request)
        return BookingResult(ok=True, booking_ref=f"demo_{uuid.uuid4().hex[:8]}")
