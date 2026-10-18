"""BookingFlow: the in-call conversation that turns "sure, let's meet" into a real calendar booking.

    agreement -> offer 2 real open slots -> contact picks one -> (capture + read back the email
    if we do not have it) -> reserve the slot -> confirm the time -> engine's two-beat close

The engine hands a contact turn to `handle()` while the flow is active. It either returns a text
stream to speak (the flow owns this turn) or None (the LLM should answer, grounded by `context()`,
e.g. when the contact asks a question in the middle of choosing).

Real-time rules: slots are prefetched as a background task at call start, so offering them costs
nothing on the turn. The booking request runs as its own task and is shielded from barge-in: if the
contact talks over "let me lock that in", the booking still completes and is still recorded.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import AsyncIterator, Optional, Sequence

from ..contracts import LLM, Booker, BookingRequest, BookingResult, CostMeter, Slot
from .email import parse_spoken_email, speak_email
from .slots import choose_offer, is_rejection, resolve_choice, speak_options, speak_slot, window

logger = logging.getLogger("voiceagent.booking")

_YES_RE = re.compile(
    r"\b(yes|yeah|yep|yup|sure|correct|right|that's (?:it|right|correct)|exactly|perfect|"
    r"works|sounds good|ok(?:ay)?|that one)\b"
)
_NO_RE = re.compile(r"\b(no|nope|wrong|not quite|incorrect|actually)\b")
_QUESTION_RE = re.compile(r"\?|\b(what|how|why|who|where|can you|could you|do you|is it|is there)\b")

_SLOT_FETCH_TIMEOUT = 4.0
_BOOK_TIMEOUT = 10.0
_MAX_OFFER_ROUNDS = 3
_MAX_EMAIL_ATTEMPTS = 2
_CLOSE_QUESTION = "Anything else before I let you go?"


class BookingFlow:
    def __init__(
        self,
        booker: Booker,
        *,
        timezone_name: str,
        contact: dict,
        offer_count: int = 2,
        lookahead_days: int = 7,
        min_notice_hours: float = 2.0,
        booking_url: str = "",
        extractor_llm: Optional[LLM] = None,
        cost_meter: Optional[CostMeter] = None,
        now=None,
    ) -> None:
        self.booker = booker
        self.tz = timezone_name
        self.contact = contact
        self.offer_count = offer_count
        self.lookahead_days = lookahead_days
        self.min_notice_hours = min_notice_hours
        self.booking_url = booking_url
        self.extractor_llm = extractor_llm
        self.cost_meter = cost_meter
        self._now = now or (lambda: datetime.now(timezone.utc))

        self.phase = "idle"          # idle | offering | email | email_confirm | booking | done
        self.confirmed = False       # the contact agreed and the close can begin
        self.booking_ref: Optional[str] = None
        self.booked_slot: Optional[Slot] = None
        self.error: Optional[str] = None

        self._slots: list[Slot] = []
        self._slots_task: Optional[asyncio.Task] = None
        self._offered: list[Slot] = []
        self._shown: list[Slot] = []   # every slot offered so far, so re-offers are new times
        self._rounds = 0
        self._chosen: Optional[Slot] = None
        self._email: str = (contact.get("email") or "").strip()
        self._pending_email: Optional[str] = None
        self._email_attempts = 0
        self._booking_task: Optional[asyncio.Task] = None

    # -- lifecycle ----------------------------------------------------------------------------

    @property
    def active(self) -> bool:
        """True while the flow owns the conversation (between offer and booking)."""

        return self.phase in ("offering", "email", "email_confirm", "booking")

    def start_prefetch(self) -> None:
        if self._slots_task is None:
            self._slots_task = asyncio.create_task(self._fetch_slots())

    async def _fetch_slots(self) -> list[Slot]:
        start, end = window(self._now(), self.lookahead_days, self.min_notice_hours)
        try:
            slots = await self.booker.get_slots(start, end, self.tz)
        except Exception as exc:  # a booker must not raise, but never trust that on a live call
            logger.warning("slot fetch failed: %r", exc)
            slots = []
        self._slots = [s for s in slots if s.start >= start]
        return self._slots

    async def _get_slots(self) -> list[Slot]:
        self.start_prefetch()
        assert self._slots_task is not None
        try:
            return await asyncio.wait_for(asyncio.shield(self._slots_task), _SLOT_FETCH_TIMEOUT)
        except (asyncio.TimeoutError, Exception):
            return list(self._slots)

    async def finish(self, timeout: float = 5.0) -> None:
        """At call end: give an in-flight booking a moment to land so its ref is recorded."""

        if self._booking_task is not None and not self._booking_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._booking_task), timeout)
            except (asyncio.TimeoutError, Exception):
                pass
        if self._slots_task is not None and not self._slots_task.done():
            self._slots_task.cancel()

    # -- prompt grounding ---------------------------------------------------------------------

    def context(self) -> str:
        """Extra system-prompt lines so the LLM stays consistent with the real calendar."""

        if self.phase == "offering" and self._offered:
            return (
                "\n\nBooking in progress: you just offered these real open times: "
                f"{speak_options(self._offered, self.tz, self._local_now())}. The contact has not "
                "picked one yet. Answer their question in one short sentence, then ask which of "
                "those times works. Never invent or confirm any other time."
            )
        if self.phase in ("email", "email_confirm"):
            return (
                "\n\nBooking in progress: you need the contact's email address to send the "
                "calendar invite. Answer briefly, then ask for their email."
            )
        if self.phase == "idle":
            return (
                "\n\nYou can book real calendar times. When they agree to meet, the system reads "
                "out actual open times, so never promise a specific day or time yourself."
            )
        return ""

    # -- entry points -------------------------------------------------------------------------

    def offer(self) -> AsyncIterator[str]:
        """The contact agreed to meet: offer real open slots."""

        self.phase = "offering"
        return self._offer_stream(prefix="Great.")

    async def handle(self, text: str) -> Optional[AsyncIterator[str]]:
        low = text.lower().strip()
        if self.phase == "offering":
            return await self._handle_offering(low)
        if self.phase == "email":
            return self._handle_email(low)
        if self.phase == "email_confirm":
            return self._handle_email_confirm(low)
        if self.phase == "booking":
            return None  # booking in flight; let the LLM fill the moment
        return None

    # -- phases -------------------------------------------------------------------------------

    async def _handle_offering(self, low: str) -> Optional[AsyncIterator[str]]:
        idx = resolve_choice(low, self._offered, self.tz, self._local_now())
        if idx is None and len(self._offered) == 1 and _YES_RE.search(low) and not _NO_RE.search(low):
            idx = 0
        if idx is None and is_rejection(low):
            return self._offer_stream(prefix="No problem.")
        if idx is None and not _QUESTION_RE.search(low):
            idx = await self._extract_choice(low)
        if idx is None:
            return None
        self._chosen = self._offered[idx]
        if self._email:
            return self._book_stream()
        self.phase = "email"
        return self._say(
            f"{_cap(speak_slot(self._chosen, self.tz, self._local_now()))} it is. "
            "What's the best email to send the invite to?"
        )

    def _handle_email(self, low: str) -> AsyncIterator[str]:
        email = parse_spoken_email(low)
        if email:
            self._pending_email = email
            self.phase = "email_confirm"
            return self._say(f"Got it, {speak_email(email)}. Is that right?")
        self._email_attempts += 1
        if self._email_attempts >= _MAX_EMAIL_ATTEMPTS:
            return self._follow_up_close()
        return self._say("Sorry, I didn't catch that. Could you spell the email for me?")

    def _handle_email_confirm(self, low: str) -> AsyncIterator[str]:
        restated = parse_spoken_email(low)
        if restated and restated != self._pending_email:
            self._pending_email = restated
            return self._say(f"Okay, {speak_email(restated)}. Is that right?")
        if _YES_RE.search(low) and not _NO_RE.search(low):
            self._email = self._pending_email or ""
            return self._book_stream()
        self.phase = "email"
        self._email_attempts += 1
        if self._email_attempts >= _MAX_EMAIL_ATTEMPTS + 1:
            return self._follow_up_close()
        return self._say("Sorry about that. Could you spell it out for me, letter by letter?")

    # -- streams ------------------------------------------------------------------------------

    async def _say(self, text: str) -> AsyncIterator[str]:
        yield text

    async def _offer_stream(self, prefix: str) -> AsyncIterator[str]:
        slots = self._slots if (self._slots_task and self._slots_task.done()) else None
        if slots is None:
            yield f"{prefix} Let me check the calendar."
            prefix = ""
            slots = await self._get_slots()
        self._rounds += 1
        options = (
            choose_offer(slots, self.offer_count, exclude=self._shown)
            if self._rounds <= _MAX_OFFER_ROUNDS
            else []
        )
        if not options:
            self.phase = "done"
            self.confirmed = True
            link = " to pick a time that works for you" if self.booking_url else ""
            yield _join(prefix, f"I'll send you a link{link}, so you can grab whatever suits. {_CLOSE_QUESTION}")
            return
        self.phase = "offering"
        self._offered = options
        self._shown.extend(options)
        spoken = speak_options(options, self.tz, self._local_now())
        if len(options) == 1:
            yield _join(prefix, f"I have {spoken}. Does that work for you?")
        else:
            yield _join(prefix, f"I have {spoken}. Which works better for you?")

    async def _book_stream(self) -> AsyncIterator[str]:
        assert self._chosen is not None
        slot = self._chosen
        self.phase = "booking"
        spoken = speak_slot(slot, self.tz, self._local_now())
        yield "Perfect, let me lock that in."
        request = BookingRequest(
            slot=slot,
            name=self.contact.get("name", "") or "Guest",
            email=self._email,
            timezone=self.tz,
            phone=self.contact.get("phone", ""),
            notes="Booked by phone with the AI assistant.",
        )
        task = asyncio.create_task(self._safe_book(request))
        task.add_done_callback(lambda t, s=slot: self._on_booked(t, s))
        self._booking_task = task
        try:
            result = await asyncio.wait_for(asyncio.shield(task), _BOOK_TIMEOUT)
        except asyncio.TimeoutError:
            result = BookingResult(ok=False, error="booking timed out")

        if result.ok:
            yield f" You're all set for {spoken}. The invite is on its way to your inbox. {_CLOSE_QUESTION}"
            return
        if result.slot_taken:
            self._slots = [s for s in self._slots if s.start != slot.start]
            nxt = choose_offer(self._slots, 1, exclude=self._shown)
            if nxt:
                self.phase = "offering"
                self._offered = nxt
                self._shown.extend(nxt)
                yield (
                    " Ah, someone just grabbed that one. How about "
                    f"{speak_slot(nxt[0], self.tz, self._local_now())}?"
                )
                return
            self.phase = "done"
            self.confirmed = True
            yield f" Ah, that one just got taken, so I'll email you a link to pick a new time. {_CLOSE_QUESTION}"
            return
        # Calendar error: keep the agreement and follow up rather than lose the meeting.
        self.phase = "done"
        self.confirmed = True
        self.booked_slot = slot
        yield f" The calendar is being slow, so I'll email you to confirm {spoken}. {_CLOSE_QUESTION}"

    async def _safe_book(self, request: BookingRequest) -> BookingResult:
        try:
            return await self.booker.create_booking(request)
        except Exception as exc:
            return BookingResult(ok=False, error=repr(exc))

    def _on_booked(self, task: asyncio.Task, slot: Slot) -> None:
        """Runs even if the speaking stream was cancelled by barge-in, so a booking that landed is
        always recorded."""

        if task.cancelled():
            return
        result = task.result()
        if result.ok:
            self.booking_ref = result.booking_ref
            self.booked_slot = slot
            self.confirmed = True
            self.phase = "done"
        else:
            self.error = result.error

    def _follow_up_close(self) -> AsyncIterator[str]:
        self.phase = "done"
        self.confirmed = True
        self.booked_slot = self._chosen
        when = speak_slot(self._chosen, self.tz, self._local_now()) if self._chosen else "a time"
        return self._say(
            f"No worries, I'll have someone reach out to confirm {when}. {_CLOSE_QUESTION}"
        )

    # -- helpers ------------------------------------------------------------------------------

    def _local_now(self) -> datetime:
        return self._now()

    async def _extract_choice(self, low: str) -> Optional[int]:
        """Ask the LLM which offered slot the contact meant, when the deterministic resolver could
        not tell. Returns None on any doubt so we never book the wrong time."""

        if self.extractor_llm is None or not self._offered:
            return None
        numbered = "; ".join(
            f"{i + 1}) {speak_slot(s, self.tz, self._local_now())}" for i, s in enumerate(self._offered)
        )
        system = (
            "You map a phone reply to one of the offered meeting times. Reply with ONLY the number "
            "of the time the person clearly chose, or 0 if they did not clearly choose one."
        )
        messages = [{"role": "user", "content": f"Offered times: {numbered}\nReply: \"{low}\""}]
        try:
            out = await asyncio.wait_for(_collect(self.extractor_llm, messages, system), 3.0)
        except Exception:
            return None
        if self.cost_meter is not None:
            try:
                self.cost_meter.add_llm_usage(self.extractor_llm.last_usage())
            except Exception:
                pass
        m = re.search(r"\d+", out)
        if not m:
            return None
        n = int(m.group(0))
        return n - 1 if 1 <= n <= len(self._offered) else None


async def _collect(llm: LLM, messages: Sequence[dict], system: str) -> str:
    parts: list[str] = []
    async for delta in llm.stream(messages, system=system):
        parts.append(delta)
    return "".join(parts)


def _join(prefix: str, text: str) -> str:
    return f"{prefix} {text}".strip() if prefix else text


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]
