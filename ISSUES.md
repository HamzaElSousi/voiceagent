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

Latency is fixed (M2). The live call surfaced that the *conversation design* was the real problem:
the old engine was a rigid scripted rail that advanced on turn count. Decision (2026-07-02): rebuild
the conversation core as **LLM-driven + guardrails** (the LLM drives every turn; code owns only
compliance + booking guards). Done in code + tests (see F9-F12). Booking currently only LOGS the
intent; live cal.com booking is a future goal (PRD.md). Remaining open item is live validation:

### O9. Barge-in storm: one utterance re-triggers barge-in repeatedly
Live retest 2 (SID CAc871...ebd): the contact saying "Saturday at ten." fired barge_in ~6 times
(10 total in the call). The `_barge_in_locked` / one-shot guard is not holding across the partial+
final transcript updates of a single utterance, so each update re-triggers a barge-in. Fix: latch
barge-in per utterance until the agent's next turn actually starts, and/or debounce on the same text.

### O10. Echo: the agent's own speech is transcribed back as a contact turn
Same call: a "contact" endpoint read "In general, yes. This week works. I have availability on,
let's say," which is the agent's own offered times echoed into the STT, and it tangled the booking.
Without acoustic echo cancellation, agent audio leaks into Deepgram. Fix: stronger echo gating
(suppress transcripts while/just after the agent is speaking) or feed a reference for cancellation.

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
