"""CallSession: the streaming loop and barge-in core, one per active call.

This is the integration heart of VoiceAgent and stays in the main session by design. It drives
the telephony media stream through the pipeline and the engine, turn by turn, and owns the
barge-in cancellation path: while the agent is speaking, a background listener watches for the
contact starting to talk and cancels the in-flight utterance immediately, the async cousin of a
threading.Event.

Fail-soft is a first-class concern: a provider error or timeout never leaves dead air. The
session recovers with a safe filler line or ends the call cleanly with a logged reason. Cost is
checked every turn so a runaway call is ended gracefully before it overspends.

Tested with FakeSTT / FakeTTS / FakeMediaStream / FakeLLM through the real AssembledPipeline and
ConversationEngine, so the turn-taking, barge-in, budget cap, and fail-soft paths all run with
zero network.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

from .contracts import (
    CallResult,
    CostMeter,
    LatencySample,
    MediaStream,
    Outcome,
    Pipeline,
    STTEventType,
    TurnRecord,
)
from .engine import AgentReply, ConversationEngine

logger = logging.getLogger("voiceagent.session")

_FILLER_LINE = "Sorry about that, could you say that again?"


class CallSession:
    def __init__(
        self,
        engine: ConversationEngine,
        pipeline: Pipeline,
        media: MediaStream,
        cost_meter: CostMeter,
        *,
        contact: Optional[dict] = None,
        silence_timeout: float = 8.0,
        max_silences: int = 2,
        max_turns: int = 40,
        user_finish_grace: float = 4.0,
        max_user_finish_graces: int = 4,
        barge_in_enabled: bool = True,
        barge_in_min_chars: int = 2,
        echo_guard_seconds: float = 0.6,
        playback_margin_seconds: float = 0.4,
        events_dir: Optional[str] = "call_logs/events",
    ):
        self.engine = engine
        self.pipeline = pipeline
        self.media = media
        self.cost_meter = cost_meter
        self.contact = contact or {}
        self.silence_timeout = silence_timeout
        self.max_silences = max_silences
        self.max_turns = max_turns
        # When a silence timeout fires but the contact is mid-utterance, keep waiting this long
        # for their endpoint instead of prompting over them, up to this many extensions.
        self.user_finish_grace = user_finish_grace
        self.max_user_finish_graces = max_user_finish_graces
        # Barge-in robustness. Without acoustic echo cancellation, a bare voice-activity signal
        # is unreliable (it fires on the agent's own audio echoed back, and on noise). So barge-in
        # requires real transcribed words of at least barge_in_min_chars, and is suppressed for
        # echo_guard_seconds after the agent starts speaking. Set barge_in_enabled False for a
        # robust half-duplex call (agent finishes, then listens).
        self.barge_in_enabled = barge_in_enabled
        self.barge_in_min_chars = barge_in_min_chars
        self.echo_guard_seconds = echo_guard_seconds
        # Extra wait beyond raw audio duration to cover network and carrier playback buffering.
        self.playback_margin_seconds = playback_margin_seconds
        self.events_dir = events_dir

        self._turns: asyncio.Queue[tuple[str, float]] = asyncio.Queue()
        self._utterance_parts: list[str] = []  # FINAL transcript segments since the last endpoint
        self._agent_speaking = False
        self._user_speaking = False
        self._barge_in_locked = False  # set while speaking a non-interruptible turn (the greeting)
        self._agent_speak_started_at = 0.0
        self._barge_ins = 0
        self._errors: list[str] = []
        self._latencies: list[LatencySample] = []
        self._events: list[dict] = []
        self._started_at = 0.0

    # -- observability -----------------------------------------------------------------------

    def _log(self, event: str, **data) -> None:
        """Record a timestamped event for the per-call timeline. Cheap and allocation-light so it
        is safe to call on the hot path. Written to a JSONL file at the end of the call and echoed
        to the logger so a live call is fully diagnosable."""

        t = round(time.perf_counter() - self._started_at, 3) if self._started_at else 0.0
        entry = {"t": t, "event": event, **data}
        self._events.append(entry)
        logger.info("call %s", entry)

    # -- public entry point ------------------------------------------------------------------

    async def run(self) -> CallResult:
        self._started_at = time.perf_counter()
        outcome, reason = Outcome.ERROR, "session did not start"
        listen_task: Optional[asyncio.Task] = None
        try:
            await self.pipeline.start(self.media)
            listen_task = asyncio.create_task(self._listen())
            outcome, reason = await self._converse()
        except Exception as exc:  # never let an integration error escape as a crash
            self._errors.append(repr(exc))
            outcome, reason = Outcome.ERROR, f"session error: {exc}"
        finally:
            if listen_task is not None:
                listen_task.cancel()
                try:
                    await listen_task
                except asyncio.CancelledError:
                    pass
            await self._safe(self.pipeline.close())
            await self._safe(self.media.close())
        return self._build_result(outcome, reason)

    # -- background listener (turn-taking + barge-in) ----------------------------------------

    async def _listen(self) -> None:
        try:
            async for ev in self.pipeline.user_events():
                if ev.type == STTEventType.SPEECH_START:
                    self._user_speaking = True
                    self._log("speech_start")
                elif ev.type == STTEventType.PARTIAL:
                    self._user_speaking = True
                    if ev.text.strip():
                        self._maybe_barge_in(ev.text)
                elif ev.type == STTEventType.FINAL:
                    self._user_speaking = True
                    if ev.text.strip():
                        # Accumulate finalized segments so the turn can be assembled even when the
                        # end-of-turn signal (UtteranceEnd) carries no text of its own.
                        self._utterance_parts.append(ev.text.strip())
                        self._maybe_barge_in(ev.text)
                elif ev.type == STTEventType.ENDPOINT:
                    self._user_speaking = False
                    # Assemble the turn from accumulated FINAL segments. Deepgram signals end of
                    # turn two ways: speech_final (ENDPOINT carries text) and UtteranceEnd (ENDPOINT
                    # is empty, the noisy-telephony fallback). Using the accumulated transcript
                    # handles both, so a turn is never stalled to the silence timeout. An endpoint
                    # with no accumulated text is genuine noise/echo: ignore it.
                    turn_text = " ".join(self._utterance_parts).strip() or ev.text.strip()
                    self._utterance_parts = []
                    if turn_text:
                        self._log("endpoint", text=turn_text)
                        await self._turns.put((turn_text, ev.at or time.perf_counter()))
                    else:
                        self._log("endpoint_empty")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._errors.append(f"listen: {exc!r}")
            self._log("listen_error", error=repr(exc))

    def _maybe_barge_in(self, text: str) -> None:
        """Cancel the agent's in-flight utterance only when the contact is genuinely interrupting:
        real transcribed words (not a bare VAD blip), past the echo-guard window, and barge-in
        enabled. This avoids the agent cutting itself off on its own echoed audio or on noise."""

        if not (self.barge_in_enabled and self._agent_speaking):
            return
        if self._barge_in_locked:
            self._log("barge_in_suppressed", reason="locked", text=text)
            return
        if len(text.strip()) < self.barge_in_min_chars:
            return
        if (time.perf_counter() - self._agent_speak_started_at) < self.echo_guard_seconds:
            self._log("barge_in_suppressed", reason="echo_guard", text=text)
            return
        self._barge_ins += 1
        self._log("barge_in", text=text)
        # Schedule the cancel without blocking the listener loop.
        asyncio.create_task(self._safe(self.pipeline.barge_in()))

    # -- conversation loop -------------------------------------------------------------------

    async def _converse(self) -> tuple[Outcome, str]:
        # Opening turn: the AI disclosure must be heard in full (compliance), so play it
        # non-interruptibly; then the greeting is interruptible so the contact can jump in
        # naturally instead of waiting out the whole opener.
        disclosure, greeting = self.engine.opening()
        if disclosure is not None:
            await self._speak_reply(disclosure, interruptible=False)
        await self._speak_reply(greeting, interruptible=True)

        silences = 0
        turns = 0
        while turns < self.max_turns:
            turns += 1

            if self.cost_meter.over_call_cap():
                return Outcome.BUDGET_EXCEEDED, "per-call budget cap reached"

            turn = await self._await_user_turn()
            if turn is None:
                silences += 1
                self._log("silence", count=silences)
                if silences > self.max_silences:
                    if turns <= 1:
                        return Outcome.NO_ANSWER, "no response after greeting"
                    return Outcome.HUNG_UP, "contact went silent"
                reply = await self._safe_handle("")
                await self._speak_reply(reply)
                if reply.end_after:
                    return reply.outcome or Outcome.HUNG_UP, reply.end_reason
                continue

            text, endpoint_at = turn
            silences = 0
            reply = await self._safe_handle(text)
            await self._speak_reply(reply, endpoint_at=endpoint_at)
            if reply.end_after:
                return reply.outcome or Outcome.HUNG_UP, reply.end_reason

        return Outcome.HUNG_UP, "max turns reached"

    async def _await_user_turn(self) -> Optional[tuple[str, float]]:
        """Wait for a completed contact turn. Returns (text, endpoint_at), or None if the contact
        is genuinely silent. If the silence timeout fires while the contact is mid-utterance
        (speech started but not yet endpointed), keep waiting for their endpoint rather than
        prompting over them, up to max_user_finish_graces extensions."""

        timeout = self.silence_timeout
        graces = 0
        while True:
            try:
                return await asyncio.wait_for(self._turns.get(), timeout=timeout)
            except asyncio.TimeoutError:
                if self._user_speaking and graces < self.max_user_finish_graces:
                    graces += 1
                    timeout = self.user_finish_grace
                    continue
                return None

    async def _safe_handle(self, text: str) -> AgentReply:
        """Engine turn with fail-soft: on engine/LLM error, speak a filler and continue."""

        try:
            return await self.engine.handle(text)
        except Exception as exc:
            self._errors.append(f"engine: {exc!r}")
            return AgentReply(text_stream=self._filler_stream(), state=self.engine.state)

    async def _filler_stream(self):
        yield _FILLER_LINE

    # -- speaking + latency ------------------------------------------------------------------

    async def _speak_reply(
        self, reply: AgentReply, endpoint_at: Optional[float] = None, interruptible: bool = True
    ) -> None:
        self._agent_speaking = True
        self._barge_in_locked = not interruptible
        self._agent_speak_started_at = time.perf_counter()
        self._log("agent_speak_start", state=reply.state.value, interruptible=interruptible)
        try:
            utt = await self.pipeline.say(reply.text_stream)
            await utt.wait()
            first_audio_at = getattr(utt, "first_audio_at", None)
            first_text_at = getattr(utt, "first_text_at", None)
            if endpoint_at is not None and first_audio_at is not None:
                sample = LatencySample(
                    endpoint_to_first_audio_ms=(first_audio_at - endpoint_at) * 1000.0
                )
                if first_text_at is not None:
                    sample.endpoint_to_llm_first_token_ms = (first_text_at - endpoint_at) * 1000.0
                    sample.llm_first_token_to_tts_first_audio_ms = (
                        first_audio_at - first_text_at
                    ) * 1000.0
                self._latencies.append(sample)
                self._log(
                    "latency",
                    endpoint_to_token_ms=round(sample.endpoint_to_llm_first_token_ms, 1),
                    token_to_audio_ms=round(sample.llm_first_token_to_tts_first_audio_ms, 1),
                    endpoint_to_audio_ms=round(sample.endpoint_to_first_audio_ms, 1),
                )
            self._log(
                "agent_audio_sent",
                bytes=getattr(utt, "bytes_sent", 0),
                playback_s=round(getattr(utt, "playback_seconds", 0.0), 2),
                cancelled=getattr(utt, "cancelled", False),
            )
            # utt.wait() returns when audio is SENT to the carrier, but the carrier PLAYS it to
            # the contact over several seconds. Wait for that playback to finish before the loop
            # listens for silence, otherwise we prompt "are you still there" over our own audio.
            await self._await_playback(utt, first_audio_at)
        except Exception as exc:
            # A TTS / playback failure must not leave dead air or crash the call.
            self._errors.append(f"speak: {exc!r}")
            self._log("speak_error", error=repr(exc))
        finally:
            self._agent_speaking = False
            self._barge_in_locked = False
            self._log("agent_speak_end")

    async def _await_playback(self, utt: object, first_audio_at: Optional[float]) -> None:
        """Sleep until the audio just sent has finished playing, so silence detection does not
        start while our own audio is still in the contact's ear. A margin covers network and
        carrier buffering. Cut short if the contact barges in (utterance cancelled) or a new turn
        is already queued."""

        playback_seconds = getattr(utt, "playback_seconds", 0.0)
        if not playback_seconds or getattr(utt, "cancelled", False):
            return
        playback_seconds += self.playback_margin_seconds
        start = first_audio_at or time.perf_counter()
        while not getattr(utt, "cancelled", False) and self._turns.empty():
            remaining = playback_seconds - (time.perf_counter() - start)
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.05, remaining))

    # -- helpers -----------------------------------------------------------------------------

    @staticmethod
    async def _safe(awaitable) -> None:
        try:
            await awaitable
        except Exception:
            pass

    def _write_events(self, call_sid: str) -> None:
        """Persist the per-call event timeline to a JSONL file for offline diagnosis."""

        if not self.events_dir or not self._events:
            return
        try:
            d = Path(self.events_dir)
            d.mkdir(parents=True, exist_ok=True)
            name = (call_sid or f"call_{int(time.time())}").replace("/", "_")
            with (d / f"{name}.jsonl").open("w", encoding="utf-8") as fh:
                for entry in self._events:
                    fh.write(json.dumps(entry) + "\n")
        except Exception as exc:  # diagnostics must never break a call
            logger.warning("could not write events file: %r", exc)

    def _build_result(self, outcome: Outcome, reason: str) -> CallResult:
        duration = max(0.0, time.perf_counter() - self._started_at)
        # Approximate the two time-based meters from the call duration. The real Deepgram client
        # exposes precise audio_seconds; prefer it when available.
        stt_seconds = getattr(getattr(self.pipeline, "stt", None), "audio_seconds", None)
        self.cost_meter.add_stt_seconds(stt_seconds if stt_seconds is not None else duration)
        self.cost_meter.add_telephony_seconds(duration)

        call_sid = getattr(self.media, "call_sid", "")
        self._log("call_end", outcome=outcome.value, reason=reason, barge_ins=self._barge_ins)
        self._write_events(call_sid)

        return CallResult(
            call_sid=call_sid,
            contact_name=self.contact.get("name", ""),
            contact_phone=self.contact.get("phone", ""),
            outcome=outcome,
            duration_seconds=round(duration, 3),
            transcript=list(self.engine.transcript),
            cost=self.cost_meter.breakdown(),
            latencies=list(self._latencies),
            end_reason=reason,
            error="; ".join(self._errors) if self._errors else None,
        )
