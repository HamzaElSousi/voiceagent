"""Shared contracts for VoiceAgent.

This module is the load-bearing boundary of the whole build. Every provider client, the
conversation engine, and the call session code against the Protocols and dataclasses defined
here. Lock this first; only then is parallel module work safe, because each piece codes against
a fixed signature and snaps together on first import.

Nothing here does network I/O. These are pure types: enums, dataclasses, and typing.Protocols.

Audio convention
----------------
All audio frames crossing these interfaces are raw bytes of 8 kHz mono mu-law (G.711 u-law),
the format Twilio Media Streams sends and accepts. Provider clients are responsible for any
resampling or transcoding to and from their own native formats internally, so the session loop
and the pipeline only ever move mu-law frames.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import AsyncIterator, Awaitable, Mapping, Optional, Protocol, Sequence, runtime_checkable

# --------------------------------------------------------------------------------------------
# Enums: the vocabulary of the conversation
# --------------------------------------------------------------------------------------------


class CallState(str, Enum):
    """States of the conversation state machine (see PRD state diagram)."""

    DIALING = "dialing"
    GREETING = "greeting"
    VALUE_PROP = "value_prop"
    OBJECTION = "objection"
    BOOKING = "booking"
    VOICEMAIL = "voicemail"
    END = "end"


class Intent(str, Enum):
    """Classified intent of a single contact turn."""

    NEUTRAL = "neutral"          # ordinary continuation, no special signal
    OBJECTION = "objection"      # an objection trigger matched
    BOOKING = "booking"          # a booking signal matched
    END = "end"                  # an end signal matched (wants to stop)
    OPT_OUT = "opt_out"          # do-not-call / remove-me (compliance, hard stop)
    VOICEMAIL = "voicemail"      # answering machine detected
    SILENCE = "silence"          # no speech / timeout


class Outcome(str, Enum):
    """Terminal outcome of a call, logged per the PRD outcomes list."""

    BOOKED_MEETING = "booked_meeting"
    CALLBACK_REQUESTED = "callback_requested"
    NOT_INTERESTED = "not_interested"
    VOICEMAIL = "voicemail"
    NO_ANSWER = "no_answer"
    OPTED_OUT = "opted_out"            # contact asked to be removed
    BUDGET_EXCEEDED = "budget_exceeded"  # cost cap ended the call
    ERROR = "error"                    # unrecoverable provider failure
    HUNG_UP = "hung_up"                # contact hung up before a clear outcome


class STTEventType(str, Enum):
    PARTIAL = "partial"      # interim transcript, may change
    FINAL = "final"          # finalized transcript for a chunk
    ENDPOINT = "endpoint"    # silence threshold reached: the contact's turn is complete
    SPEECH_START = "speech_start"  # contact began speaking (drives barge-in)


# --------------------------------------------------------------------------------------------
# Data carried across the interfaces
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class STTEvent:
    """An event emitted by an STT stream."""

    type: STTEventType
    text: str = ""
    is_final: bool = False
    confidence: float = 1.0
    # Wall-clock perf_counter() seconds when produced, for latency measurement.
    at: float = 0.0


@dataclass(slots=True)
class IntentResult:
    """The classification of one contact turn."""

    intent: Intent
    matched_trigger: Optional[str] = None   # the objection/booking phrase that matched
    confidence: float = 1.0
    source: str = "regex"                   # "regex" | "llm" | "compliance"


@dataclass(slots=True)
class TokenUsage:
    """LLM token accounting for the cost meter."""

    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(slots=True)
class CostRates:
    """Per-unit USD rates for the four meters. Defaults are rough public rates and are
    overridable so the meter stays honest as pricing changes."""

    deepgram_usd_per_minute: float = 0.0077     # nova-3 streaming STT, billed per second
    claude_in_usd_per_mtok: float = 1.00        # Haiku input per 1M tokens
    claude_out_usd_per_mtok: float = 5.00       # Haiku output per 1M tokens
    elevenlabs_usd_per_1k_chars: float = 0.10   # Flash/Turbo, tier-dependent, override per plan
    twilio_usd_per_minute: float = 0.014        # outbound US PSTN, approximate


@dataclass(slots=True)
class CostBreakdown:
    """Snapshot of accumulated cost across the four meters."""

    deepgram_seconds: float = 0.0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    tts_characters: int = 0
    telephony_seconds: float = 0.0
    total_usd: float = 0.0


@dataclass(slots=True)
class LatencySample:
    """One speech-to-speech measurement, per hop, in milliseconds. Populated with perf_counter
    deltas during a turn so we can report honest p50/p95 per hop."""

    endpoint_to_llm_first_token_ms: float = 0.0
    llm_first_token_to_tts_first_audio_ms: float = 0.0
    endpoint_to_first_audio_ms: float = 0.0     # the headline speech-to-speech number


@dataclass(slots=True)
class TurnRecord:
    """One exchange in the transcript."""

    speaker: str          # "agent" | "contact"
    text: str
    state: CallState
    at: float = 0.0


@dataclass(slots=True)
class CallResult:
    """Everything a finished call hands back to the CallManager for logging."""

    call_sid: str
    contact_name: str
    contact_phone: str
    outcome: Outcome
    duration_seconds: float
    transcript: list[TurnRecord] = field(default_factory=list)
    cost: CostBreakdown = field(default_factory=CostBreakdown)
    latencies: list[LatencySample] = field(default_factory=list)
    end_reason: str = ""      # human-readable, e.g. "booking confirmed", "budget cap hit"
    error: Optional[str] = None
    booking_ref: Optional[str] = None   # calendar booking id when a real slot was booked
    booked_slot: Optional[str] = None   # ISO-8601 start of the booked slot (UTC)


# --------------------------------------------------------------------------------------------
# Booking: real calendar slots offered and reserved during the call
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class Slot:
    """One bookable calendar slot. `start` is timezone-aware."""

    start: datetime
    end: Optional[datetime] = None


@dataclass(slots=True)
class BookingRequest:
    slot: Slot
    name: str
    email: str
    timezone: str              # attendee IANA timezone, for the invite
    phone: str = ""
    notes: str = ""


@dataclass(slots=True)
class BookingResult:
    ok: bool
    booking_ref: str = ""
    error: str = ""
    slot_taken: bool = False   # the slot was booked by someone else in the meantime


# --------------------------------------------------------------------------------------------
# Provider Protocols (the Strategy-pattern seams)
# --------------------------------------------------------------------------------------------


@runtime_checkable
class STT(Protocol):
    """Streaming speech-to-text. Feed mu-law frames in, get transcript/endpoint events out."""

    async def connect(self) -> None:
        """Open the upstream stream (no-op for fakes)."""

    async def send_audio(self, frame: bytes) -> None:
        """Push one inbound mu-law audio frame from the contact."""

    def events(self) -> AsyncIterator[STTEvent]:
        """Async iterator of transcript and endpoint events. Runs until close()."""

    async def close(self) -> None:
        """Tear down the stream."""


@runtime_checkable
class TTS(Protocol):
    """Streaming text-to-speech. Synthesize a piece of text into mu-law audio frames.

    Called once per sentence so audio for sentence 1 starts while the LLM is still writing
    sentence 2. Implementations must support prompt cancellation of an in-flight synthesis
    (barge-in) by cancelling the async iterator (aclose) or honoring cancel()."""

    async def connect(self) -> None: ...

    def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Yield mu-law audio frames for `text`. Cancellable mid-stream."""

    async def cancel(self) -> None:
        """Stop any in-flight synthesis immediately (barge-in)."""

    async def close(self) -> None: ...


@runtime_checkable
class LLM(Protocol):
    """Streaming chat LLM. Yields text deltas; reports token usage for the cost meter.

    Cancellation is cooperative: barge-in cancels the asyncio task consuming `stream`, the
    async cousin of AgentForge's threading.Event."""

    def stream(
        self,
        messages: Sequence[Mapping[str, str]],
        system: str = "",
    ) -> AsyncIterator[str]:
        """Yield response text deltas for the given message history."""

    def last_usage(self) -> TokenUsage:
        """Token usage of the most recent completed stream (for cost accounting)."""

    @property
    def model(self) -> str:
        """The model id in use (for logging)."""


@runtime_checkable
class MediaStream(Protocol):
    """Bidirectional mu-law audio with the contact, as exposed by the telephony layer.

    This is what CallSession talks to: read inbound frames, write outbound frames, and clear
    buffered playback when the contact barges in."""

    async def receive_audio(self) -> AsyncIterator[bytes]:
        """Async iterator of inbound mu-law frames from the contact."""

    async def send_audio(self, frame: bytes) -> None:
        """Send one outbound mu-law frame to the contact."""

    async def clear(self) -> None:
        """Drop any audio buffered for playback (used on barge-in)."""

    async def close(self) -> None: ...

    @property
    def call_sid(self) -> str: ...


@runtime_checkable
class Telephony(Protocol):
    """Call origination and teardown over PSTN."""

    async def place_call(self, to: str, from_: str, stream_url: str) -> str:
        """Dial `to`, instructing the carrier to open a Media Stream to `stream_url`.
        Returns the call sid."""

    async def hang_up(self, call_sid: str) -> None:
        """End an in-progress call."""


class Utterance(Protocol):
    """A handle to an in-progress agent turn (one streamed response being spoken).

    Lets the session cancel speech on barge-in and await natural completion."""

    async def cancel(self) -> None:
        """Cancel TTS and stop sending audio immediately."""

    async def wait(self) -> None:
        """Await until the full utterance has been spoken (or cancelled)."""

    @property
    def finished(self) -> bool: ...


@runtime_checkable
class Pipeline(Protocol):
    """The audio<->text seam presented to CallSession, uniform across pipeline modes.

    - "assembled" wires Deepgram (STT) + ElevenLabs (TTS); the engine + LLM are the brain above.
    - "realtime" wraps a single Realtime API socket (interface-conformant stub this milestone).

    Inbound contact audio becomes STTEvents on `user_events()`. The engine decides what to say
    and streams the text into `say()`, which synthesizes and plays it, returning an Utterance
    handle so the session can barge-in."""

    async def start(self, media: MediaStream) -> None:
        """Bind to a live media stream and begin forwarding audio both ways."""

    def user_events(self) -> AsyncIterator[STTEvent]:
        """Transcript and endpoint events from the contact."""

    def say(self, text_stream: AsyncIterator[str]) -> Awaitable[Utterance]:
        """Speak an agent turn. `text_stream` yields text (LLM deltas or a single script line);
        the pipeline splits into sentences and streams audio out, sentence 1 first."""

    async def barge_in(self) -> None:
        """Cancel the current utterance and clear buffered playback."""

    async def close(self) -> None: ...


@runtime_checkable
class CostMeter(Protocol):
    """Tracks per-call cost across the four meters and answers whether a cap is exceeded.

    Concrete implementation lives in voiceagent/cost.py; the engine and session depend only on
    this surface so cost control is first-class and testable with fakes."""

    def add_stt_seconds(self, seconds: float) -> None: ...

    def add_llm_usage(self, usage: TokenUsage) -> None: ...

    def add_tts_characters(self, n_chars: int) -> None: ...

    def add_telephony_seconds(self, seconds: float) -> None: ...

    @property
    def total_usd(self) -> float: ...

    def over_call_cap(self) -> bool:
        """True once the per-call USD cap is reached."""

    def breakdown(self) -> CostBreakdown: ...


@runtime_checkable
class OutcomeSink(Protocol):
    """Where finished calls are logged (CSV file, webhook, both)."""

    async def record(self, result: CallResult) -> None: ...


@runtime_checkable
class Booker(Protocol):
    """A calendar the agent can book into (Cal.com today). Implementations must be fully async:
    they run during a live call, so they may never block the event loop."""

    async def get_slots(self, start: datetime, end: datetime, timezone: str) -> list[Slot]:
        """Open slots between start and end, earliest first."""

    async def create_booking(self, request: BookingRequest) -> BookingResult:
        """Reserve a slot. Never raises for an expected failure; returns ok=False instead."""
