"""Cal.com API v2 booker: read open slots and create bookings for one event type.

Fully async (httpx) so it is safe on the call path. Every expected failure (slot taken, bad key,
timeout, Cal.com down) comes back as a BookingResult / empty slot list rather than an exception,
because a calendar hiccup must never crash or dead-air a live call.

Docs: https://cal.com/docs/api-reference/v2
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from ..contracts import BookingRequest, BookingResult, Slot

logger = logging.getLogger("voiceagent.booking.calcom")

API_BASE = "https://api.cal.com/v2"
# Cal.com versions each v2 endpoint family by header.
_SLOTS_VERSION = "2024-09-04"
_BOOKINGS_VERSION = "2024-08-13"

# Phrases in a Cal.com error that mean "someone else got this slot first".
_TAKEN_HINTS = ("not available", "already has booking", "no available users", "slot", "booked")


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class CalcomBooker:
    def __init__(
        self,
        api_key: str,
        *,
        event_type_id: Optional[int] = None,
        username: Optional[str] = None,
        event_type_slug: Optional[str] = None,
        base_url: str = API_BASE,
        timeout: float = 8.0,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        if not (event_type_id or (username and event_type_slug)):
            raise ValueError("CalcomBooker needs event_type_id, or username + event_type_slug")
        self._api_key = api_key
        self._event_type_id = event_type_id
        self._username = username
        self._slug = event_type_slug
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._client = client

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    def _headers(self, version: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "cal-api-version": version}

    def _event_type(self) -> dict[str, Any]:
        if self._event_type_id:
            return {"eventTypeId": self._event_type_id}
        return {"username": self._username, "eventTypeSlug": self._slug}

    async def get_slots(self, start: datetime, end: datetime, timezone: str) -> list[Slot]:
        params = {**self._event_type(), "start": _iso_utc(start), "end": _iso_utc(end), "timeZone": timezone}
        try:
            resp = await self._http().get(
                f"{self._base}/slots", params=params, headers=self._headers(_SLOTS_VERSION)
            )
        except httpx.HTTPError as exc:
            logger.warning("cal.com slots request failed: %r", exc)
            return []
        if resp.status_code != 200:
            logger.warning("cal.com slots returned %s: %s", resp.status_code, resp.text[:300])
            return []
        data = (resp.json() or {}).get("data") or {}
        slots: list[Slot] = []
        # data is {"YYYY-MM-DD": [{"start": "...", "end"?: "..."}, ...], ...}
        for day in data.values():
            for item in day or []:
                raw_start = item.get("start") if isinstance(item, dict) else item
                if not raw_start:
                    continue
                raw_end = item.get("end") if isinstance(item, dict) else None
                slots.append(
                    Slot(start=_parse_dt(raw_start), end=_parse_dt(raw_end) if raw_end else None)
                )
        return sorted(slots, key=lambda s: s.start)

    async def create_booking(self, request: BookingRequest) -> BookingResult:
        attendee: dict[str, Any] = {
            "name": request.name or "Guest",
            "email": request.email,
            "timeZone": request.timezone,
        }
        if request.phone:
            attendee["phoneNumber"] = request.phone
        body: dict[str, Any] = {
            "start": _iso_utc(request.slot.start),
            "attendee": attendee,
            **self._event_type(),
        }
        if request.notes:
            body["bookingFieldsResponses"] = {"notes": request.notes}
        try:
            resp = await self._http().post(
                f"{self._base}/bookings", json=body, headers=self._headers(_BOOKINGS_VERSION)
            )
        except httpx.HTTPError as exc:
            return BookingResult(ok=False, error=f"request failed: {exc!r}")
        payload = _json(resp)
        if resp.status_code in (200, 201) and payload.get("status") == "success":
            data = payload.get("data") or {}
            ref = str(data.get("uid") or data.get("id") or "")
            return BookingResult(ok=True, booking_ref=ref)
        message = _error_message(payload) or resp.text[:300]
        taken = resp.status_code in (400, 409) and any(h in message.lower() for h in _TAKEN_HINTS)
        return BookingResult(ok=False, error=f"{resp.status_code}: {message}", slot_taken=taken)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()


def _json(resp: httpx.Response) -> dict:
    try:
        value = resp.json()
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _error_message(payload: dict) -> str:
    err = payload.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("details") or "")
    return str(err or payload.get("message") or "")
