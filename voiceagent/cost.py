"""Per-call cost metering across the four meters.

Cost control is first-class, not an afterthought: the meter tracks Deepgram seconds, Claude
tokens, ElevenLabs characters, and Twilio minutes, converts to USD with overridable rates, and
answers whether the per-call cap is exceeded so the session can end a call gracefully before it
overspends. The PRD's whole pitch is cost, so the meter has to be real.

Conforms to the CostMeter Protocol in voiceagent.contracts.
"""

from __future__ import annotations

from .contracts import CostBreakdown, CostRates, TokenUsage


class DollarCostMeter:
    """Accumulates usage and computes USD. One instance per call."""

    def __init__(self, per_call_cap_usd: float = 0.15, rates: CostRates | None = None):
        self.per_call_cap_usd = per_call_cap_usd
        self.rates = rates or CostRates()
        self._stt_seconds = 0.0
        self._llm_in_tokens = 0
        self._llm_out_tokens = 0
        self._tts_chars = 0
        self._telephony_seconds = 0.0

    # -- accumulation -----------------------------------------------------------------------

    def add_stt_seconds(self, seconds: float) -> None:
        self._stt_seconds += max(0.0, seconds)

    def add_llm_usage(self, usage: TokenUsage) -> None:
        self._llm_in_tokens += max(0, usage.input_tokens)
        self._llm_out_tokens += max(0, usage.output_tokens)

    def add_tts_characters(self, n_chars: int) -> None:
        self._tts_chars += max(0, n_chars)

    def add_telephony_seconds(self, seconds: float) -> None:
        self._telephony_seconds += max(0.0, seconds)

    # -- computation ------------------------------------------------------------------------

    @property
    def total_usd(self) -> float:
        r = self.rates
        stt = (self._stt_seconds / 60.0) * r.deepgram_usd_per_minute
        llm = (
            self._llm_in_tokens / 1_000_000.0 * r.claude_in_usd_per_mtok
            + self._llm_out_tokens / 1_000_000.0 * r.claude_out_usd_per_mtok
        )
        tts = (self._tts_chars / 1000.0) * r.elevenlabs_usd_per_1k_chars
        tel = (self._telephony_seconds / 60.0) * r.twilio_usd_per_minute
        return round(stt + llm + tts + tel, 6)

    def over_call_cap(self) -> bool:
        return self.total_usd >= self.per_call_cap_usd

    def breakdown(self) -> CostBreakdown:
        return CostBreakdown(
            deepgram_seconds=round(self._stt_seconds, 3),
            llm_input_tokens=self._llm_in_tokens,
            llm_output_tokens=self._llm_out_tokens,
            tts_characters=self._tts_chars,
            telephony_seconds=round(self._telephony_seconds, 3),
            total_usd=self.total_usd,
        )


class CampaignBudget:
    """Tracks cumulative spend across a campaign and enforces the per-campaign cap. The
    CallManager checks this before dialing the next contact and stops the campaign when the cap
    would be exceeded."""

    def __init__(self, per_campaign_cap_usd: float = 25.0):
        self.per_campaign_cap_usd = per_campaign_cap_usd
        self.spent_usd = 0.0
        self.calls_made = 0

    def record_call(self, call_usd: float) -> None:
        self.spent_usd = round(self.spent_usd + max(0.0, call_usd), 6)
        self.calls_made += 1

    def exhausted(self) -> bool:
        return self.spent_usd >= self.per_campaign_cap_usd

    def remaining(self) -> float:
        return round(max(0.0, self.per_campaign_cap_usd - self.spent_usd), 6)
