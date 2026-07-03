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

## Open

None. All Milestone 2 issues resolved and validated live (see Fixed F5-F8). Next possible work if
lag resurfaces: trim `endpointing_ms` below 250 to shave the ~250ms Deepgram VAD window off perceived
latency (trade: occasional early cut-in), or pivot to a single-socket realtime API (OpenAI Realtime)
if a further step-change is needed (leaves Claude; see G4).

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
