"""Intent detection: objection / booking / end / opt-out classification of a contact turn.

Regex first (fast, deterministic, free), driven by the phrases in agent.yaml, with an optional
LLM fallback for ambiguous turns that do not match any configured trigger. Opt-out always wins,
because it is a compliance hard stop.

Tested deterministically with the FakeLLM, so the regex paths and the fallback both run with zero
network in CI.
"""

from __future__ import annotations

import re
from typing import Optional

from .config import ComplianceConfig, ScriptConfig
from .contracts import LLM, CallState, Intent, IntentResult


def _compile_alternation(spec: str) -> Optional[re.Pattern[str]]:
    """Compile a pipe-delimited signal spec ('yes|sure|okay') into a case-insensitive regex
    that matches any alternative as a whole word/phrase. Returns None if spec is empty."""

    parts = [p.strip() for p in spec.split("|") if p.strip()]
    if not parts:
        return None
    # Sort longest-first so multi-word phrases win over their prefixes.
    parts.sort(key=len, reverse=True)
    alts = "|".join(re.escape(p) for p in parts)
    return re.compile(rf"(?<!\w)(?:{alts})(?!\w)", re.IGNORECASE)


class IntentClassifier:
    """Classifies one contact utterance into an Intent, given the script and compliance config.

    Priority order (first match wins):
      1. opt-out phrases (compliance hard stop)  -> OPT_OUT
      2. end signal                              -> END
      3. booking signal                          -> BOOKING
      4. objection triggers                      -> OBJECTION (with matched_trigger)
      5. LLM fallback (if provided and ambiguous)
      6. NEUTRAL
    """

    def __init__(
        self,
        script: ScriptConfig,
        compliance: ComplianceConfig,
        llm: Optional[LLM] = None,
    ):
        self.script = script
        self.compliance = compliance
        self.llm = llm

        self._booking_re = _compile_alternation(script.booking_signal)
        self._end_re = _compile_alternation(script.end_signal)
        self._opt_out_re = _compile_alternation("|".join(compliance.opt_out_phrases))
        # Objection triggers: each trigger phrase -> its configured response.
        self._objections = [
            (o.trigger.strip().lower(), o.response) for o in script.objections
        ]

    def objection_response_for(self, trigger: str) -> Optional[str]:
        """The configured response for a matched objection trigger, if any."""

        t = trigger.strip().lower()
        for trig, resp in self._objections:
            if trig == t:
                return resp
        return None

    def _match_objection(self, text: str) -> Optional[str]:
        low = text.lower()
        for trig, _resp in self._objections:
            if trig and trig in low:
                return trig
        return None

    async def classify(self, text: str, state: CallState) -> IntentResult:
        """Classify a contact turn. Async because of the optional LLM fallback."""

        stripped = text.strip()
        if not stripped:
            return IntentResult(intent=Intent.SILENCE, source="regex")

        # 1. Opt-out (compliance) always wins.
        if self._opt_out_re and self._opt_out_re.search(stripped):
            m = self._opt_out_re.search(stripped)
            return IntentResult(
                intent=Intent.OPT_OUT,
                matched_trigger=m.group(0) if m else None,
                source="compliance",
            )

        # 2. End signal.
        if self._end_re and self._end_re.search(stripped):
            m = self._end_re.search(stripped)
            return IntentResult(
                intent=Intent.END, matched_trigger=m.group(0) if m else None, source="regex"
            )

        # 3. Booking signal.
        if self._booking_re and self._booking_re.search(stripped):
            m = self._booking_re.search(stripped)
            return IntentResult(
                intent=Intent.BOOKING, matched_trigger=m.group(0) if m else None, source="regex"
            )

        # 4. Objection triggers.
        trig = self._match_objection(stripped)
        if trig is not None:
            return IntentResult(intent=Intent.OBJECTION, matched_trigger=trig, source="regex")

        # 5. LLM fallback for ambiguous turns during the pitch.
        if self.llm is not None and state in (CallState.VALUE_PROP, CallState.OBJECTION):
            label = await self._llm_classify(stripped)
            if label is not None:
                return IntentResult(intent=label, source="llm")

        # 6. Default.
        return IntentResult(intent=Intent.NEUTRAL, source="regex")

    async def _llm_classify(self, text: str) -> Optional[Intent]:
        """Ask the LLM to label an ambiguous turn. Returns None if the label is unusable."""

        system = (
            "You label a single reply from a person on a sales call. "
            "Respond with EXACTLY ONE word from this list and nothing else: "
            "objection, booking, end, neutral."
        )
        messages = [{"role": "user", "content": text}]
        chunks: list[str] = []
        try:
            async for delta in self.llm.stream(messages, system=system):
                chunks.append(delta)
        except Exception:
            return None
        answer = "".join(chunks).strip().lower()
        first = re.sub(r"[^a-z]", "", answer.split()[0]) if answer.split() else ""
        mapping = {
            "objection": Intent.OBJECTION,
            "booking": Intent.BOOKING,
            "end": Intent.END,
            "neutral": Intent.NEUTRAL,
        }
        return mapping.get(first)
