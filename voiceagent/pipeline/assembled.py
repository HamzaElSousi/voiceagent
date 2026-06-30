"""AssembledPipeline: Deepgram STT + ElevenLabs TTS behind the Pipeline contract.

This is the "best of breed" pipeline (the project's learning goal): full control of the latency
budget. Inbound contact audio is pumped into the STT; the engine streams agent text into say(),
which aggregates sentences and synthesizes them one at a time so audio for sentence 1 starts
while the LLM is still writing sentence 2. Barge-in cancels the in-flight utterance and clears
buffered playback immediately.

Works with the real Deepgram/ElevenLabs clients or with FakeSTT/FakeTTS (both satisfy the STT
and TTS Protocols), so the whole streaming seam is testable with zero network.
"""

from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator, Optional

from ..contracts import STT, TTS, CostMeter, MediaStream, STTEvent
from ..text import SentenceAggregator


class _Utterance:
    """Concrete Utterance handle for one agent turn. Cancellable for barge-in."""

    def __init__(self) -> None:
        self._cancel = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self.finished = False
        self.first_text_at: Optional[float] = None  # first text delta in (≈ LLM first token)
        self.first_audio_at: Optional[float] = None  # first audio frame sent (TTS first byte out)
        self.spoken_chars = 0
        self.bytes_sent = 0  # total mu-law bytes sent to the contact for this utterance

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def playback_seconds(self) -> float:
        """How long the audio sent for this utterance takes to play. 8 kHz mu-law is 8000 bytes
        per second, so this holds regardless of how the TTS chunked its frames. Used by the
        session to wait for playback to finish before it listens for contact silence."""

        return self.bytes_sent / 8000.0

    async def cancel(self) -> None:
        self._cancel.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.finished = True

    async def wait(self) -> None:
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass


class AssembledPipeline:
    """Conforms to the Pipeline Protocol."""

    def __init__(self, stt: STT, tts: TTS, cost_meter: Optional[CostMeter] = None):
        self.stt = stt
        self.tts = tts
        self.cost_meter = cost_meter
        self.media: Optional[MediaStream] = None
        self._pump_task: Optional[asyncio.Task] = None
        self._current: Optional[_Utterance] = None

    async def start(self, media: MediaStream) -> None:
        self.media = media
        await self.stt.connect()
        await self.tts.connect()
        self._pump_task = asyncio.create_task(self._pump_audio())

    async def _pump_audio(self) -> None:
        """Forward inbound contact audio into the STT stream."""

        assert self.media is not None
        try:
            async for frame in self.media.receive_audio():
                await self.stt.send_audio(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Inbound pump failing should not crash the call; the session fails soft on silence.
            pass

    def user_events(self) -> AsyncIterator[STTEvent]:
        return self.stt.events()

    async def say(self, text_stream: AsyncIterator[str]) -> _Utterance:
        utt = _Utterance()
        utt._task = asyncio.create_task(self._speak(text_stream, utt))
        self._current = utt
        return utt

    async def _speak(self, text_stream: AsyncIterator[str], utt: _Utterance) -> None:
        agg = SentenceAggregator()
        try:
            async for delta in text_stream:
                if utt.first_text_at is None:
                    utt.first_text_at = time.perf_counter()
                if utt.cancelled:
                    break
                for sentence in agg.add(delta):
                    await self._speak_sentence(sentence, utt)
                    if utt.cancelled:
                        break
            if not utt.cancelled:
                for sentence in agg.flush():
                    await self._speak_sentence(sentence, utt)
                    if utt.cancelled:
                        break
        finally:
            # Close the upstream text generator so the engine records the (possibly partial)
            # spoken text and meters usage promptly, even on barge-in.
            aclose = getattr(text_stream, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:
                    pass
            utt.finished = True

    async def _speak_sentence(self, sentence: str, utt: _Utterance) -> None:
        if not sentence.strip():
            return
        utt.spoken_chars += len(sentence)
        if self.cost_meter is not None:
            self.cost_meter.add_tts_characters(len(sentence))
        assert self.media is not None
        async for audio in self.tts.synthesize(sentence):
            if utt.cancelled:
                return
            if utt.first_audio_at is None:
                utt.first_audio_at = time.perf_counter()
            utt.bytes_sent += len(audio)
            await self.media.send_audio(audio)

    async def barge_in(self) -> None:
        if self._current is not None and not self._current.finished:
            await self._current.cancel()
        try:
            await self.tts.cancel()
        except Exception:
            pass
        if self.media is not None:
            await self.media.clear()

    async def close(self) -> None:
        if self._pump_task is not None and not self._pump_task.done():
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
        for closer in (self.stt.close(), self.tts.close()):
            try:
                await closer
            except Exception:
                pass
