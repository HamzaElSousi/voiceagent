"""Cost meter and campaign budget tests."""

from __future__ import annotations

from voiceagent.contracts import CostRates, TokenUsage
from voiceagent.cost import CampaignBudget, DollarCostMeter


def test_meter_accumulates_all_four_meters():
    meter = DollarCostMeter(per_call_cap_usd=1.0, rates=CostRates())
    meter.add_stt_seconds(60)          # 1 min Deepgram
    meter.add_llm_usage(TokenUsage(input_tokens=1000, output_tokens=500))
    meter.add_tts_characters(1000)     # 1k chars ElevenLabs
    meter.add_telephony_seconds(60)    # 1 min Twilio
    b = meter.breakdown()
    assert b.deepgram_seconds == 60
    assert b.llm_input_tokens == 1000
    assert b.tts_characters == 1000
    assert b.total_usd > 0


def test_per_call_cap_trips():
    meter = DollarCostMeter(per_call_cap_usd=0.01, rates=CostRates())
    assert not meter.over_call_cap()
    meter.add_tts_characters(1000)  # 1000 * 0.10/1000 = $0.10 > $0.01
    assert meter.over_call_cap()


def test_campaign_budget_exhaustion():
    budget = CampaignBudget(per_campaign_cap_usd=0.10)
    budget.record_call(0.04)
    assert not budget.exhausted()
    assert budget.remaining() == 0.06
    budget.record_call(0.07)
    assert budget.exhausted()
    assert budget.remaining() == 0.0
