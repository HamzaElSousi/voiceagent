# VoiceAgent Build Tasks

Tracking the build against the approved plan. Live test call is the final, key-gated milestone.

Legend: [x] done, [~] in progress, [ ] not started

## Decisions (locked)
- LLM: configurable behind interface, Haiku default (`claude-haiku-4-5`)
- Pipeline: assembled + realtime behind one `Pipeline` interface; realtime is a stub this milestone
- Build order: all modules first, then integrate
- Compliance: full, on by default
- Config surfaces: YAML (prod) + CLI (automation) + demo Web UI (demos)
- Cost caps: $0.15/call, $25/campaign (configurable)
- Env: WSL2 + Python 3.11 venv, Docker available

## Phase 0 - Scaffold + lock contracts
- [x] pyproject.toml, .gitignore, .env.example
- [x] examples/agent.yaml, examples/contacts.csv
- [x] config.py (Pydantic models, secret-rejection, env settings)
- [x] contracts.py (STT / TTS / LLM / Telephony / MediaStream / Pipeline / CostMeter protocols + dataclasses)
- [x] tests/fakes.py (FakeSTT / FakeTTS / FakeLLM / FakeMediaStream / FakeTelephony)
- [x] venv + editable install + import/config smoke check (all checks passed)

## Phase 1 - Provider research (cheat-sheets) -> docs/research/
- [x] Deepgram streaming STT (wss listen, encoding=mulaw&sample_rate=8000, endpointing/speech_final, KeepAlive)
- [x] Twilio Media Streams (Connect/Stream bidirectional, media JSON, clear/mark barge-in, 160-byte frames)
- [x] ElevenLabs WS TTS (stream-input, output_format=ulaw_8000, eleven_flash_v2_5, BOS/text/EOS, close-to-cancel)
- [x] Anthropic streaming (claude-haiku-4-5, AsyncAnthropic messages.stream, usage, $1/$5 per Mtok)

## Phase 2 - Core conversation logic (verified with fakes)
- [x] engine.py + state machine (main) - greeting/value-prop/objection/booking/opt-out/limit paths pass
- [x] intent.py (regex priority: opt-out > end > booking > objection > LLM fallback > neutral)
- [x] compliance.py (disclosure, opt-out + DNC persistence, recording consent, calling hours)

## Phase 3 - Provider clients behind interfaces (Sonnet agent team, all conform + tested)
- [x] llm/claude.py (claude-haiku-4-5, AsyncAnthropic streaming, usage capture) - 10 tests
- [x] stt/deepgram.py (wss listen, speech_final/UtteranceEnd, audio_seconds metering) - 9 tests
- [x] tts/elevenlabs.py + sentence splitter (ulaw_8000, flash, close-to-cancel) - 24 tests
- [x] telephony/twilio.py (Connect/Stream TwiML, media JSON, clear barge-in) - 24 tests
- [x] pipeline/ (AssembledPipeline composes STT+TTS+sentence streaming+barge-in; RealtimePipeline stub)

## Phase 4 - Integration session core (verified end-to-end with fakes)
- [x] cost.py (DollarCostMeter four meters + CampaignBudget)
- [x] text.py (SentenceAggregator for sentence-level streaming)
- [x] session.py (CallSession loop + barge-in cancellation, verified)
- [x] fail-soft handling (provider error -> no dead air, logged)
- [x] per-hop latency instrumentation (endpoint -> first audio, perf_counter)
- [x] logger.py (CSV + webhook + composite sinks)
- [x] call_manager.py (load_contacts w/ aliases, CampaignRunner, budget + compliance gate)
- [x] builder.py (wiring factory) + server.py (FastAPI + Twilio WS /stream) + cli.py (dial/run-list/test-voice/serve)

## Phase 5 - Tests, Docker, packaging (written; user runs pytest)
- [x] test_config, test_engine, test_intent, test_compliance, test_cost, test_text,
      test_session (incl. barge-in + fail-soft), test_call_manager, test_logger, test_cli, test_server
- [x] provider unit tests from Phase 3 (deepgram, sentence splitter, claude, twilio frames)
- [x] Dockerfile + docker-compose.yml
- [x] full non-pytest wiring smoke passes; pytest command handed to user (never auto-run)

## Phase 6 - Demo Web UI (mocked, no keys)
- [x] FastAPI-mounted /ui page: edit + validate agent.yaml, see pipeline/llm, run mocked call,
      show transcript + outcome + p50 latency + cost. Served on the same app as /stream.

## Follow-ups (done after first full pytest run)
- [x] Fixed 4 failing tests: 'Full Name' header alias + phone +/no-+ matching (code bugs);
      fail-soft turns never hit LLM + sentence aggregator $ emission (test expectations). 21 passed.
- [x] Call-SID correlation: contact info threaded as XML-escaped TwiML <Parameter>s through
      place_call; server keys served CallResult to contact by real call_sid; reconcile via
      outcomes CSV. New tests/test_correlation.py (8) + test_twilio_frames (24) = 32 passed.
- [x] SETUP_TODO.md written (account/key checklist, links, .env map, 3-terminal live sequence).

## Phase 7 - Live test call (gated on keys) - DONE for milestone 1 (calls connect + book)
- [x] Keys placed; live calls connected; a call booked a meeting end to end
- [x] Found + fixed live bugs: websockets header, trial "press 1", empty-endpoint, false barge-in
- [x] Per-call event timeline logging added (call_logs/events/<sid>.jsonl)
- [~] Latency: measured ~892ms, felt laggy -> Milestone 2 (below)

## Milestone 2 - Latency + turn-taking fixes (see ISSUES.md, plan file)
- [x] M2-0 Docs: ISSUES.md (Open/Fixed/Gotchas) + CLAUDE.md project orientation
- [x] M2-1 Persistent ElevenLabs multi-context WebSocket (was 1 socket per sentence) + fallback; test
- [x] M2-2 Restore turn detection: assemble turn from accumulated FINAL text, flush on speech_final
      OR UtteranceEnd; ignore only true-noise empty endpoints; tests
- [x] M2-3 endpointing_ms (250) + utterance_end_ms (1000) configurable in agent.yaml -> deepgram URL
- [x] M2-4 Per-hop latency: endpoint -> LLM first token -> TTS first audio -> sent, into events log
- [x] M2-5 OpenRouter LLM behind the LLM interface (llm.provider: openrouter); Claude stays default; test
- [x] Full suite green (144 tests); realistic timing sim: 0 stalls, 0 false barge-ins, books
- [x] M2-6 commit + push, then live retest with per-hop p50 report
      Live retest 2026-07-02 (SID CA02e7a1...2380): endpoint->first-audio p50 ~130ms
      (turns: 141ms, 119ms) vs ~892ms before. Outcome booked_meeting, 0 false barge-ins,
      clean end, no socket errors. Perceived ~400ms incl. Deepgram 250ms endpointing.
