"""ElevenLabs TTS uses ONE persistent multi-context socket across the call, not one per sentence.

This is the main latency fix (see ISSUES.md O1). No network: websockets.connect is monkeypatched
with a fake that returns canned audio then isFinal per drained context.
"""

from __future__ import annotations

import base64
import json

import pytest

import voiceagent.tts.elevenlabs as el
from voiceagent.tts.elevenlabs import ElevenLabsTTS

AUDIO_B64 = base64.b64encode(b"\xff" * 160).decode()


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False
        self._n = 0

    async def send(self, msg: str) -> None:
        self.sent.append(msg)

    async def recv(self):
        # Alternate one audio frame then an isFinal per context, so each synthesize() drains
        # exactly one audio frame and stops on isFinal.
        self._n += 1
        if self._n % 2 == 1:
            return json.dumps({"audio": AUDIO_B64})
        return json.dumps({"isFinal": True})

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def count_connects(monkeypatch):
    calls = {"n": 0, "sockets": []}

    async def fake_connect(url, **kwargs):
        calls["n"] += 1
        ws = FakeWS()
        calls["sockets"].append(ws)
        calls["last_url"] = url
        return ws

    monkeypatch.setattr(el.websockets, "connect", fake_connect)
    return calls


async def test_one_socket_across_many_sentences(count_connects):
    tts = ElevenLabsTTS("key", "voice")
    await tts.connect()  # pre-warm: one connect
    for sentence in ["One.", "Two.", "Three.", "Four."]:
        frames = [f async for f in tts.synthesize(sentence)]
        assert frames == [b"\xff" * 160]
    # The whole call used a single persistent connection, not one per sentence.
    assert count_connects["n"] == 1
    assert "multi-stream-input" in count_connects["last_url"]
    # Characters are counted once per sentence.
    assert tts.characters_sent == len("One.Two.Three.Four.")
    await tts.close()
    assert count_connects["sockets"][0].closed


async def test_cancel_drops_socket_and_next_synthesize_reconnects(count_connects):
    tts = ElevenLabsTTS("key", "voice")
    await tts.connect()  # connect #1
    frames = [f async for f in tts.synthesize("Hello there.")]
    assert frames
    await tts.cancel()  # drops the socket
    assert count_connects["sockets"][0].closed
    # Next synthesize reopens a fresh persistent socket (connect #2).
    frames2 = [f async for f in tts.synthesize("After barge in.")]
    assert frames2
    assert count_connects["n"] == 2
