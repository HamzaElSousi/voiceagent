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
import re
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

# Word tokens for echo overlap (letters/digits/apostrophes), used to compare a transcript against
# the agent's own in-flight speech.
_WORD_RE = re.compile(r"[a-z0-9']+")


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
        echo_tail_seconds: float = 0.6,
        playback_margin_seconds: float = 0.4,
        events_dir: Optional[str] = "call_logs/events",
        prewarm_llm: bool = True,
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
        # Without acoustic echo cancellation, the agent's own audio is transcribed back by the STT
        # (during playback and for a short tail after). Such transcripts overlap the agent's words,
        # so they are dropped as echo rather than processed as a contact turn or a barge-in.
        self.echo_tail_seconds = echo_tail_seconds
        # Extra wait beyond raw audio duration to cover network and carrier playback buffering.
        self.playback_margin_seconds = playback_margin_seconds
        self.events_dir = events_dir
        # Warm the LLM connection during the greeting to hide its cold-start. Turned off for
        # scripted-fake tests, where a warmup call would consume a canned FakeLLM response.
        self.prewarm_llm = prewarm_llm

        self._turns: asyncio.Queue[tuple[str, float]] = asyncio.Queue()
        self._utterance_parts: list[str] = []  # FINAL transcript segments since the last endpoint
        self._agent_speaking = False
        self._user_speaking = False
        self._barge_in_locked = False  # set while speaking a non-interruptible turn (the greeting)
        self._barge_in_fired = False   # latched per agent turn so one utterance fires barge-in once
        self._stt_ended = False        # set if the STT event stream closes mid-call (we go deaf)
        self._agent_speak_started_at = 0.0
        self._agent_current_text = ""  # what the agent is currently/just spoke (for echo detection)
        self._agent_spoke_until = 0.0  # perf_counter through which echo of the last turn may arrive
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
        prewarm_task: Optional[asyncio.Task] = None
        try:
            await self.pipeline.start(self.media)
            listen_task = asyncio.create_task(self._listen())
            # Warm the LLM connection concurrently with the greeting so the first real turn does
            # not pay the ~13s cold-start (client + DNS/TLS). Best-effort; failures are ignored.
            if self.prewarm_llm:
                prewarm_task = asyncio.create_task(self._prewarm_llm())
            outcome, reason = await self._converse()
        except Exception as exc:  # never let an integration error escape as a crash
            self._errors.append(repr(exc))
            outcome, reason = Outcome.ERROR, f"session error: {exc}"
        finally:
            for task in (listen_task, prewarm_task):
                if task is not None:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            await self._safe(self.pipeline.close())
            await self._safe(self.media.close())
        return self._build_result(outcome, reason)

    async def _prewarm_llm(self) -> None:
        """Fire a tiny throwaway generation to establish the LLM's HTTP connection before the first
        real turn. Runs during the greeting so its cold-start is hidden. Best-effort: any error
        (and cancellation at call end) is swallowed."""

        llm = getattr(self.engine, "llm", None)
        if llm is None:
            return
        try:
            agen = llm.stream([{"role": "user", "content": "hi"}], system="Reply with: ok")
            try:
                async for _ in agen:
                    break  # first token means the connection is warm; stop immediately
            finally:
                aclose = getattr(agen, "aclose", None)
                if aclose is not None:
                    await aclose()
            self._log("llm_prewarmed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._errors.append(f"prewarm: {exc!r}")

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
                        # Drop transcripts that are the agent's own audio echoed back, so echo does
                        # not become a contact turn (which tangles the conversation).
                        if self._looks_like_echo(ev.text):
                            self._log("echo_suppressed", text=ev.text.strip())
                            continue
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
                    endpoint_text = ev.text.strip()
                    if endpoint_text and self._looks_like_echo(endpoint_text):
                        endpoint_text = ""
                    turn_text = " ".join(self._utterance_parts).strip() or endpoint_text
                    self._utterance_parts = []
                    if turn_text:
                        self._log("endpoint", text=turn_text)
                        await self._turns.put((turn_text, ev.at or time.perf_counter()))
                    else:
                        self._log("endpoint_empty")
            # The STT event stream ended on its own. In normal operation _listen is cancelled at
            # call end (handled below), so reaching here means the STT socket closed while the call
            # was still live (e.g. Deepgram idle-closed after an event-loop stall) and we are now
            # deaf. Leave a timeline breadcrumb so this is diagnosable instead of silently running
            # out the clock on "contact went silent". (Not treated as a hard error: a finite test
            # STT ends this way too; the real fix is preventing the loop stall that kills the socket.)
            self._stt_ended = True
            self._log("stt_stream_ended")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._errors.append(f"listen: {exc!r}")
            self._log("listen_error", error=repr(exc))

    def _looks_like_echo(self, text: str) -> bool:
        """True when `text` is very likely the agent's own audio transcribed back, not the contact.

        Only applies while the agent is speaking or within the echo tail after it. Requires a
        multi-word transcript that mostly overlaps the agent's own words, so short genuine
        replies ("yes", "Saturday") are never misread as echo even if they share a word."""

        if not (self._agent_speaking or time.perf_counter() < self._agent_spoke_until):
            return False
        words = [w for w in _WORD_RE.findall(text.lower()) if w]
        # Only long, highly-overlapping transcripts are treated as echo, so short genuine replies
        # (even ones that repeat a word the agent said, like "Saturday at ten works") are trusted.
        if len(words) < 4:
            return False
        agent_words = set(_WORD_RE.findall(self._agent_current_text.lower()))
        if not agent_words:
            return False
        overlap = sum(1 for w in words if w in agent_words) / len(words)
        return overlap >= 0.8

    def _maybe_barge_in(self, text: str) -> None:
        """Cancel the agent's in-flight utterance only when the contact is genuinely interrupting:
        real transcribed words (not a bare VAD blip), past the echo-guard window, and barge-in
        enabled. This avoids the agent cutting itself off on its own echoed audio or on noise."""

        if not (self.barge_in_enabled and self._agent_speaking):
            return
        if self._barge_in_fired:
            return  # latched: one utterance interrupts the current turn at most once (no storm)
        if self._barge_in_locked:
            self._log("barge_in_suppressed", reason="locked", text=text)
            return
        if len(text.strip()) < self.barge_in_min_chars:
            return
        if (time.perf_counter() - self._agent_speak_started_at) < self.echo_guard_seconds:
            self._log("barge_in_suppressed", reason="echo_guard", text=text)
            return
        if self._looks_like_echo(text):
            self._log("barge_in_suppressed", reason="echo_text", text=text)
            return
        self._barge_in_fired = True
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
                # A deaf call (STT socket died mid-call) surfaces here as silence. If we have
                # already had at least one real exchange and then went deaf, end with an honest
                # reason rather than re-prompting a mic we can no longer read. On the very first
                # turn we keep the normal no-answer path (a finite/idle stream is indistinguishable
                # from genuine early silence, and the preload fix prevents the real death anyway).
                if self._stt_ended and turns > 1:
                    return Outcome.ERROR, "speech recognition stream ended mid-call"
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
        self._barge_in_fired = False  # fresh latch for this turn
        self._agent_current_text = ""  # capture what we speak, for echo detection
        self._agent_speak_started_at = time.perf_counter()
        self._log("agent_speak_start", state=reply.state.value, interruptible=interruptible)
        try:
            utt = await self.pipeline.say(self._capture_agent_text(reply.text_stream))
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
                # Audio-smoothness diagnostics (O13 "voice cutting off"): a large max_behind_ms
                # means the TTS fell behind real-time and Twilio underran (choppy). A large
                # max_gap_ms with small max_behind_ms is a burst-then-pause the buffer absorbed.
                max_gap_ms=round(getattr(utt, "max_gap_ms", 0.0), 1),
                max_behind_ms=round(getattr(utt, "max_behind_ms", 0.0), 1),
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
            # Echo of what we just said can keep arriving from the carrier for a short tail.
            self._agent_spoke_until = time.perf_counter() + self.echo_tail_seconds
            self._log("agent_speak_end")

    async def _capture_agent_text(self, stream):
        """Pass the agent's text stream through unchanged while accumulating it, so echo detection
        can compare incoming transcripts against what the agent is actually saying."""

        async for delta in stream:
            self._agent_current_text += delta
            yield delta

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
