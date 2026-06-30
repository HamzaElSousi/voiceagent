"""Deepgram streaming STT client.

Conforms to the voiceagent.contracts.STT Protocol. Feed raw 8 kHz mono mu-law
frames (exactly what Twilio Media Streams sends) and receive STTEvent objects
from the events() async iterator.

Audio convention: every frame is raw G.711 u-law bytes. No transcoding is done
here; the bytes are forwarded as-is to Deepgram with encoding=mulaw&sample_rate=8000.

Turn-end detection (two independent triggers, first one wins per utterance):
  1. A Results message with speech_final=true: Deepgram VAD detected a real pause.
  2. An UtteranceEnd message: word-gap detector fired (robust in noisy telephony).

Cost metering: the audio_seconds read-only property tracks total bytes sent
divided by the sample rate (8000 bytes/sec for mu-law 8kHz), matching how
Deepgram bills per second of audio processed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncIterator
from urllib.parse import urlencode

from voiceagent.contracts import STTEvent, STTEventType

logger = logging.getLogger(__name__)

_KEEPALIVE_MSG: str = json.dumps({"type": "KeepAlive"})
_CLOSE_MSG: str = json.dumps({"type": "CloseStream"})
_SENTINEL: object = object()  # signals end-of-stream to events()


class DeepgramSTT:
    """Deepgram Streaming STT client over WebSockets (websockets v12).

    Construction is pure Python and does no network I/O. Call connect() before
    sending audio. The websockets library is imported lazily inside connect() so
    this module loads without error even if the library is absent (it is a
    declared project dependency, but importing eagerly breaks isolated unit tests
    that only exercise _parse_message).

    Parameters
    ----------
    api_key:
        Deepgram API key. Passed as "Authorization: Token <key>" header.
    model:
        Deepgram model name (default "nova-3").
    language:
        BCP-47 language tag or "multi" for nova-3 multilingual (default "en").
    endpointing_ms:
        Milliseconds of silence before Deepgram marks speech_final (default 300).
    sample_rate:
        Audio sample rate in Hz (default 8000, matches Twilio mu-law).
    interim_results:
        Whether to receive partial (interim) transcripts (default True). Required
        for utterance_end_ms to work.
    vad_events:
        Whether to receive SpeechStarted events for barge-in detection (default True).
    utterance_end_ms:
        Word-gap in ms after the last word before UtteranceEnd fires (default 1000).
        Noisy-audio fallback for turn detection when VAD alone misses the pause.
    """

    _WS_BASE: str = "wss://api.deepgram.com/v1/listen"
    _KEEPALIVE_INTERVAL: float = 5.0
    _MULAW_BYTES_PER_SECOND: int = 8000  # 8kHz, 1 byte/sample

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "nova-3",
        language: str = "en",
        endpointing_ms: int = 300,
        sample_rate: int = 8000,
        interim_results: bool = True,
        vad_events: bool = True,
        utterance_end_ms: int = 1000,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._language = language
        self._endpointing_ms = endpointing_ms
        self._sample_rate = sample_rate
        self._interim_results = interim_results
        self._vad_events = vad_events
        self._utterance_end_ms = utterance_end_ms

        self._ws = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._closed: bool = False
        self._total_bytes: int = 0

    # ------------------------------------------------------------------
    # Public interface (STT Protocol)
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the Deepgram WebSocket and start background reader/keepalive tasks."""
        import websockets  # lazy import: module loads cleanly without websockets installed

        url = self._build_url()
        headers = {"Authorization": f"Token {self._api_key}"}
        # websockets 12.x (pinned in pyproject) uses extra_headers; additional_headers is the
        # v13+ name and raises TypeError on v12. Keep this aligned with the pinned version and
        # with the ElevenLabs client, which also uses extra_headers.
        self._ws = await websockets.connect(url, extra_headers=headers)
        self._closed = False
        self._reader_task = asyncio.create_task(self._reader(), name="dg-reader")
        self._keepalive_task = asyncio.create_task(self._keepalive(), name="dg-keepalive")
        logger.debug("DeepgramSTT connected: %s", url)

    async def send_audio(self, frame: bytes) -> None:
        """Forward one mu-law audio frame to Deepgram as a binary WebSocket frame."""
        if self._ws is None or self._closed:
            return
        try:
            await self._ws.send(frame)
            self._total_bytes += len(frame)
        except Exception as exc:
            logger.debug("DeepgramSTT send_audio error (socket likely closed): %s", exc)

    async def events(self) -> AsyncIterator[STTEvent]:
        """Yield STTEvent objects until the stream is closed.

        This is an async generator: call stt.events() to get an AsyncIterator
        and iterate it with async for. Blocks on the internal queue between events.
        """
        while True:
            item = await self._queue.get()
            if item is _SENTINEL:
                return
            yield item  # type: ignore[misc]

    async def close(self) -> None:
        """Gracefully tear down: send CloseStream, cancel tasks, close socket."""
        if self._closed:
            return
        self._closed = True

        # Tell Deepgram to flush remaining audio and close on its side.
        if self._ws is not None:
            try:
                await self._ws.send(_CLOSE_MSG)
            except Exception:
                pass  # socket may already be gone

        # Cancel background tasks.
        for task in (self._keepalive_task, self._reader_task):
            if task is not None and not task.done():
                task.cancel()

        # Unblock any consumer waiting in events() regardless of whether the
        # reader's finally block succeeds.
        self._queue.put_nowait(_SENTINEL)

        # Close the socket.
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        logger.debug("DeepgramSTT closed")

    # ------------------------------------------------------------------
    # Cost metering
    # ------------------------------------------------------------------

    @property
    def audio_seconds(self) -> float:
        """Total seconds of mu-law audio sent to Deepgram.

        Computed from raw byte count: mu-law at 8 kHz = 8000 bytes/second.
        This is the value to feed into CostMeter.add_stt_seconds().
        """
        return self._total_bytes / self._MULAW_BYTES_PER_SECOND

    # ------------------------------------------------------------------
    # Message parsing (pure static method, unit-testable with no network)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_message(msg: dict) -> list[STTEvent]:
        """Convert one Deepgram JSON message dict to a list of STTEvent objects.

        This is a pure function with no side effects. The .at field is left at
        the dataclass default (0.0); the caller (the reader task) stamps it with
        time.perf_counter() just before enqueuing.

        Handled message types:
          SpeechStarted  -> [STTEvent(SPEECH_START)]
          Results (partial, is_final=False) -> [STTEvent(PARTIAL, text)]
          Results (is_final=True) -> [STTEvent(FINAL, text)]
          Results (is_final=True, speech_final=True) -> [FINAL, ENDPOINT]
          UtteranceEnd   -> [STTEvent(ENDPOINT)]
          anything else  -> []

        Empty transcripts are silently ignored (Deepgram sends interim frames
        with empty text while the speaker has not yet said anything).
        """
        events: list[STTEvent] = []
        msg_type = msg.get("type")

        if msg_type == "SpeechStarted":
            events.append(STTEvent(type=STTEventType.SPEECH_START))

        elif msg_type == "Results":
            is_final: bool = bool(msg.get("is_final", False))
            speech_final: bool = bool(msg.get("speech_final", False))

            channel = msg.get("channel") or {}
            alternatives = channel.get("alternatives") or [{}]
            best = alternatives[0] if alternatives else {}
            transcript: str = best.get("transcript", "")
            confidence: float = float(best.get("confidence", 1.0))

            if not transcript:
                return events  # ignore empty interim frames

            if is_final:
                events.append(
                    STTEvent(
                        type=STTEventType.FINAL,
                        text=transcript,
                        is_final=True,
                        confidence=confidence,
                    )
                )
                if speech_final:
                    # VAD detected a real pause: turn is complete.
                    events.append(
                        STTEvent(
                            type=STTEventType.ENDPOINT,
                            text=transcript,
                            is_final=True,
                            confidence=confidence,
                        )
                    )
            else:
                events.append(
                    STTEvent(
                        type=STTEventType.PARTIAL,
                        text=transcript,
                        is_final=False,
                        confidence=confidence,
                    )
                )

        elif msg_type == "UtteranceEnd":
            # Word-gap detector fired: noisy-audio fallback for turn-end.
            events.append(STTEvent(type=STTEventType.ENDPOINT))

        # Metadata, Error, and other types are silently ignored.
        return events

    # ------------------------------------------------------------------
    # Background tasks (private)
    # ------------------------------------------------------------------

    async def _reader(self) -> None:
        """Consume Deepgram JSON messages and push STTEvents to the queue."""
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    # Deepgram sends all control messages as UTF-8 text frames.
                    # Binary frames here would be unexpected; skip them.
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("DeepgramSTT: unparseable message: %r", raw)
                    continue

                for event in self._parse_message(msg):
                    event.at = time.perf_counter()
                    await self._queue.put(event)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("DeepgramSTT reader loop exited: %s", exc)
        finally:
            # Always unblock the events() consumer on any exit path.
            try:
                self._queue.put_nowait(_SENTINEL)
            except Exception:
                pass

    async def _keepalive(self) -> None:
        """Send a KeepAlive text frame every 5 seconds to prevent NET-0001 disconnects."""
        try:
            while True:
                await asyncio.sleep(self._KEEPALIVE_INTERVAL)
                if self._ws is None or self._closed:
                    return
                try:
                    await self._ws.send(_KEEPALIVE_MSG)
                    logger.debug("DeepgramSTT: KeepAlive sent")
                except Exception as exc:
                    logger.debug("DeepgramSTT keepalive send failed: %s", exc)
                    return
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_url(self) -> str:
        """Construct the Deepgram WebSocket URL with all query parameters."""
        params: dict = {
            "encoding": "mulaw",
            "sample_rate": self._sample_rate,
            "channels": 1,
            "model": self._model,
            "language": self._language,
            "punctuate": "true",
            "interim_results": "true" if self._interim_results else "false",
            "smart_format": "true",
            "endpointing": self._endpointing_ms,
            "utterance_end_ms": self._utterance_end_ms,
            "vad_events": "true" if self._vad_events else "false",
        }
        return f"{self._WS_BASE}?{urlencode(params)}"
