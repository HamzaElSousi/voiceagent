"""Scripted fake providers for deterministic, zero-network tests.

The reusable pattern across the whole project: a scripted fake client. Here that means a fake
STT (canned transcript/endpoint events), a fake TTS (canned audio frames), a fake LLM (canned
responses + fake token counts), and a fake telephony/media stream. With these, the state
machine, intent detection, cost cap, compliance, and turn-taking are all tested with zero
network and zero real calls. Real APIs appear only in a manual end-to-end run.

These conform to the Protocols in voiceagent.contracts. Asserting against them tests outcomes
and state transitions, not timing.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Iterable, Mapping, Optional, Sequence

from voiceagent.contracts import (
    STTEvent,
    STTEventType,
    TokenUsage,
)

# A small chunk of silence-ish mu-law audio to stand in for real frames.
SILENCE_FRAME = b"\xff" * 160  # 20ms of 8kHz mu-law silence


class FakeSTT:
    """Emits a scripted sequence of STTEvents regardless of the audio fed in.

    Construct with a list of events, or use `from_turns` to build a simple
    "final transcript then endpoint" sequence per contact utterance.
    """

    def __init__(self, events: Sequence[STTEvent]):
        self._events = list(events)
        self.connected = False
        self.closed = False
        self.audio_frames_received = 0

    @classmethod
    def from_turns(cls, turns: Iterable[str], include_speech_start: bool = True) -> "FakeSTT":
        events: list[STTEvent] = []
        for text in turns:
            if include_speech_start:
                events.append(STTEvent(type=STTEventType.SPEECH_START))
            events.append(STTEvent(type=STTEventType.FINAL, text=text, is_final=True))
            events.append(STTEvent(type=STTEventType.ENDPOINT, text=text, is_final=True))
        return cls(events)

    async def connect(self) -> None:
        self.connected = True

    async def send_audio(self, frame: bytes) -> None:
        self.audio_frames_received += 1

    async def events(self) -> AsyncIterator[STTEvent]:
        for ev in self._events:
            yield ev
            await asyncio.sleep(0)

    async def close(self) -> None:
        self.closed = True


class FakeTTS:
    """Yields a fixed number of canned mu-law frames per synthesize() call and records the text
    it was asked to speak. Honors cancellation so barge-in can be tested."""

    def __init__(self, frames_per_call: int = 3):
        self.frames_per_call = frames_per_call
        self.spoken: list[str] = []
        self.connected = False
        self.closed = False
        self.cancelled = False

    async def connect(self) -> None:
        self.connected = True

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        self.spoken.append(text)
        self.cancelled = False
        for _ in range(self.frames_per_call):
            if self.cancelled:
                return
            yield SILENCE_FRAME
            await asyncio.sleep(0)

    async def cancel(self) -> None:
        self.cancelled = True

    async def close(self) -> None:
        self.closed = True


class FakeLLM:
    """Replays scripted responses in order, one per stream() call, yielding word-by-word deltas.
    Reports a deterministic token count so the cost meter can be exercised."""

    def __init__(self, responses: Sequence[str], model: str = "fake-haiku"):
        self._responses = list(responses)
        self._idx = 0
        self._model = model
        self._last_usage = TokenUsage()
        self.calls: list[Sequence[Mapping[str, str]]] = []

    async def stream(
        self,
        messages: Sequence[Mapping[str, str]],
        system: str = "",
    ) -> AsyncIterator[str]:
        self.calls.append(list(messages))
        if self._idx < len(self._responses):
            text = self._responses[self._idx]
        else:
            text = "I'll have someone follow up."
        self._idx += 1

        words = text.split()
        in_tokens = sum(len(str(m.get("content", "")).split()) for m in messages)
        for i, word in enumerate(words):
            yield word if i == 0 else " " + word
            await asyncio.sleep(0)
        self._last_usage = TokenUsage(input_tokens=in_tokens, output_tokens=len(words))

    def last_usage(self) -> TokenUsage:
        return self._last_usage

    @property
    def model(self) -> str:
        return self._model


class FakeMediaStream:
    """A scripted bidirectional audio stream. Replays canned inbound frames and captures every
    outbound frame so tests can assert that the agent actually spoke (and stopped, on barge-in)."""

    def __init__(self, inbound_frames: Optional[Sequence[bytes]] = None, call_sid: str = "CA_fake"):
        self._inbound = list(inbound_frames or [SILENCE_FRAME] * 5)
        self.outbound: list[bytes] = []
        self.cleared = 0
        self.closed = False
        self._call_sid = call_sid

    async def receive_audio(self) -> AsyncIterator[bytes]:
        for frame in self._inbound:
            yield frame
            await asyncio.sleep(0)

    async def send_audio(self, frame: bytes) -> None:
        self.outbound.append(frame)

    async def clear(self) -> None:
        self.cleared += 1
        self.outbound.clear()

    async def close(self) -> None:
        self.closed = True

    @property
    def call_sid(self) -> str:
        return self._call_sid


class FakeTelephony:
    """Records dial requests and returns a synthetic call sid. Never touches the network."""

    def __init__(self) -> None:
        self.placed: list[dict] = []
        self.hung_up: list[str] = []
        self._counter = 0

    async def place_call(self, to: str, from_: str, stream_url: str) -> str:
        self._counter += 1
        sid = f"CA_fake_{self._counter}"
        self.placed.append({"to": to, "from": from_, "stream_url": stream_url, "sid": sid})
        return sid

    async def hang_up(self, call_sid: str) -> None:
        self.hung_up.append(call_sid)


class FakeBooker:
    """A scripted calendar. Serves fixed slots and records booking requests. `taken` marks slots
    that fail as already booked; `fail` makes every booking a calendar error."""

    def __init__(self, slots, taken=(), fail: bool = False):
        self.slots = list(slots)
        self.taken = {s.start for s in taken}
        self.fail = fail
        self.requests = []
        self.slot_queries = 0

    async def get_slots(self, start, end, timezone):
        self.slot_queries += 1
        return list(self.slots)

    async def create_booking(self, request):
        from voiceagent.contracts import BookingResult

        self.requests.append(request)
        if self.fail:
            return BookingResult(ok=False, error="500: calendar down")
        if request.slot.start in self.taken:
            return BookingResult(ok=False, error="400: slot not available", slot_taken=True)
        return BookingResult(ok=True, booking_ref=f"bk_{len(self.requests)}")
