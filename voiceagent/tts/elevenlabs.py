"""ElevenLabs streaming TTS client for VoiceAgent.

Conforms to the TTS Protocol in voiceagent.contracts.

Audio convention: all yielded bytes are raw 8 kHz mono mu-law (G.711 u-law),
requested directly from ElevenLabs via output_format=ulaw_8000. No transcoding
is needed downstream.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from typing import TYPE_CHECKING, AsyncIterator

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy websockets import: the module can be imported even if websockets is
# not installed, so tests that only exercise the sentence splitter still work.
# ---------------------------------------------------------------------------
try:
    import websockets
    import websockets.exceptions as _ws_exc

    _WS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _WS_AVAILABLE = False

# ---------------------------------------------------------------------------
# Sentence splitter
# ---------------------------------------------------------------------------

# Common abbreviations whose trailing period must not be treated as a sentence
# boundary. Lowercase, no trailing dot.
_ABBREVS: frozenset[str] = frozenset({
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs",
    "etc", "approx", "no", "fig", "vol",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug",
    "sep", "oct", "nov", "dec",
    "dept", "est", "govt", "inc", "ltd", "co", "corp",
})

# Matches one or more sentence-terminal characters followed by whitespace.
_BOUNDARY_RE = re.compile(r"([.!?]+)\s+")


def _is_abbreviation(word: str, first_punct: str) -> bool:
    """Return True when `word` + `first_punct` looks like an abbreviation.

    Only periods are ambiguous; ! and ? are unambiguous sentence terminals.
    Single-letter words (initials like 'J.') are also treated as abbreviations.
    """
    if first_punct != ".":
        return False
    w = word.lower().rstrip(".")
    if w in _ABBREVS:
        return True
    # Single alphabetic character: likely an initial (e.g., "J. Smith")
    if len(w) == 1 and w.isalpha():
        return True
    return False


def split_into_sentences(text: str) -> list[str]:
    """Split plain text into sentences on ., !, ? terminal punctuation.

    Rules:
    - Splits only where terminal punctuation is followed by whitespace, so
      end-of-string punctuation lands in the trailing fragment.
    - Ellipsis (three or more consecutive dots) is never a boundary.
    - Known abbreviations and single-letter initials are not boundaries.
    - Trailing fragments (no terminal punctuation) are returned as the last
      element.
    - Each returned sentence has its punctuation preserved and is stripped of
      surrounding whitespace.

    Returns an empty list for empty or whitespace-only input.
    """
    text = text.strip()
    if not text:
        return []

    sentences: list[str] = []
    pos = 0  # start of the next candidate sentence in `text`

    for m in _BOUNDARY_RE.finditer(text):
        punct = m.group(1)

        # Skip ellipsis: three or more consecutive dots are a stylistic pause,
        # not a sentence boundary.
        if punct.count(".") >= 3:
            continue

        # Determine the word immediately before the punctuation to check for
        # abbreviations. `pre_text` is the text from the last boundary to here.
        pre_text = text[pos:m.start()]
        tokens = pre_text.split()
        last_word = tokens[-1] if tokens else ""

        if _is_abbreviation(last_word, punct[0]):
            # Not a real sentence boundary: carry on without advancing `pos`.
            continue

        # m.end(1) is the position just after the punctuation group, before
        # any trailing whitespace, which is exactly what we want to include.
        sentence = text[pos:m.end(1)].strip()
        if sentence:
            sentences.append(sentence)
        pos = m.end()  # advance past the whitespace

    # Any remaining text is a trailing fragment (may or may not end with punct).
    remainder = text[pos:].strip()
    if remainder:
        sentences.append(remainder)

    return sentences


def _buffer_ends_with_real_terminal(text: str) -> bool:
    """Return True if `text` ends with sentence-terminal punctuation that is
    NOT an abbreviation or initial.

    Used by SentenceAggregator to decide whether the last fragment in its
    buffer is a complete sentence or a potential abbreviation that requires
    more context. Rules mirror _is_abbreviation():
    - ! and ? are always unambiguous terminals.
    - . is a terminal UNLESS the immediately preceding word is a known
      abbreviation or a single alphabetic character (initial).
    - Ellipsis (three or more consecutive dots at the very end) is NOT a
      terminal.
    """
    stripped = text.rstrip()
    if not stripped:
        return False

    last_char = stripped[-1]

    if last_char in "!?":
        return True

    if last_char == ".":
        # Ellipsis at the tail: not a terminal.
        if stripped.endswith("..."):
            return False
        # Check whether the last word is an abbreviation.
        words = stripped.split()
        if words:
            last_word = words[-1].rstrip(".").lower()
            if last_word in _ABBREVS:
                return False
            if len(last_word) == 1 and last_word.isalpha():
                return False
        return True

    return False


class SentenceAggregator:
    """Accumulates streaming LLM text deltas and yields complete sentences.

    Designed for the pattern where TTS synthesis begins on sentence 1 while
    the LLM is still generating sentence 2. Call push() with each delta; it
    returns any newly completed sentences. Call flush() at stream end to
    drain the trailing fragment.

    Example:

        agg = SentenceAggregator()
        async for delta in llm.stream(messages):
            for sentence in agg.push(delta):
                async for frame in tts.synthesize(sentence):
                    await media.send_audio(frame)
        remainder = agg.flush()
        if remainder:
            async for frame in tts.synthesize(remainder):
                await media.send_audio(frame)
    """

    def __init__(self) -> None:
        self._buffer: str = ""

    def push(self, delta: str) -> list[str]:
        """Append `delta` to the internal buffer. Return any complete sentences.

        A sentence is considered complete when split_into_sentences detects a
        real boundary inside the buffer. The last element returned by
        split_into_sentences is held back unless the buffer itself ends with
        unambiguous terminal punctuation (meaning it too is complete).
        Abbreviations and initials at the end of the buffer (e.g., "Mr.") are
        NOT treated as sentence terminals; more context is needed.
        """
        self._buffer += delta
        sentences = split_into_sentences(self._buffer)

        if not sentences:
            return []

        # If the buffer ends with real terminal punctuation (not an
        # abbreviation), every split is confirmed complete: flush them all.
        if _buffer_ends_with_real_terminal(self._buffer):
            self._buffer = ""
            return [s for s in sentences if s]

        # Otherwise, the last element might be an incomplete fragment: keep it
        # in the buffer and return only the confirmed-complete ones.
        if len(sentences) > 1:
            complete = sentences[:-1]
            self._buffer = sentences[-1]
            return [s for s in complete if s]

        # Single element that does not clearly end with terminal punctuation:
        # not ready yet.
        return []

    def flush(self) -> str | None:
        """Return any buffered trailing fragment and reset the buffer.

        Returns None if the buffer is empty.
        """
        remainder = self._buffer.strip()
        self._buffer = ""
        return remainder if remainder else None


# ---------------------------------------------------------------------------
# ElevenLabs TTS client
# ---------------------------------------------------------------------------

_ELEVENLABS_WS_URL = (
    "wss://api.elevenlabs.io/v1/text-to-speech"
    "/{voice_id}/stream-input"
    "?model_id={model_id}&output_format={output_format}"
)

# Multi-context endpoint: ONE socket stays open for the whole call, and each sentence is a
# separate context opened and closed with close_context. This avoids the per-sentence WebSocket
# handshake (the dominant latency cost) and the socket-state corruption that the single-context
# (stream-input) per-call connect caused between turns. See ISSUES.md O1.
_ELEVENLABS_MULTI_WS_URL = (
    "wss://api.elevenlabs.io/v1/text-to-speech"
    "/{voice_id}/multi-stream-input"
    "?model_id={model_id}&output_format={output_format}"
)

# BOS voice and generation settings. speed < 1.0 slows the delivery: live callers reported the
# agent spoke too fast on the phone, so 0.9 gives a calmer, more natural pace (range 0.7-1.2).
_DEFAULT_VOICE_SETTINGS = {
    "stability": 0.5,
    "similarity_boost": 0.75,
    "speed": 0.9,
}

# chunk_length_schedule: controls when ElevenLabs auto-generates audio from
# its buffer. Lower first value means lower initial latency (per the research
# doc), at a small prosody cost.
_DEFAULT_GEN_CONFIG = {
    "chunk_length_schedule": [120, 160, 250, 290],
}


class ElevenLabsTTS:
    """ElevenLabs streaming TTS via the stream-input WebSocket endpoint.

    Conforms to the TTS Protocol (voiceagent.contracts.TTS).

    Lifecycle:
        tts = ElevenLabsTTS(api_key=..., voice_id=...)
        await tts.connect()                # no-op, validates config
        async for frame in tts.synthesize("Hello."):
            await media.send_audio(frame)
        await tts.cancel()                 # barge-in: stops in-flight synthesis
        await tts.close()                  # clean shutdown

    Audio: each yielded bytes value is a chunk of raw 8 kHz mono mu-law audio.
    The chunk size matches what ElevenLabs returns per WebSocket message after
    base64 decoding; callers may slice into 160-byte Twilio frames as needed.

    Cost metering: every character sent to ElevenLabs in `text` fields (across
    all synthesize() calls) is counted in `characters_sent`. Feed this to the
    CostMeter via add_tts_characters() at the end of each turn.

    Thread safety: not thread-safe. Designed for a single asyncio event loop.
    """

    def __init__(
        self,
        api_key: str,
        voice_id: str,
        model: str = "eleven_flash_v2_5",
        output_format: str = "ulaw_8000",
    ) -> None:
        if not api_key:
            raise ValueError("ElevenLabsTTS requires a non-empty api_key")
        if not voice_id:
            raise ValueError("ElevenLabsTTS requires a non-empty voice_id")

        self._api_key = api_key
        self._voice_id = voice_id
        self._model = model
        self._output_format = output_format

        # Barge-in state: set by cancel(), cleared at the top of synthesize().
        self._cancelled: bool = False
        # The persistent multi-context WebSocket for the whole call (None when not open).
        self._ws: object | None = None  # websockets.WebSocketClientProtocol | None
        # Whether to use the persistent multi-context socket. Falls back to a fresh one-shot
        # single-context connection per sentence if the persistent socket cannot be opened.
        self._persistent: bool = True
        # Monotonic counter for unique context ids on the shared socket.
        self._ctx_counter: int = 0
        # Running total of text characters sent to ElevenLabs (for the cost meter).
        self._characters_sent: int = 0

    # ------------------------------------------------------------------
    # TTS Protocol methods
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Validate config and pre-warm the persistent multi-context WebSocket so the first
        sentence has no handshake on its critical path. Pre-warming is best-effort: if it fails,
        synthesize() opens the socket lazily or falls back to a one-shot connection.

        Raises ImportError if the `websockets` library is not installed.
        """
        if not _WS_AVAILABLE:
            raise ImportError(
                "websockets is required for ElevenLabsTTS. "
                "Install it with: pip install 'websockets>=12,<13'"
            )
        if self._persistent:
            try:
                await self._ensure_socket()
            except Exception as exc:  # pre-warm is optional; do not fail the call
                logger.warning("ElevenLabs pre-warm failed (%s); will open lazily", exc)

    # ------------------------------------------------------------------
    # Persistent socket management
    # ------------------------------------------------------------------

    async def _ensure_socket(self):
        """Return an open multi-context WebSocket, opening it if needed."""
        if self._ws is not None:
            return self._ws
        url = _ELEVENLABS_MULTI_WS_URL.format(
            voice_id=self._voice_id,
            model_id=self._model,
            output_format=self._output_format,
        )
        ws = await websockets.connect(url, extra_headers={"xi-api-key": self._api_key})  # type: ignore[possibly-undefined]
        self._ws = ws
        logger.debug("ElevenLabsTTS: persistent multi-context socket open")
        return ws

    async def _drop_socket(self) -> None:
        """Close and forget the persistent socket so the next call reopens it."""
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:  # type: ignore[override]
        """Yield raw mu-law audio frames for `text`.

        Uses the persistent multi-context socket (one context per sentence). If the socket cannot
        be opened, falls back to a one-shot single-context connection for this sentence so behavior
        is never worse than a fresh connect. Cancellable mid-stream for barge-in.
        """
        if not _WS_AVAILABLE:  # pragma: no cover
            raise ImportError("websockets library is required")

        self._cancelled = False
        self._characters_sent += len(text)  # count original text, not the padded form

        if self._persistent:
            try:
                ws = await self._ensure_socket()
            except Exception as exc:
                logger.warning("ElevenLabs persistent socket open failed (%s); one-shot", exc)
                await self._drop_socket()
            else:
                async for frame in self._synthesize_multi(ws, text):
                    yield frame
                return

        # Fallback path: a fresh single-context connection just for this sentence.
        async for frame in self._synthesize_oneshot(text):
            yield frame

    async def _synthesize_multi(self, ws, text: str) -> AsyncIterator[bytes]:
        """Synthesize one sentence as its own context on the shared socket. On any socket error
        mid-stream, drop the socket (the next sentence reopens it) and stop; we do not fall back
        here to avoid emitting the sentence's audio twice."""
        self._ctx_counter += 1
        ctx = f"ctx{self._ctx_counter}"
        chunk_text = text if text.endswith(" ") else text + " "
        try:
            await ws.send(json.dumps({
                "text": " ",
                "voice_settings": _DEFAULT_VOICE_SETTINGS,
                "generation_config": _DEFAULT_GEN_CONFIG,
                "context_id": ctx,
            }))
            await ws.send(json.dumps({
                "text": chunk_text,
                "context_id": ctx,
                "try_trigger_generation": True,
                "flush": True,
            }))
            # close_context ends THIS context and makes the server emit its final audio + isFinal,
            # while leaving the socket open for the next sentence's context.
            await ws.send(json.dumps({"context_id": ctx, "close_context": True}))

            while not self._cancelled:
                raw = await ws.recv()
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                audio_b64 = msg.get("audio")
                if audio_b64:
                    yield base64.b64decode(audio_b64)
                if msg.get("isFinal"):
                    break
        except _ws_exc.ConnectionClosed:  # type: ignore[possibly-undefined]
            logger.debug("ElevenLabsTTS: multi-context socket closed during synthesis")
            await self._drop_socket()
        except Exception as exc:
            logger.warning("ElevenLabsTTS: multi-context synth error: %s", exc)
            await self._drop_socket()

    async def _synthesize_oneshot(self, text: str) -> AsyncIterator[bytes]:  # type: ignore[override]
        """Legacy single-context path: a fresh WebSocket for one sentence (BOS -> text+flush ->
        EOS -> drain). Used only as a fallback when the persistent socket is unavailable.

        Parameters:
            text: the sentence or phrase to synthesize.

        Yields:
            bytes: decoded mu-law audio frames, ready for Twilio Media Streams.
        """
        if not _WS_AVAILABLE:  # pragma: no cover
            raise ImportError("websockets library is required")

        url = _ELEVENLABS_WS_URL.format(
            voice_id=self._voice_id,
            model_id=self._model,
            output_format=self._output_format,
        )
        headers = {"xi-api-key": self._api_key}

        try:
            ws = await websockets.connect(url, extra_headers=headers)  # type: ignore[possibly-undefined]
        except Exception as exc:
            logger.error("ElevenLabs WebSocket connect failed: %s", exc)
            raise

        self._ws = ws

        try:
            # BOS: prime the stream with a single space, voice settings, and
            # generation config. The api-key is also sent here as an alternative
            # to the header (belt-and-suspenders).
            bos = {
                "text": " ",
                "voice_settings": _DEFAULT_VOICE_SETTINGS,
                "generation_config": _DEFAULT_GEN_CONFIG,
            }
            await ws.send(json.dumps(bos))

            # Text chunk: append a trailing space per ElevenLabs recommendation.
            # flush=true forces audio generation for everything buffered so far,
            # cutting latency for short sentences.
            chunk_text = text if text.endswith(" ") else text + " "
            # Note: characters are counted in synthesize(), not here, to avoid double counting.
            text_msg = {
                "text": chunk_text,
                "try_trigger_generation": True,
                "flush": True,
            }
            await ws.send(json.dumps(text_msg))

            # EOS: empty text closes the input side and drains buffered audio.
            await ws.send(json.dumps({"text": ""}))

            # Receive audio frames until isFinal or cancellation.
            while True:
                if self._cancelled:
                    logger.debug("ElevenLabsTTS: synthesis cancelled before recv()")
                    break

                try:
                    raw = await ws.recv()
                except _ws_exc.ConnectionClosed:  # type: ignore[possibly-undefined]
                    # cancel() closed the socket, or server closed it.
                    logger.debug("ElevenLabsTTS: WebSocket closed during recv()")
                    break
                except Exception as exc:
                    logger.warning("ElevenLabsTTS: recv() error: %s", exc)
                    break

                # ElevenLabs always sends JSON text frames (not binary).
                if isinstance(raw, bytes):
                    continue

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("ElevenLabsTTS: non-JSON frame received, skipping")
                    continue

                audio_b64 = msg.get("audio")
                if audio_b64:
                    yield base64.b64decode(audio_b64)

                if msg.get("isFinal"):
                    break

        finally:
            self._ws = None
            try:
                await ws.close()
            except Exception:
                pass  # Already closed or network error during teardown.

    async def cancel(self) -> None:
        """Stop any in-flight synthesis immediately (barge-in).

        Sets the cancel flag and closes the socket so the in-flight generation aborts (recv() gets
        ConnectionClosed). The next synthesize() reopens a fresh persistent socket. Closing the
        whole socket on barge-in is simple and correct; persistence resumes on the next turn.
        """
        self._cancelled = True
        await self._drop_socket()
        logger.debug("ElevenLabsTTS: cancelled, socket dropped")

    async def close(self) -> None:
        """Close the persistent WebSocket and release resources."""
        await self._drop_socket()
        logger.debug("ElevenLabsTTS: closed")

    # ------------------------------------------------------------------
    # Cost metering
    # ------------------------------------------------------------------

    @property
    def characters_sent(self) -> int:
        """Total characters sent to ElevenLabs across all synthesize() calls.

        Use this to feed the CostMeter:
            meter.add_tts_characters(tts.characters_sent)

        ElevenLabs bills approximately 1 credit per 2 characters on Flash/Turbo
        (roughly $0.05 per 1,000 characters as of the research doc).
        """
        return self._characters_sent
