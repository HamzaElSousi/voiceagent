"""Tests for call-SID correlation: XML escaping in build_stream_twiml,
parameter threading through TwilioTelephony.place_call, and server-side
_contact_from_params.

No network I/O and no Twilio credentials required.
"""

from __future__ import annotations

import asyncio

import pytest

from voiceagent.telephony.twilio import TwilioTelephony, build_stream_twiml
from voiceagent.server import _contact_from_params


# ---------------------------------------------------------------------------
# build_stream_twiml -- XML escaping
# ---------------------------------------------------------------------------


def test_build_stream_twiml_escapes_ampersand_in_value():
    """& in a contact name must become &amp; so the TwiML is well-formed XML."""
    twiml = build_stream_twiml(
        "wss://x/stream",
        {"name": "John & Sons", "phone": "+1415"},
    )
    assert '<Parameter name="name" value="John &amp; Sons"/>' in twiml
    assert '<Parameter name="phone" value="+1415"/>' in twiml
    assert "<Connect>" in twiml
    assert "<Stream" in twiml


def test_build_stream_twiml_escapes_double_quote_in_value():
    """A double-quote in a value must be replaced with &quot; to avoid breaking XML."""
    twiml = build_stream_twiml("wss://x/stream", {"name": 'Say "Hello"'})
    assert "&quot;" in twiml
    # Verify the raw unescaped quote does not appear inside the attribute boundary.
    assert 'value="Say "Hello""' not in twiml


def test_build_stream_twiml_escapes_ampersand_in_name():
    """& in a parameter name (unusual but possible) is also escaped."""
    twiml = build_stream_twiml("wss://x/stream", {"a&b": "val"})
    assert 'name="a&amp;b"' in twiml


def test_build_stream_twiml_no_params_unchanged():
    """When no parameters are passed the TwiML must not contain any <Parameter> tag."""
    twiml = build_stream_twiml("wss://host/ws")
    assert "<Parameter" not in twiml
    assert "<Connect>" in twiml


# ---------------------------------------------------------------------------
# TwilioTelephony.place_call -- parameter forwarding
# ---------------------------------------------------------------------------


def test_place_call_passes_parameters_into_twiml():
    """place_call must embed contact parameters as <Parameter> tags in the TwiML
    sent to client.calls.create, with the stream URL present as well.

    Strategy: monkeypatch _client on the TwilioTelephony instance so the lazy
    import of twilio.rest.Client is never triggered. FakeCalls.create is a plain
    sync callable, which is what asyncio.to_thread expects.
    """
    telephony = TwilioTelephony("AC_fake", "token_fake")

    stored: dict = {}

    class _FakeCall:
        sid = "CA_test"

    class _FakeCalls:
        def create(self, **kwargs):
            stored.update(kwargs)
            return _FakeCall()

    class _FakeClient:
        calls = _FakeCalls()

    telephony._client = _FakeClient()

    asyncio.run(
        telephony.place_call(
            "+1999",
            "+1888",
            "wss://x/stream",
            parameters={"name": "Jane"},
        )
    )

    twiml = stored["twiml"]
    # The dict key "name" becomes the Parameter name attribute;
    # "Jane" becomes the value attribute.
    assert 'name="name"' in twiml
    assert 'value="Jane"' in twiml
    assert "wss://x/stream" in twiml
    assert stored["to"] == "+1999"
    assert stored["from_"] == "+1888"


def test_place_call_without_parameters_omits_parameter_tags():
    """Calling place_call with no parameters must not inject any <Parameter> tags."""
    telephony = TwilioTelephony("AC_fake", "token_fake")

    stored: dict = {}

    class _FakeCall:
        sid = "CA_no_params"

    class _FakeCalls:
        def create(self, **kwargs):
            stored.update(kwargs)
            return _FakeCall()

    class _FakeClient:
        calls = _FakeCalls()

    telephony._client = _FakeClient()

    asyncio.run(
        telephony.place_call("+1999", "+1888", "wss://x/stream")
    )

    assert "<Parameter" not in stored["twiml"]


# ---------------------------------------------------------------------------
# Server-side _contact_from_params
# ---------------------------------------------------------------------------


def test_contact_from_params_full_payload():
    """All four keys populated by the CLI must round-trip through _contact_from_params."""
    params = {
        "name": "Jane",
        "phone": "+1",
        "email": "j@x.com",
        "suggested_time": "Tue",
    }
    contact = _contact_from_params(params)
    assert contact["name"] == "Jane"
    assert contact["phone"] == "+1"
    assert contact["email"] == "j@x.com"
    assert contact["suggested_time"] == "Tue"


def test_contact_from_params_missing_keys_use_defaults():
    """Missing keys must fall back to the server defaults, not raise KeyError."""
    contact = _contact_from_params({})
    assert contact["name"] == "there"
    assert contact["phone"] == ""
    assert contact["email"] == ""
    assert contact["suggested_time"] == "later this week"
