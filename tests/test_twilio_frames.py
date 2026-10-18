"""Unit tests for voiceagent.telephony.twilio pure helpers.

No network I/O, no real WebSocket, no Twilio credentials required.
All functions under test are pure or depend only on base64/stdlib.
"""

from __future__ import annotations

import base64

from voiceagent.telephony.twilio import (
    build_stream_twiml,
    clear_message,
    decode_media_payload,
    encode_media_message,
    mark_message,
    parse_start_event,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SILENCE_FRAME = b"\xff" * 160  # 20 ms of 8 kHz mu-law silence


def _inbound_media_event(stream_sid: str, frame: bytes) -> dict:
    """Construct a Twilio-style inbound media event dict (matches the real wire format)."""
    return {
        "event": "media",
        "sequenceNumber": "4",
        "streamSid": stream_sid,
        "media": {
            "track": "inbound",
            "chunk": "2",
            "timestamp": "40",
            "payload": base64.b64encode(frame).decode("ascii"),
        },
    }


# ---------------------------------------------------------------------------
# build_stream_twiml
# ---------------------------------------------------------------------------


def test_build_stream_twiml_contains_connect_and_stream():
    twiml = build_stream_twiml("wss://example.com/stream")
    assert "<Connect>" in twiml
    assert "<Stream" in twiml
    assert "</Response>" in twiml


def test_build_stream_twiml_url_is_present():
    url = "wss://my-server.ngrok.io/media"
    twiml = build_stream_twiml(url)
    assert f'url="{url}"' in twiml


def test_build_stream_twiml_with_parameters_embeds_tags():
    twiml = build_stream_twiml(
        "wss://host/ws",
        parameters={"ContactName": "Jane Doe", "LeadId": "42"},
    )
    assert 'name="ContactName"' in twiml
    assert 'value="Jane Doe"' in twiml
    assert 'name="LeadId"' in twiml
    assert 'value="42"' in twiml


def test_build_stream_twiml_no_params_no_parameter_tag():
    twiml = build_stream_twiml("wss://host/ws")
    assert "<Parameter" not in twiml


def test_build_stream_twiml_none_params_no_parameter_tag():
    twiml = build_stream_twiml("wss://host/ws", parameters=None)
    assert "<Parameter" not in twiml


# ---------------------------------------------------------------------------
# encode_media_message
# ---------------------------------------------------------------------------


def test_encode_media_message_event_and_sid():
    msg = encode_media_message("MZ_abc", SILENCE_FRAME)
    assert msg["event"] == "media"
    assert msg["streamSid"] == "MZ_abc"


def test_encode_media_message_payload_is_valid_base64():
    frame = b"\x00\x01\x02" * 53 + b"\x00"  # 160 bytes
    msg = encode_media_message("MZ1", frame)
    decoded = base64.b64decode(msg["media"]["payload"])
    assert decoded == frame


# ---------------------------------------------------------------------------
# decode_media_payload
# ---------------------------------------------------------------------------


def test_decode_media_payload_returns_bytes_for_media_event():
    frame = SILENCE_FRAME
    inbound = _inbound_media_event("MZ1", frame)
    result = decode_media_payload(inbound)
    assert result == frame


def test_decode_media_payload_returns_none_for_stop_event():
    stop = {"event": "stop", "streamSid": "MZ1", "stop": {"callSid": "CA1"}}
    assert decode_media_payload(stop) is None


def test_decode_media_payload_returns_none_for_connected_event():
    connected = {"event": "connected", "protocol": "Call", "version": "1.0.0"}
    assert decode_media_payload(connected) is None


def test_decode_media_payload_returns_none_for_start_event():
    start = {"event": "start", "streamSid": "MZ1", "start": {}}
    assert decode_media_payload(start) is None


def test_decode_media_payload_returns_none_for_mark_event():
    mark = {"event": "mark", "streamSid": "MZ1", "mark": {"name": "reply-1-end"}}
    assert decode_media_payload(mark) is None


def test_decode_media_payload_returns_none_when_payload_missing():
    msg = {"event": "media", "streamSid": "MZ1", "media": {"track": "inbound"}}
    assert decode_media_payload(msg) is None


# ---------------------------------------------------------------------------
# Round-trip: encode then decode
# ---------------------------------------------------------------------------


def test_encode_decode_round_trip_direct():
    """encode_media_message output can be decoded by decode_media_payload directly.

    Both share the same JSON shape for the fields that matter (event + media.payload).
    """
    frame = bytes(range(160))
    outbound = encode_media_message("MZ_rt", frame)
    recovered = decode_media_payload(outbound)
    assert recovered == frame


def test_encode_decode_round_trip_via_inbound_shape():
    """Simulate the full wire round-trip: encode outbound, reconstruct as inbound, decode."""
    frame = b"\xab\xcd\xef" * 53 + b"\xab"  # 160 bytes
    sid = "MZ_wire"
    inbound = _inbound_media_event(sid, frame)
    recovered = decode_media_payload(inbound)
    assert recovered == frame


# ---------------------------------------------------------------------------
# clear_message
# ---------------------------------------------------------------------------


def test_clear_message_structure():
    msg = clear_message("MZ_xyz")
    assert msg == {"event": "clear", "streamSid": "MZ_xyz"}


# ---------------------------------------------------------------------------
# mark_message
# ---------------------------------------------------------------------------


def test_mark_message_structure():
    msg = mark_message("MZ_xyz", "turn-1-end")
    assert msg["event"] == "mark"
    assert msg["streamSid"] == "MZ_xyz"
    assert msg["mark"]["name"] == "turn-1-end"


def test_mark_message_name_is_preserved():
    msg = mark_message("MZ_1", "greeting-end")
    assert msg["mark"]["name"] == "greeting-end"


# ---------------------------------------------------------------------------
# parse_start_event
# ---------------------------------------------------------------------------

START_EVENT: dict = {
    "event": "start",
    "sequenceNumber": "1",
    "start": {
        "accountSid": "AC_test_account",
        "streamSid": "MZ_stream_001",
        "callSid": "CA_call_001",
        "tracks": ["inbound"],
        "mediaFormat": {
            "encoding": "audio/x-mulaw",
            "sampleRate": 8000,
            "channels": 1,
        },
        "customParameters": {
            "ContactName": "Jane Doe",
            "LeadId": "99",
        },
    },
    "streamSid": "MZ_stream_001",
}


def test_parse_start_event_stream_sid():
    stream_sid, _, _ = parse_start_event(START_EVENT)
    assert stream_sid == "MZ_stream_001"


def test_parse_start_event_call_sid():
    _, call_sid, _ = parse_start_event(START_EVENT)
    assert call_sid == "CA_call_001"


def test_parse_start_event_custom_parameters():
    _, _, params = parse_start_event(START_EVENT)
    assert params["ContactName"] == "Jane Doe"
    assert params["LeadId"] == "99"


def test_parse_start_event_empty_custom_parameters():
    msg = {
        "event": "start",
        "start": {
            "streamSid": "MZ_x",
            "callSid": "CA_x",
        },
    }
    stream_sid, call_sid, params = parse_start_event(msg)
    assert stream_sid == "MZ_x"
    assert call_sid == "CA_x"
    assert params == {}


def test_parse_start_event_missing_start_block_defaults():
    msg = {"event": "start"}
    stream_sid, call_sid, params = parse_start_event(msg)
    assert stream_sid == ""
    assert call_sid == ""
    assert params == {}


def test_parse_start_event_custom_parameters_is_a_copy():
    """Mutating the returned dict must not affect a subsequent parse of the same event."""
    _, _, params = parse_start_event(START_EVENT)
    params["ContactName"] = "MUTATED"
    _, _, params2 = parse_start_event(START_EVENT)
    assert params2["ContactName"] == "Jane Doe"
