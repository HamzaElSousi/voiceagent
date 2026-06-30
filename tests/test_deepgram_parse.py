"""Unit tests for DeepgramSTT._parse_message.

No network, no WebSocket, no API key. Exercises the pure static parser against
canned Deepgram JSON message dicts and asserts correct STTEvent types and fields.

Message types covered:
  - SpeechStarted (barge-in trigger)
  - Results with is_final=False (partial/interim)
  - Results with is_final=True, speech_final=False (final chunk, turn not over)
  - Results with is_final=True, speech_final=True (final chunk + turn end via VAD)
  - UtteranceEnd (word-gap turn-end, noisy-audio fallback)
  - Empty transcript in a Results message (must be silently dropped)
  - Unknown message type (must be silently ignored)
"""

from __future__ import annotations

import pytest

from voiceagent.contracts import STTEventType
from voiceagent.stt.deepgram import DeepgramSTT

parse = DeepgramSTT._parse_message  # convenience alias


# ------------------------------------------------------------------
# SpeechStarted
# ------------------------------------------------------------------


def test_speech_started_emits_speech_start_event() -> None:
    msg = {"type": "SpeechStarted", "channel": [0], "timestamp": 0.1}
    events = parse(msg)
    assert len(events) == 1
    assert events[0].type == STTEventType.SPEECH_START
    assert events[0].text == ""
    assert events[0].at == 0.0  # caller stamps this, parser leaves it default


# ------------------------------------------------------------------
# Results: partial (interim)
# ------------------------------------------------------------------


def test_results_partial_emits_partial_event() -> None:
    msg = {
        "type": "Results",
        "is_final": False,
        "speech_final": False,
        "channel": {
            "alternatives": [{"transcript": "hel", "confidence": 0.72, "words": []}]
        },
    }
    events = parse(msg)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == STTEventType.PARTIAL
    assert ev.text == "hel"
    assert ev.is_final is False
    assert ev.confidence == pytest.approx(0.72)


# ------------------------------------------------------------------
# Results: is_final=True but speech_final=False
# ------------------------------------------------------------------


def test_results_is_final_emits_single_final_event() -> None:
    msg = {
        "type": "Results",
        "is_final": True,
        "speech_final": False,
        "channel": {
            "alternatives": [{"transcript": "hello world", "confidence": 0.95, "words": []}]
        },
    }
    events = parse(msg)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == STTEventType.FINAL
    assert ev.text == "hello world"
    assert ev.is_final is True
    assert ev.confidence == pytest.approx(0.95)


# ------------------------------------------------------------------
# Results: is_final=True AND speech_final=True (turn end via VAD)
# ------------------------------------------------------------------


def test_results_speech_final_emits_final_then_endpoint() -> None:
    msg = {
        "type": "Results",
        "is_final": True,
        "speech_final": True,
        "channel": {
            "alternatives": [{"transcript": "goodbye now", "confidence": 0.99, "words": []}]
        },
    }
    events = parse(msg)
    assert len(events) == 2, "expected FINAL followed by ENDPOINT"

    final_ev = events[0]
    assert final_ev.type == STTEventType.FINAL
    assert final_ev.text == "goodbye now"
    assert final_ev.is_final is True
    assert final_ev.confidence == pytest.approx(0.99)

    endpoint_ev = events[1]
    assert endpoint_ev.type == STTEventType.ENDPOINT
    assert endpoint_ev.text == "goodbye now"
    assert endpoint_ev.is_final is True


# ------------------------------------------------------------------
# UtteranceEnd (word-gap detector, noisy-audio fallback)
# ------------------------------------------------------------------


def test_utterance_end_emits_endpoint_event() -> None:
    msg = {"type": "UtteranceEnd", "channel": [0], "last_word_end": 3.1}
    events = parse(msg)
    assert len(events) == 1
    assert events[0].type == STTEventType.ENDPOINT


# ------------------------------------------------------------------
# Edge cases
# ------------------------------------------------------------------


def test_empty_transcript_is_ignored() -> None:
    """Deepgram sends interim frames with empty transcript while warming up."""
    msg = {
        "type": "Results",
        "is_final": True,
        "speech_final": False,
        "channel": {
            "alternatives": [{"transcript": "", "confidence": 0.0, "words": []}]
        },
    }
    events = parse(msg)
    assert events == []


def test_unknown_message_type_returns_empty_list() -> None:
    """Metadata, Error, and other Deepgram messages must be silently ignored."""
    for msg_type in ("Metadata", "Error", "Finalize", "SomeFutureType"):
        events = parse({"type": msg_type})
        assert events == [], f"expected empty list for type={msg_type!r}"


def test_results_missing_alternatives_gracefully_ignored() -> None:
    """Malformed Results with no alternatives should not raise."""
    msg = {
        "type": "Results",
        "is_final": True,
        "speech_final": False,
        "channel": {"alternatives": []},
    }
    events = parse(msg)
    assert events == []


def test_confidence_defaults_to_one_when_absent() -> None:
    """Some Deepgram responses omit the confidence field; default to 1.0."""
    msg = {
        "type": "Results",
        "is_final": True,
        "speech_final": False,
        "channel": {
            "alternatives": [{"transcript": "hi there"}]
        },
    }
    events = parse(msg)
    assert len(events) == 1
    assert events[0].confidence == pytest.approx(1.0)
