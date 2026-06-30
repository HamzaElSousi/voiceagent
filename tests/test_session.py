"""CallSession integration tests through the real AssembledPipeline with fakes.

Covers the full mocked call, the budget cap, no-answer, fail-soft on a provider error, and the
barge-in cancellation path. Zero network, zero real calls.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from voiceagent.contracts import Outcome
from voiceagent.pipeline.assembled import AssembledPipeline
from tests.fakes import SILENCE_FRAME, FakeLLM, FakeMediaStream, FakeSTT, FakeTTS


async def test_full_call_books_meeting(make_session):
    session, _engine, tts, media, _meter = make_session(
        turns=["yeah sure who is this", "okay sounds good lets do it"]
    )
    result = await session.run()
    assert result.outcome == Outcome.BOOKED_MEETING
    assert result.end_reason == "booking confirmed"
    # Greeting (with disclosure), value prop, and booking response were all synthesized.
    assert any("AI assistant" in s for s in tts.spoken)
    assert len(media.outbound) > 0
    assert result.cost.total_usd > 0
    assert result.error is None


async def test_budget_cap_ends_gracefully(make_session):
    session, *_ = make_session(
        turns=["tell me more", "tell me more", "tell me more"],
        llm_responses=["Here is a longer pitch about the value we provide."] * 5,
        per_call_cap=0.0000001,
    )
    result = await session.run()
    assert result.outcome == Outcome.BUDGET_EXCEEDED
    assert "budget" in result.end_reason


async def test_no_answer_when_silent(make_session):
    session, *_ = make_session(turns=[], silence_timeout=0.05, max_silences=0)
    result = await session.run()
    assert result.outcome == Outcome.NO_ANSWER


async def test_fail_soft_on_llm_error(make_session):
    class RaisingLLM(FakeLLM):
        async def stream(self, messages, system=""):
            raise RuntimeError("provider down")
            yield ""  # pragma: no cover - makes this an async generator

    # Turn 1 enters VALUE_PROP state. Turn 2 is a NEUTRAL turn that triggers the
    # (raising) LLM. Turn 3 is the end signal that closes the call cleanly.
    session, _engine, _tts, _media, _meter = make_session(
        turns=["yeah who is this", "can you tell me more about it", "goodbye"],
        llm=RaisingLLM([]),
    )
    result = await session.run()
    # The provider error did not crash the call: it ended cleanly on the end signal.
    assert result.outcome == Outcome.NOT_INTERESTED
    assert result.error is not None and "provider down" in result.error


async def test_barge_in_cancels_in_flight_utterance():
    """When the contact speaks mid-response, the in-flight utterance is cancelled and buffered
    playback is cleared immediately."""

    class SlowTTS(FakeTTS):
        async def synthesize(self, text):
            self.spoken.append(text)
            self.cancelled = False
            for _ in range(100):
                if self.cancelled:
                    return
                yield SILENCE_FRAME
                await asyncio.sleep(0.01)

    stt = FakeSTT([])  # no STT events; we drive barge-in directly
    tts = SlowTTS()
    media = FakeMediaStream(inbound_frames=[SILENCE_FRAME])
    pipeline = AssembledPipeline(stt, tts)
    await pipeline.start(media)

    async def long_reply():
        yield "This is a long sentence that just keeps going and going for a while."

    utterance = await pipeline.say(long_reply())
    await asyncio.sleep(0.03)  # let a few frames stream out
    assert len(media.outbound) > 0

    await pipeline.barge_in()
    assert utterance.cancelled or utterance.finished
    assert media.cleared >= 1          # playback buffer was flushed
    assert tts.cancelled is True       # synthesis was told to stop

    sent_after_clear = len(media.outbound)
    await asyncio.sleep(0.05)           # give any stragglers a chance
    assert len(media.outbound) == sent_after_clear  # no new audio after barge-in

    await pipeline.close()


async def test_opt_out_during_call_suppresses(make_session):
    session, _engine, _tts, _media, _meter = make_session(
        turns=["yeah who is this", "actually please remove me from your list"]
    )
    result = await session.run()
    assert result.outcome == Outcome.OPTED_OUT


async def test_barge_in_gating_ignores_echo_and_noise(make_session):
    """barge-in must require real transcribed words past the echo-guard window, so the agent does
    not cut itself off on its own echoed audio or on a noise blip."""

    session, *_ = make_session(turns=[])
    session._agent_speaking = True

    # A sub-min-chars blip past the guard: ignored.
    session._agent_speak_started_at = time.perf_counter() - 1.0
    session._maybe_barge_in("u")
    assert session._barge_ins == 0

    # Real words, but within the echo-guard window: suppressed.
    session._agent_speak_started_at = time.perf_counter()
    session._maybe_barge_in("hello there")
    assert session._barge_ins == 0

    # Real words past the guard: this is a genuine interruption.
    session._agent_speak_started_at = time.perf_counter() - 1.0
    session._maybe_barge_in("wait stop")
    await asyncio.sleep(0)  # let the scheduled barge_in task run
    assert session._barge_ins == 1


async def test_barge_in_disabled_is_half_duplex(make_session):
    session, *_ = make_session(turns=[])
    session.barge_in_enabled = False
    session._agent_speaking = True
    session._agent_speak_started_at = time.perf_counter() - 1.0
    session._maybe_barge_in("wait stop hold on")
    assert session._barge_ins == 0


async def test_turn_assembled_from_final_then_empty_utterance_end(make_session):
    """The turn must be delivered from accumulated FINAL text even when the end-of-turn signal
    (Deepgram UtteranceEnd) carries empty text. Regression guard for ISSUES.md O2. Drives _listen
    with a scripted event stream."""

    from voiceagent.contracts import STTEvent, STTEventType

    session, *_ = make_session(turns=[])

    async def fake_events():
        yield STTEvent(type=STTEventType.SPEECH_START)
        yield STTEvent(type=STTEventType.FINAL, text="yes", is_final=True)
        yield STTEvent(type=STTEventType.FINAL, text="that works for me", is_final=True)
        yield STTEvent(type=STTEventType.ENDPOINT, text="")  # UtteranceEnd: no text of its own

    session.pipeline.user_events = fake_events
    await session._listen()
    assert not session._turns.empty()
    text, _at = session._turns.get_nowait()
    assert text == "yes that works for me"


async def test_pure_noise_empty_endpoint_is_ignored(make_session):
    """An endpoint with no accumulated transcript is genuine noise/echo and must be ignored."""

    from voiceagent.contracts import STTEvent, STTEventType

    session, *_ = make_session(turns=[])

    async def fake_events():
        yield STTEvent(type=STTEventType.SPEECH_START)
        yield STTEvent(type=STTEventType.ENDPOINT, text="")  # no FINAL accumulated

    session.pipeline.user_events = fake_events
    await session._listen()
    assert session._turns.empty()
