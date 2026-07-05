# VoiceAgent Issues Log

A running record of issues found during the build and live testing, so a fresh session can resume
without re-discovering them. Grouped as Open, Fixed, and Gotchas. Newest context at the top of each
section. See `CLAUDE.md` for project orientation and `call_logs/events/<call_sid>.jsonl` for the
per-call timeline that drives most of this diagnosis.

## Status snapshot (2026-07-02)
Milestone 2 latency + turn-taking fixes are DONE and validated on a live call. endpoint->first-audio
p50 is ~130ms (was ~892ms), target was sub-500ms. Live retest SID CA02e7a1...2380 booked a meeting
with 0 false barge-ins, clean end, no socket errors. All O1-O4 below are fixed (see F5-F8). The
Twilio -> Deepgram -> LLM -> ElevenLabs design and Claude default are intact; OpenRouter is available
as a model-agnostic alternative behind the LLM interface.

---

## Open (Milestone 3: conversation quality)

Status 2026-07-04: after the F16 event-loop fix, a live freestyle call (SID CA9b848c) FINALLY held a
real back-and-forth: instant greeting (first audio 0.24s), answered "who is this", "how did you get
my number" (x2), "what do you guys do", an opt-out question, ~0.8-1.1s per turn, and booked. The core
works. Remaining items are polish surfaced by that call plus Fable's deferred recommendations.

### O12. False booking on "yes ..." (booking regex too eager)
Live: the contact said "Yes. Tell me more." and the agent BOOKED and hung up, because the booking
signal regex matches the bare "yes" and `_meeting_offered` was already true. "Yes, tell me more" is a
request for more info, not consent to book. Fix: booking should require the "yes" to actually be
agreement to the meeting, not merely contain an affirmative word followed by a question / "tell me
more" / another request. Tighten the booking guard (e.g. reject when the turn also contains a
question or an info-request phrase), and/or let the LLM confirm booking intent.

### O13. "Your voice is cutting off" (audio choppiness under barge-in)
The contact said this mid-call (t=18.9s). Lines up with frequent barge-ins cancelling audio
mid-sentence and restarting, plus one TTS latency spike (t=36.8: token_to_audio 3617ms). Need to
determine whether choppiness is only during interruptions or also on uninterrupted speech (asked the
user). Candidate fixes: Twilio `mark`-based playback tracking (O15), gentler barge-in, smoothing.

### O11. Booking closes before the contact finishes giving the time
Because booking only logs intent, the agent closed as soon as agreement was detected, cutting off
"Saturday at ten." It felt abrupt. Acceptable for now (intent-only), but when cal.com lands the
booking must capture the actual time/slot before closing. Tracked with the cal.com future goal.

### O8. Adversarial paths - PARTIALLY validated (2026-07-04)
Live call CA9b848c exercised: interrupting, "who is this", "how did you get my number", an opt-out
QUESTION, info requests, booking. Still not exercised live: a hard opt-out COMMAND ("remove me" /
"don't call") mid-call, a soft "not interested", and a genuine silence/hangup. Keep testing.

## Fable's deferred recommendations (from the 2026-07-04 architecture review)
Captured so we implement them deliberately, not rediscover them. Done ones are in Fixed (F16).
- **Move the repo off `/mnt/c` to WSL-native ext4 (`~/`).** Root cause of the slow imports/I-O that
  caused F16; fixes a whole class of loop-stall risk. (Not done; needs user buy-in, changes paths.)
- **O14. Deepgram auto-reconnect + loud failure.** Make `DeepgramSTT` detect an unexpected socket
  close and reconnect (or emit a distinct error event) instead of the session inferring deafness
  from a silent stream-end. Partly done: `session.py` now logs `stt_stream_ended` + ERROR-exits
  after a real exchange (F16), but there is no reconnect yet.
- **O15. Use Twilio `mark` events for exact playback-done.** `twilio.py` already sends/receives
  `mark`; `_await_playback` still uses computed duration + a 0.4s margin. Switching to `mark` gives
  precise end-of-playback and likely helps O13 choppiness and silence timing.
- **O16. Shorten the greeting** to one sentence + a question (less to sit through, faster to the
  contact's first turn). Greeting text is in `examples/agent.yaml`.
- Endpointing (`endpointing_ms` 250 / `utterance_end_ms` 1000) is fine; Fable said do not tune it
  until the loop stall was fixed. Now fixed, so it can be revisited if turns feel too eager/slow.
- Greeting-audio caching: only the disclosure is truly static (greeting is templated with
  `{contact_name}`). Lower priority now that F16 made the greeting fast, but if reintroduced, cache
  key on (voice_id, model, output_format, text) and still populate `_agent_current_text`,
  `bytes_sent`/`playback_seconds`, and `engine._record("agent", ...)` if bypassing `pipeline.say()`.

### O11. Booking closes before the contact finishes giving the time
Because booking only logs intent, the agent closed as soon as agreement was detected, cutting off
"Saturday at ten." It felt abrupt. Acceptable for now (intent-only), but when cal.com lands the
booking must capture the actual time/slot before closing. Tracked with the cal.com future goal.

### O8. Adversarial paths not yet validated live
The rebuilt core is verified offline (fakes), but on a real call only the happy path has ever been
exercised. Need live runs that argue, ask "who is this / how did you get my number / are you an AI",
give a soft "not interested", opt out mid-call, go silent, and confirm a booking, then read
`call_logs/events/<sid>.jsonl` and tune. Until then, treat conversation quality as unproven live.

---

## Fixed

### F16. Event-loop freeze from a blocking `import anthropic` (root cause of dead-air AND deaf calls)
Live call CAe9178c: caller heard ~10s silence then a voice, and no user audio was captured all
call (agent monologued through 3 silences, hung up "contact went silent"). Fable's diagnosis,
confirmed by `time python -c "import anthropic"` = **14.1s** from `/mnt/c` (WSL2 9p filesystem):
the lazy `from anthropic import AsyncAnthropic` in `ClaudeLLM._ensure_client`, fired by
`_prewarm_llm` at t=0.4s, is a synchronous import that **blocked the asyncio event loop for ~14s**.
That single freeze (a) stalled ElevenLabs frame send -> ~13s dead-air on the greeting, and (b)
stalled Deepgram's keepalive task so its socket idle-closed (NET-0001) and the call went deaf for
good (no STT events -> silence -> hangup). Fixes: (1) `cli.py serve` preloads `anthropic`/`httpx`/
`websockets` before uvicorn accepts calls (pays the cost once at boot); (2) `claude.py` builds the
client via `asyncio.to_thread(self._ensure_client)` so the import never blocks the loop even if not
preloaded. Proven: with the import in a thread, worst event-loop stall during it dropped from ~14s
to 55ms. Also added a `stt_stream_ended` timeline breadcrumb + an honest ERROR exit when the STT
dies after a real exchange (`session.py`), so a deaf call is diagnosable instead of silent.
Recommendation (not done): move the repo to WSL-native ext4 (~/) to fix this class of slow I/O.

### F15. Echo: the agent's own speech transcribed back as a contact turn (was O10)
Without acoustic echo cancellation, Deepgram transcribes the agent's own audio (during playback and
a short carrier tail after), which could become a spurious contact turn and tangle the conversation.
Fixed: `session.py` captures the agent's in-flight text (`_capture_agent_text`) and
`_looks_like_echo()` drops a transcript when, while the agent is speaking or within
`echo_tail_seconds`, it is a long (>=4 word) transcript that overlaps the agent's own words >=80%.
Short genuine replies (even ones repeating a word the agent said, like "Saturday at ten works") are
always trusted. Applied to both turn capture (`_listen`) and barge-in (reason `echo_text`). Tests:
`test_echo_transcript_not_captured_as_turn`, `test_real_reply_during_agent_speech_is_kept`,
`test_short_reply_repeating_agent_word_is_not_echo`. (For a stubborn environment, `barge_in: false`
in agent.yaml gives a robust half-duplex call.)

### F14. Barge-in storm: one utterance re-triggered barge-in repeatedly (was O9)
Live retest 2 (SID CAc871...ebd): "Saturday at ten." fired barge-in ~6 times because
`_maybe_barge_in` ran on every PARTIAL/FINAL update with no latch. Fixed: `session.py` latches
`_barge_in_fired` per agent turn (reset at each `_speak_reply` start), so one utterance interrupts at
most once. Tests: `test_barge_in_latched_once_per_utterance`, `test_latch_resets_for_next_agent_turn`.

### F13. LLM cold-start caused ~13-18s dead air on the first turn of every call
Live retest 1 showed the first contact turn taking 18s to first audio (endpoint_to_token_ms=18046).
Root cause: each call builds a fresh LLM client, so the first `stream()` pays client creation +
DNS/TLS (~13s under WSL2); warm calls are ~0.85-0.95s TTFT. This was invisible in M2 because
value_prop/booking were static script lines (LLM off the latency path). Fixed: `session.py`
`_prewarm_llm()` fires a tiny throwaway generation concurrently with the greeting so the cold-start
is hidden; the first real turn is warm. Gated by `prewarm_llm` (off for scripted-fake tests/CLI so
the warmup does not consume a canned FakeLLM response). Live retest 2 (SID CAc871...ebd): first-turn
endpoint_to_audio dropped to ~0.96s, later turns ~0.86-1.4s; it handled "who is this / how did you
get my number" and booked. Honest latency floor for a live LLM turn here is ~0.9s (LLM ~0.5-0.8s +
TTS ~0.4-0.6s) + ~250ms Deepgram endpointing, i.e. perceived ~1.2-1.6s; the sub-500ms figure only
ever held for static lines. Also trimmed reply length (max_tokens 100 + a ~30-word prompt cap).

### F12. No handling for legitimacy challenges or real pushback (was O7)
"Who is this?", "How did you get my number?", "Is this a robot?", and genuine arguments used to fall
to a generic pitch-continuation. Fixed by the LLM-driven core: the system prompt (`engine.py`
`_system_prompt`) grounds the model in persona, goal, the honest identity/source answers, and the
agent.yaml objection hints, so it answers these naturally. The old 2-objection hard limit that made
the agent give up was removed (objections no longer end the call). Tests in `tests/test_engine.py`
(`test_legitimacy_question_is_answered_not_ended`, `test_objections_do_not_end_the_call`).

### F11. Agent ignored its own questions; turn machine advanced on count (was O6)
The old machine advanced GREETING -> VALUE_PROP on the first contact turn regardless of content, so
answers (e.g. the name) were never used and there was "no way to respond." Fixed: after the greeting
every turn is an LLM reply over the full transcript, so the agent actually responds to what was said.

### F10. Booking hung up mid-question / fired too early (was O5)
Two bugs: (a) the BOOKING branch set `end_after=True` while `booking_response` asked an unanswered
question, so agreeing dropped the call; (b) any early "sure" counted as a booking. Fixed: booking is
gated on `engine._meeting_offered` (true only once an agent turn actually offered the demo, via
`_MEETING_OFFER_RE`), and the close line (`_BOOKING_CLOSE_LINE`) promises follow-up without asking a
question. An early "sure" now continues the conversation. Tests: `test_early_agreement_is_not_a_booking`,
`test_booking_only_after_meeting_offered_then_logs_and_ends`.

### F9. Signal matching brittle to STT variance + missing opt-out contractions
Booking/end/opt-out matching required exact apostrophes and phrasing ("let's do it" missed "lets do
it"; "do not call" missed "don't call"). Fixed: `intent.py` strips apostrophes from both the
configured signals and the incoming text (`_deapostrophe`), booking phrases broadened, and
`don't call` / `dont call` / `stop contacting` added to opt-out phrases (compliance hard stop).
A soft "not interested" was also removed from `end_signal` so it no longer instantly hangs up; the
LLM handles it. Because booking is offer-gated, the broadened affirmatives cannot cause false bookings.

---

## Fixed

### F8. Per-hop latency was not separable (was O4)
Logging gave one endpoint-to-first-audio number; LLM vs TTS time was invisible. Fixed: `session.py`
now logs a `latency` event per turn with `endpoint_to_token_ms`, `token_to_audio_ms`,
`endpoint_to_audio_ms` into `call_logs/events/<sid>.jsonl` and `LatencySample`. Live retest showed
LLM token ~0.3ms, TTS first audio ~120-140ms, i.e. TTS is the remaining cost, not the LLM.

### F7. Turn-end detection not tuned/configurable (was O3)
`endpointing_ms` and `utterance_end_ms` are now `agent.yaml` fields (defaults 250 / 1000) wired
through `config.py` -> `builder.py` -> `stt/deepgram.py` `_build_url`. Test: `tests/test_ws_connect.py`
plus config round-trip in `tests/test_config.py`.

### F6. UtteranceEnd turn detection dropped (was O2; regression, multi-second stalls)
The "ignore empty-transcript endpoints" change dropped Deepgram's `UtteranceEnd` turn signal (empty
text), stalling turns to the 8s timeout when `speech_final` did not fire. Fixed: `session.py`
accumulates FINAL text in `_utterance_parts` and, on any ENDPOINT, flushes the accumulated text as
the turn (falling back to the event's own text); only genuinely empty turns are ignored
("endpoint_empty"). Tests: turn assembled from FINAL + empty UtteranceEnd; pure-noise empty ignored.

### F5. ElevenLabs opened a new WebSocket per sentence (was O1; biggest latency cost)
`synthesize()` connected a fresh WebSocket per sentence, paying a TCP/TLS + BOS handshake before
each sentence (~50-200ms compounding) and corrupting socket state between turns (turn-2
`WebSocketDisconnect` / "Cannot call send once a close message has been sent"). Fixed: `tts/elevenlabs.py`
holds ONE persistent multi-context socket per call (`_ensure_socket`/`_drop_socket`/`_synthesize_multi`),
pre-warmed at `connect()`, streaming each sentence as init+text+flush+close_context and reconnecting
only on real error (`_synthesize_oneshot` kept as fallback). This was the dominant win: first-audio
dropped to ~130ms. Test: `tests/test_elevenlabs_persistent.py` (one connect across sentences).



### F1. websockets v12 vs v13 kwarg (call-killer)
`stt/deepgram.py` called `websockets.connect(url, additional_headers=...)` (the v13+ name) while the
project pins `websockets>=12,<13`, where the kwarg is `extra_headers`. Deepgram connects first, so
the whole session died instantly with a `TypeError` that was swallowed. Fixed to `extra_headers`
(matching the ElevenLabs client). Regression test: `tests/test_ws_connect.py`.

### F2. Errors were swallowed silently (debugging blind)
The server `/stream` handler caught exceptions with a bare `pass`, and the session result was only
in the outcomes CSV. Fixed: server configures a stdout log handler for the `voiceagent` logger, the
handler logs failures, and the session writes a full per-call event timeline to
`call_logs/events/<call_sid>.jsonl`.

### F3. Greeting cut off; noise treated as a turn
On a live call the contact saying "hello/yeah" or echo/noise triggered barge-in that cut off the
greeting (and the AI disclosure), and empty/noise endpoints made the agent re-prompt. Fixed: the
opening greeting is non-interruptible (also a compliance win, the disclosure always plays), barge-in
requires real transcribed words past an echo-guard window (`session.py` `_maybe_barge_in`), and a
playback wait keeps the silence timer from starting while our own audio is still playing. (Note: the
empty-endpoint part of this fix over-corrected into O2, now being refined.)

### F4. Hermetic test failures on a populated .env
`tests/test_cli.py::test_dial_without_keys_fails_clearly` and `tests/test_server.py::test_health`
failed on a machine with a filled `.env`, because `settings_from_env()` calls `load_dotenv()` which
reloaded the real keys and undid the tests' `monkeypatch.delenv`. Fixed by patching
`dotenv.load_dotenv` to a no-op in those tests.

---

## Gotchas (environment / live-call operations)

### G1. Twilio trial accounts require pressing a key
On a trial account an outbound call plays "you have a trial account ... press any key to execute
your code," and the TwiML (`<Connect><Stream>`) only runs AFTER the contact presses a digit. If you
do not press a key, the call ends after the recording and the Media Stream never connects. Press 1
when prompted. Upgrading the Twilio account removes this.

### G2. Port 8000 is the user's Django app
Something already listens on `:8000` (a Django dev server) that we did not start. Do NOT kill it.
Run VoiceAgent on `:8080` (`voiceagent serve --port 8080`); ngrok forwards any port.

### G3. ngrok is required for the public WSS URL, and it rotates
Twilio Media Streams needs a public `wss://` URL. Run `ngrok http 8080`, read the public URL from
`http://localhost:4040/api/tunnels`, and pass it at runtime:
`PUBLIC_BASE_URL=https://<id>.ngrok-free.app python -m voiceagent.cli dial ...`. The free URL changes
on every ngrok restart. The WSS upgrade passes through ngrok fine (no interstitial issue observed).

### G4. The chained-websockets question (decided)
Considered rewriting to a unified realtime API to avoid chaining three sockets. Findings: the lag is
implementation (O1/O2), not the architecture. There is no Claude realtime voice API as of 2026, so a
unified realtime rewrite (OpenAI Realtime ~820ms ~$0.06/min, or Gemini Live ~3s) means leaving Claude
and the assembled-pipeline learning goal. Decision: keep the architecture, fix and tune it; the user
will add an OpenRouter key for model flexibility (an OpenRouter LLM is added behind the `LLM`
interface). Commit and push before any future pivot so trial-and-error is visible.

### G5. Measured costs are lower than some public estimates
Our cost meter measured roughly $0.07-0.10 per call with Haiku, far below the $0.25-0.33/min some
2026 comparisons cite for premium tiers. Keep using the real meter, not external estimates.
