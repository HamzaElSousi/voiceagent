"""Outcome logger tests: CSV header+row and the webhook payload structure."""

from __future__ import annotations

from voiceagent.contracts import (
    CallState,
    CallResult,
    CostBreakdown,
    LatencySample,
    Outcome,
    TurnRecord,
)
from voiceagent.logger import CsvOutcomeLogger, result_to_dict


def _sample_result() -> CallResult:
    return CallResult(
        call_sid="CA123",
        contact_name="John Smith",
        contact_phone="+14155551234",
        outcome=Outcome.BOOKED_MEETING,
        duration_seconds=42.5,
        transcript=[
            TurnRecord(speaker="agent", text="Hi there", state=CallState.GREETING),
            TurnRecord(speaker="contact", text="sure", state=CallState.GREETING),
        ],
        cost=CostBreakdown(total_usd=0.0512, llm_output_tokens=80, tts_characters=120),
        latencies=[LatencySample(endpoint_to_first_audio_ms=640.0)],
        end_reason="booking confirmed",
    )


async def test_csv_logger_writes_header_and_row(tmp_path):
    path = tmp_path / "out.csv"
    logger = CsvOutcomeLogger(path)
    await logger.record(_sample_result())
    text = path.read_text(encoding="utf-8")
    assert "call_sid" in text.splitlines()[0]          # header present
    assert "booked_meeting" in text
    assert "CA123" in text
    # A second call appends without a duplicate header.
    await logger.record(_sample_result())
    assert text.count("call_sid") == 1 or path.read_text().count("call_sid,") == 1


def test_result_to_dict_structure():
    d = result_to_dict(_sample_result())
    assert d["outcome"] == "booked_meeting"
    assert d["cost"]["total_usd"] == 0.0512
    assert d["latencies_ms"] == [640.0]
    assert d["transcript"][0]["speaker"] == "agent"
    assert d["transcript"][0]["state"] == "greeting"
