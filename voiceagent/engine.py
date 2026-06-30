"""ConversationEngine: the state machine and the brain of a call.

Implements the PRD state machine (DIALING -> GREETING -> VALUE_PROP -> OBJECTION / BOOKING ->
END) over (state, intent). It decides WHAT the agent says; the pipeline handles HOW (STT/TTS).
Script lines come straight from agent.yaml; free-form turns stream from the LLM so audio can
start on sentence 1 while the model is still writing sentence 2.

The engine is driven turn by turn by the CallSession and is deterministic given a fake LLM and
fake intent inputs, so the whole state machine is testable with zero network.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
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
_OBJECTION_LIMIT_LINE = "I completely understand. I'll leave it there. Thanks for your time."
_SILENCE_LINE = "Sorry, I may have lost you there. Are you still on the line?"
_FALLBACK_LINE = "I'll have someone follow up with you. Thanks for your time."


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
        self.objection_count = 0
        self.transcript: list[TurnRecord] = []

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
        if text.strip():
            self.transcript.append(
                TurnRecord(speaker=speaker, text=text.strip(), state=self.state, at=time.perf_counter())
            )

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
        return (
            f"{self.config.agent.persona}\n\n"
            "You are on a live outbound phone call. Keep every reply to at most two short "
            "sentences. Be natural and conversational. Do not use lists or markdown."
        )

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

    def _llm_reply(self) -> AsyncIterator[str]:
        return self._llm_tracked()

    # -- conversation entry points ------------------------------------------------------------

    def open(self) -> AgentReply:
        """The agent's opening turn: AI disclosure (if configured) plus the greeting."""

        self.state = CallState.GREETING
        greeting = self.compliance.apply_disclosure(self._render(self.config.script.greeting))
        return AgentReply(text_stream=self._static(greeting), state=CallState.GREETING)

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
        """Process one contact turn and produce the next agent turn."""

        self._record("contact", contact_text)
        result = await self.classifier.classify(contact_text, self.state)

        # Compliance hard stop, valid in any state.
        if result.intent == Intent.OPT_OUT:
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

        # Explicit end signal, any state.
        if result.intent == Intent.END:
            self.state = CallState.END
            return AgentReply(
                text_stream=self._static(_END_LINE),
                state=CallState.END,
                end_after=True,
                outcome=Outcome.NOT_INTERESTED,
                end_reason="contact ended the call",
                intent=result,
            )

        # Silence in any state: gently reprompt without advancing the state machine.
        if result.intent == Intent.SILENCE:
            return AgentReply(
                text_stream=self._static(_SILENCE_LINE), state=self.state, intent=result
            )

        if self.state == CallState.GREETING:
            return self._enter_value_prop(result)

        if self.state in (CallState.VALUE_PROP, CallState.OBJECTION):
            return await self._handle_pitch(result)

        # Fallback: anything unexpected, hand off gracefully and end.
        self.state = CallState.END
        return AgentReply(
            text_stream=self._static(_FALLBACK_LINE),
            state=CallState.END,
            end_after=True,
            outcome=Outcome.CALLBACK_REQUESTED,
            end_reason="unhandled state",
            intent=result,
        )

    # -- internal transitions -----------------------------------------------------------------

    def _enter_value_prop(self, result: IntentResult) -> AgentReply:
        self.state = CallState.VALUE_PROP
        value_prop = self._render(self.config.script.value_prop)
        return AgentReply(
            text_stream=self._static(value_prop), state=CallState.VALUE_PROP, intent=result
        )

    async def _handle_pitch(self, result: IntentResult) -> AgentReply:
        if result.intent == Intent.BOOKING:
            self.state = CallState.BOOKING
            line = self._render(self.config.script.booking_response)
            return AgentReply(
                text_stream=self._static(line),
                state=CallState.BOOKING,
                end_after=True,
                outcome=Outcome.BOOKED_MEETING,
                end_reason="booking confirmed",
                intent=result,
            )

        if result.intent == Intent.OBJECTION:
            self.objection_count += 1
            if self.objection_count > self.config.script.max_objections:
                self.state = CallState.END
                return AgentReply(
                    text_stream=self._static(_OBJECTION_LIMIT_LINE),
                    state=CallState.END,
                    end_after=True,
                    outcome=Outcome.NOT_INTERESTED,
                    end_reason="objection limit reached",
                    intent=result,
                )
            # Handle the objection, then loop back to the pitch.
            response = (
                self.classifier.objection_response_for(result.matched_trigger or "")
                if result.matched_trigger
                else None
            )
            stream = self._static(self._render(response)) if response else self._llm_reply()
            reply = AgentReply(text_stream=stream, state=CallState.OBJECTION, intent=result)
            self.state = CallState.VALUE_PROP  # handled: ready for the next turn
            return reply

        if result.intent == Intent.SILENCE:
            return AgentReply(
                text_stream=self._static(_SILENCE_LINE), state=CallState.VALUE_PROP, intent=result
            )

        # NEUTRAL: free-form continuation of the pitch via the LLM.
        return AgentReply(text_stream=self._llm_reply(), state=CallState.VALUE_PROP, intent=result)
