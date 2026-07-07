"""ConversationEngine: the brain of a call. LLM-driven with deterministic guardrails.

The LLM drives every conversational turn, grounded in the persona, the core pitch, the objection
hints, and the compliance disclosure from agent.yaml. It can therefore handle real human calls:
questions ("who is this?", "how did you get my number?", "is this an AI?"), pushback, and off-script
turns, not just a scripted happy path.

Deterministic code only owns the guardrails that must be reliable:
  - opt-out / do-not-call  -> comply, suppress, end (compliance hard stop)
  - explicit end / goodbye -> end politely
  - booking agreement      -> log the booked_meeting outcome + a graceful close, then end
    (only once the agent has actually offered the meeting, so an early "sure" is not a booking)

Booking currently only LOGS the intent (outcome + transcript). Giving the agent a real cal.com
booking is a planned future integration (see PRD.md "Future work"); the graceful close is written to
promise a follow-up rather than to claim a specific slot was reserved.

The engine keeps the same public surface the CallSession drives (open / handle / voicemail /
.state / .transcript / AgentReply) and stays testable with a FakeLLM, zero network.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from .compliance import ComplianceGate
from .config import AgentConfig
from .contracts import (
    LLM,
    CallState,
    CostMeter,
    Intent,
    IntentResult,
    Outcome,
    TurnRecord,
)
from .intent import IntentClassifier


@dataclass
class AgentReply:
    """One agent turn handed to the session: a stream of text to speak plus control flow."""

    text_stream: AsyncIterator[str]
    state: CallState
    end_after: bool = False
    outcome: Optional[Outcome] = None
    end_reason: str = ""
    intent: Optional[IntentResult] = None


# Short, safe static lines for terminal moments. Kept in code (not the LLM) so they are reliable.
_OPT_OUT_LINE = "Understood, I'll take you off our list. Sorry to bother you, and take care."
_END_LINE = "No problem at all. Thanks for your time, have a great day."
_SILENCE_LINE = "Sorry, I may have lost you there. Are you still on the line?"
# Booking is a two-beat close so the agent does not hang up the instant the contact agrees:
# first confirm and invite a last word, then say goodbye on their next turn (or a silence).
_BOOKING_CONFIRM_LINE = (
    "Perfect, I'll get that set up and send the details over. "
    "Anything else before I let you go?"
)
_BOOKING_GOODBYE_LINE = "Sounds good. Thanks so much for your time, take care!"

# Words in an agent turn that mean the meeting has actually been put on the table. Only after this
# does a contact "yes/sure" count as a booking (fixes the early-agreement false booking).
_MEETING_OFFER_RE = re.compile(
    r"\b(demo|meeting|15[- ]?minute|fifteen[- ]?minute|calendar|schedule|book(?:ing)?|"
    r"quick call|hop on a call|set (?:it|something) up|find a time)\b",
    re.IGNORECASE,
)

# A contact turn that contains an affirmative but ALSO asks for more info / a question is NOT a
# booking ("Yes, tell me more", "Sure, but what do you do?"). Guards against the false-booking where
# a bare "yes" inside an info request closed the call. If any of these match, do not book; let the
# LLM keep talking and book on a cleaner confirmation.
_INFO_REQUEST_RE = re.compile(
    r"\?|\b(tell me more|more about|know more|more info(?:rmation)?|what (?:do|does|is|are|kind)|"
    r"how (?:do|does|did|much|long|many)|why|who (?:are|is)|where|when do|explain|"
    r"like what|such as|for example|first|before (?:we|i)|hold on|wait)\b",
    re.IGNORECASE,
)

# A compliance phrase (opt-out / goodbye) can appear inside a QUESTION about the process rather than
# as a command: "if I opt out, does that mean you'll stop calling me?" matched "stop calling" and
# wrongly hung up on a live call. When the turn is clearly interrogative/hypothetical, do not treat
# it as the command; let the LLM answer honestly (it is instructed to offer opt-out). Bare imperative
# opt-outs ("remove me", "take me off", "stop calling me") are NOT interrogative and still fire.
_HYPOTHETICAL_RE = re.compile(
    r"\?|\b(if i|if you|what if|what happens|would (?:i|you|that)|will (?:i|you|that)|"
    r"do(?:es)? (?:that|this|it) mean|are you (?:gonna|going to)|can i still|is there (?:a|an) (?:way|option))\b",
    re.IGNORECASE,
)


class ConversationEngine:
    def __init__(
        self,
        config: AgentConfig,
        llm: LLM,
        classifier: IntentClassifier,
        compliance: ComplianceGate,
        contact: Optional[dict] = None,
        cost_meter: Optional[CostMeter] = None,
    ):
        self.config = config
        self.llm = llm
        self.classifier = classifier
        self.compliance = compliance
        self.contact = contact or {}
        self.cost_meter = cost_meter

        self.state: CallState = CallState.DIALING
        self.transcript: list[TurnRecord] = []
        self._meeting_offered = False
        self._booking_confirmed = False  # set once the contact agrees; next turn closes the call
        self._system_cache: Optional[str] = None

    # -- template rendering ------------------------------------------------------------------

    def _render(self, template: str) -> str:
        values = {
            "contact_name": self.contact.get("name", "there"),
            "contact_email": self.contact.get("email", ""),
            "suggested_time": self.contact.get("suggested_time", "later this week"),
        }
        out = template
        for key, val in values.items():
            out = out.replace("{" + key + "}", str(val))
        return out

    # -- transcript ---------------------------------------------------------------------------

    def _record(self, speaker: str, text: str) -> None:
        clean = text.strip()
        if not clean:
            return
        self.transcript.append(
            TurnRecord(speaker=speaker, text=clean, state=self.state, at=time.perf_counter())
        )
        # Track whether the agent has actually offered the meeting yet, so an early contact "sure"
        # is not mistaken for a booking.
        if speaker == "agent" and _MEETING_OFFER_RE.search(clean):
            self._meeting_offered = True

    # -- streaming helpers --------------------------------------------------------------------

    async def _tracked(self, source: AsyncIterator[str]) -> AsyncIterator[str]:
        """Forward an agent text stream, accumulating what was actually emitted and recording it
        to the transcript on completion (or on barge-in cancellation, via finally)."""

        parts: list[str] = []
        try:
            async for delta in source:
                parts.append(delta)
                yield delta
        finally:
            self._record("agent", "".join(parts))

    async def _once(self, text: str) -> AsyncIterator[str]:
        yield text

    def _static(self, text: str) -> AsyncIterator[str]:
        return self._tracked(self._once(text))

    # -- LLM turn -----------------------------------------------------------------------------

    def _llm_messages(self) -> list[dict[str, str]]:
        """Map the transcript to chat messages for a free-form turn."""

        messages: list[dict[str, str]] = []
        for turn in self.transcript:
            role = "user" if turn.speaker == "contact" else "assistant"
            messages.append({"role": role, "content": turn.text})
        if not messages or messages[-1]["role"] != "user":
            # Ensure the model has a user turn to answer.
            messages.append({"role": "user", "content": "(the contact is silent)"})
        return messages

    def _system_prompt(self) -> str:
        """Build the grounding prompt once per call: persona, goal, core pitch, honesty rules,
        identity answers, and the configured objection hints."""

        if self._system_cache is not None:
            return self._system_cache

        cfg = self.config
        name = self.contact.get("name", "there")
        pitch = self._render(cfg.script.value_prop)
        disclosure = getattr(cfg.compliance, "ai_disclosure", "")

        lines = [
            cfg.agent.persona.strip(),
            "",
            f"You are on a live outbound phone call with {name}. You are an AI voice assistant, "
            "and you have already disclosed that. Speak naturally, like a real person on the phone.",
            "",
            "Your goal is to book a 15-minute demo. Weave this core pitch in naturally early on, in "
            f'your own words, do not read it verbatim: "{pitch}"',
            "",
            "Rules:",
            "- CRITICAL: keep replies to ONE short spoken sentence whenever possible, and never "
            "more than two. Aim for under 20 words. Answer or ask one thing, then STOP and let them "
            "talk. Do not monologue or explain at length on a phone call; a long reply gets talked "
            "over. No lists, markdown, stage directions, or emoji.",
            "- Be genuinely helpful and never pushy. If they are clearly not interested after you "
            "have made your case once, thank them warmly and let them go.",
            "- Answer honestly and briefly. If they ask who you are or if you are an AI, say yes, "
            "you are an AI assistant calling on their behalf" + (f' ({disclosure})' if disclosure else "")
            + ". If they ask how you got their number, say they are on a contact list you are "
            "reaching out to and they can opt out any time.",
            "- If you do not know something, say you'll have someone follow up.",
            "- When they agree to the demo, confirm warmly in one short sentence.",
        ]

        if cfg.script.objections:
            lines.append("")
            lines.append("Common objections and good responses (adapt, do not quote):")
            for o in cfg.script.objections:
                lines.append(f'- If they say "{o.trigger}": {self._render(o.response)}')

        self._system_cache = "\n".join(lines)
        return self._system_cache

    async def _llm_tracked(self) -> AsyncIterator[str]:
        """Stream a free-form LLM reply, recording the spoken text and metering token usage on
        completion (or on barge-in cancellation, via finally)."""

        parts: list[str] = []
        try:
            async for delta in self.llm.stream(
                self._llm_messages(), system=self._system_prompt()
            ):
                parts.append(delta)
                yield delta
        finally:
            self._record("agent", "".join(parts))
            if self.cost_meter is not None:
                try:
                    self.cost_meter.add_llm_usage(self.llm.last_usage())
                except Exception:
                    pass

    def _llm_reply(self, state: CallState, intent: Optional[IntentResult]) -> AgentReply:
        self.state = state
        return AgentReply(text_stream=self._llm_tracked(), state=state, intent=intent)

    # -- conversation entry points ------------------------------------------------------------

    def open(self) -> AgentReply:
        """The agent's opening turn as one combined reply (AI disclosure + greeting). Retained for
        callers/tests that want the whole opener; the live session uses `opening()` to play the
        disclosure non-interruptibly and let the contact barge in on the greeting."""

        self.state = CallState.GREETING
        greeting = self.compliance.apply_disclosure(self._render(self.config.script.greeting))
        return AgentReply(text_stream=self._static(greeting), state=CallState.GREETING)

    def opening(self) -> tuple[Optional[AgentReply], AgentReply]:
        """Split the opener into (disclosure, greeting). The disclosure (when configured) must be
        heard in full for compliance, so the session plays it non-interruptibly; the greeting is
        then interruptible so the contact can jump in naturally. Returns (None, greeting) when no
        up-front disclosure is configured."""

        self.state = CallState.GREETING
        disclosure_reply: Optional[AgentReply] = None
        cc = self.compliance.config
        if cc.disclose_in_greeting and cc.ai_disclosure:
            disclosure_reply = AgentReply(
                text_stream=self._static(cc.ai_disclosure.strip()), state=CallState.GREETING
            )
        greeting_reply = AgentReply(
            text_stream=self._static(self._render(self.config.script.greeting)),
            state=CallState.GREETING,
        )
        return disclosure_reply, greeting_reply

    def voicemail(self) -> AgentReply:
        """Answering machine detected: leave a short message and end."""

        self.state = CallState.VOICEMAIL
        msg = self._render(self.config.script.greeting)
        return AgentReply(
            text_stream=self._static(msg),
            state=CallState.VOICEMAIL,
            end_after=True,
            outcome=Outcome.VOICEMAIL,
            end_reason="voicemail detected",
        )

    async def handle(self, contact_text: str) -> AgentReply:
        """Process one contact turn and produce the next agent turn.

        Guardrails run first (opt-out, end, silence, booking); everything else is an LLM-driven
        reply grounded in the system prompt.
        """

        self._record("contact", contact_text)
        # Guard classification is regex-only (no LLM fallback) to keep the turn latency low; the
        # reply LLM does the conversational heavy lifting.
        result = await self.classifier.classify(
            contact_text, self.state, use_llm_fallback=False
        )

        # Compliance hard stop, valid in any state. A real opt-out command ("remove me", "take me
        # off", "stop calling me") suppresses and ends. But when the opt-out phrase is inside a
        # QUESTION about the process ("if I opt out, will you stop calling?"), that is not a command:
        # let the LLM answer honestly (it is instructed to offer opt-out) instead of hanging up.
        if result.intent == Intent.OPT_OUT and not _HYPOTHETICAL_RE.search(contact_text):
            phone = self.contact.get("phone", "")
            if phone:
                self.compliance.suppress(phone, reason="opt_out")
            self.state = CallState.END
            return AgentReply(
                text_stream=self._static(_OPT_OUT_LINE),
                state=CallState.END,
                end_after=True,
                outcome=Outcome.OPTED_OUT,
                end_reason="contact opted out",
                intent=result,
            )

        # Booking two-beat close: the previous turn confirmed the booking. If the contact now asks a
        # last question, answer it (do not hang up on a genuine question); otherwise this is their
        # last word, so say goodbye and end with the booking outcome. This fixes both the abrupt
        # hangup on agreement AND cutting off a final "do I need to prepare anything?".
        if self._booking_confirmed:
            if contact_text.strip() and _INFO_REQUEST_RE.search(contact_text):
                return self._llm_reply(CallState.BOOKING, result)  # answer, stay ready to close
            self.state = CallState.END
            return AgentReply(
                text_stream=self._static(_BOOKING_GOODBYE_LINE),
                state=CallState.END,
                end_after=True,
                outcome=Outcome.BOOKED_MEETING,
                end_reason="booking confirmed",
                intent=result,
            )

        # Explicit end / goodbye, any state (but not when it is part of a question, same as opt-out).
        if result.intent == Intent.END and not _HYPOTHETICAL_RE.search(contact_text):
            self.state = CallState.END
            return AgentReply(
                text_stream=self._static(_END_LINE),
                state=CallState.END,
                end_after=True,
                outcome=Outcome.NOT_INTERESTED,
                end_reason="contact ended the call",
                intent=result,
            )

        # Silence: gently reprompt without ending or advancing.
        if result.intent == Intent.SILENCE:
            return AgentReply(
                text_stream=self._static(_SILENCE_LINE), state=self.state, intent=result
            )

        # Booking: only a real booking once the meeting has actually been offered AND the contact's
        # turn is a clean confirmation, not an affirmative wrapped in an info request or question
        # ("Yes, tell me more" must NOT book). Otherwise let the LLM keep the conversation going.
        if (
            result.intent == Intent.BOOKING
            and self._meeting_offered
            and not _INFO_REQUEST_RE.search(contact_text)
        ):
            self.state = CallState.BOOKING
            self._booking_confirmed = True  # next turn (or a silence) delivers the goodbye + end
            return AgentReply(
                text_stream=self._static(_BOOKING_CONFIRM_LINE),
                state=CallState.BOOKING,
                end_after=False,  # do not hang up yet; close warmly on their next turn
                intent=result,
            )

        # Everything else: let the LLM carry the conversation (pitch, questions, objections,
        # curveballs). The engaged phase is tracked as VALUE_PROP for metrics.
        return self._llm_reply(CallState.VALUE_PROP, result)
