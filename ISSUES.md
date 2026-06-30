# VoiceAgent Issues Log

A running record of issues found during the build and live testing, so a fresh session can resume
without re-discovering them. Grouped as Open, Fixed, and Gotchas. Newest context at the top of each
section. See `CLAUDE.md` for project orientation and `call_logs/events/<call_sid>.jsonl` for the
per-call timeline that drives most of this diagnosis.

## Status snapshot (2026-06-30)
The pipeline works mocked and on real calls (a call even booked a meeting), but live calls feel
laggy. Root cause is implementation, not the architecture. Milestone 2 is fixing latency and
turn-taking while keeping the Twilio -> Deepgram -> LLM -> ElevenLabs design and Claude as default.
Measured response lag was ~892ms; target is sub-500ms.

---

## Open (being fixed in Milestone 2)

### O1. ElevenLabs opens a new WebSocket per sentence (biggest latency cost)
`tts/elevenlabs.py` `synthesize()` calls `websockets.connect(...)` on every call, and the pipeline
calls `synthesize()` once per sentence. Each sentence pays a TCP/TLS handshake + BOS setup before
any audio (~50-200ms each, compounding across sentences). It also corrupts socket state between
turns, which caused a turn to fail with `WebSocketDisconnect` / "Cannot call send once a close
message has been sent". Fix: hold ONE socket open per call, stream each sentence as text+flush,
reconnect only on real error. (Milestone 2, fix #1.)

### O2. UtteranceEnd turn detection dropped (regression I introduced; multi-second stalls)
Deepgram signals end-of-turn two ways: a Results message with `speech_final=true` (carries text),
and a separate `UtteranceEnd` message (empty text, the noisy-telephony fallback). A session change
to "ignore empty-transcript endpoints" (added to suppress noise/echo) now also drops the
`UtteranceEnd` signal, so when `speech_final` does not fire the contact's turn is not detected until
the 8s silence timeout. Fix: accumulate transcript text and, on any ENDPOINT, flush the accumulated
text as the turn; ignore only when nothing accumulated. (Milestone 2, fix #2.)

### O3. Turn-end detection latency not yet tuned for sub-500ms
`endpointing_ms=300` and `utterance_end_ms=1000` are defaults in `stt/deepgram.py` and not yet
exposed in `agent.yaml`. Tune lower and make configurable. (Milestone 2, fix #3.)

### O4. Per-hop latency is not separable
Current logging gives one endpoint-to-first-audio number; we cannot tell LLM time from TTS time.
Add timestamps for endpoint, LLM first token, TTS first byte, audio first sent. (Milestone 2, fix #4.)

---

## Fixed

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
