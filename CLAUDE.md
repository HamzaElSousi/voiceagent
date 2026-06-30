# VoiceAgent (project orientation for Claude)

Read this first when picking up VoiceAgent in a fresh session. Then read `ISSUES.md` for the current
known issues and `TASKS.md` for progress.

## What it is
An AI outbound sales agent that places real phone calls. Pipeline:
`Twilio Media Streams -> Deepgram STT -> LLM (Claude Haiku default) -> ElevenLabs TTS`, one
`CallSession` per call, configured by a single `examples/agent.yaml`. Target: smooth, low-latency
(sub-500ms response) speech-to-speech. Full spec in `PRD.md`; orchestration history in `KICKOFF.md`.

## Where things are
- `voiceagent/contracts.py` - the locked `STT` / `TTS` / `LLM` / `Telephony` / `MediaStream` /
  `Pipeline` / `CostMeter` Protocols. Everything codes against these.
- `voiceagent/session.py` - `CallSession`: the streaming loop, turn-taking, barge-in, per-call event
  timeline. This is the integration core; change it carefully.
- `voiceagent/pipeline/assembled.py` - composes STT + TTS + sentence streaming. `realtime.py` is a stub.
- `voiceagent/stt/deepgram.py`, `voiceagent/tts/elevenlabs.py`, `voiceagent/llm/claude.py`,
  `voiceagent/telephony/twilio.py` - provider clients behind the interfaces.
- `voiceagent/builder.py` - wires a `CallSession` from config + settings (inject fakes for tests).
- `voiceagent/server.py` - FastAPI: `WS /stream` (Twilio), `/health`, demo UI at `/ui`.
- `voiceagent/cli.py` - `test-voice` (mocked, no keys), `serve`, `dial`, `run-list`.
- `tests/` - deterministic, fake-driven, zero network. `tests/fakes.py` is the substrate.
- `call_logs/events/<call_sid>.jsonl` - per-call event timeline (the main live-debugging tool).

## How to run
- Mocked call, no keys: `python -m voiceagent.cli test-voice`
- Demo console: `python -m voiceagent.cli serve` then open `http://localhost:8080/ui`
- Tests (do NOT auto-run; hand the command to the user): `python -m pytest -q`
- Live call (needs keys in `.env` + ngrok), see `SETUP_TODO.md`:
  1. `python -m voiceagent.cli serve --port 8080`  (port 8000 is the user's Django app; do not touch it)
  2. `ngrok http 8080`, get the URL from `http://localhost:4040/api/tunnels`
  3. `PUBLIC_BASE_URL=https://<id>.ngrok-free.app python -m voiceagent.cli dial "+1..." --name "..."`
  4. The contact must press 1 (Twilio trial gate). Then read `call_logs/events/<sid>.jsonl`.

## Conventions and rules
- Audio across all interfaces is 8 kHz mono mu-law (G.711). ElevenLabs is requested as `ulaw_8000`,
  so no transcoding for Twilio.
- Secrets live only in `.env`, never in `agent.yaml` (`config.py` enforces this).
- `websockets` is pinned `>=12,<13`: use `extra_headers`, not `additional_headers` (see `ISSUES.md` F1).
- Never run pytest automatically; give the user the command. No em dashes in docs.
- Commits only at a good working point; the user asked to commit + push before any architecture pivot.

## Current focus
Milestone 2: fix live-call latency and turn-taking (see `ISSUES.md` Open items O1-O4) while keeping
the architecture and Claude default. The decision to fix rather than rewrite to a unified realtime
API is recorded in `ISSUES.md` G4.

## Key pointers
- Known issues and decisions: `ISSUES.md`
- Progress checklist: `TASKS.md`
- Spec and success criteria: `PRD.md`
- Live-call account/key setup: `SETUP_TODO.md`
