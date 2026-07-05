# VoiceAgent

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-mocked%20providers%2C%20zero%20network-brightgreen.svg)](#status)

AI outbound sales agent that makes real phone calls — streaming STT → LLM → TTS with barge-in,
compliance gates (disclosure, opt-out, calling hours), and per-call cost metering. Configure the
script in YAML, own your cost.

VoiceAgent assembles the same stack the commercial voice platforms use (Twilio Media Streams to
Deepgram STT to Claude Haiku to ElevenLabs TTS) into a headless tool you run yourself. It dials
contacts from a CSV, holds a configurable sales conversation, handles objections, attempts to
book a meeting, and logs the outcome. The whole script and persona live in one `agent.yaml`.

The hard problem here is latency: voice has an 800ms wall where text generation does not. The
pipeline streams end to end, so synthesis of sentence one starts while the model is still writing
sentence two, and a barge-in cancels the in-flight response the instant the contact speaks.

## Status

The full pipeline is built and verified with mocked providers (zero network, zero real calls).
The one remaining step is the live test call to a real phone, which is gated on your API keys.
See "Live test call" below.

- Built and tested with fakes: config, conversation state machine, intent detection, compliance,
  cost metering, the assembled pipeline, the CallSession streaming loop, barge-in, fail-soft, the
  CSV and webhook loggers, the campaign runner, the Twilio Media Streams handler, the Deepgram /
  ElevenLabs / Claude clients, the CLI, and a demo web console.
- Not yet measured: real speech-to-speech latency and real per-call cost. Those numbers come from
  the live call and are filled into the "Measured" section after the first run.

## How it works

```
Contact phone  <->  Twilio Media Streams  <->  VoiceAgent (FastAPI + asyncio)
                                                  |  Deepgram STT  (endpoint detection)
                                                  |  Claude Haiku  (streamed, sentence by sentence)
                                                  |  ElevenLabs    (Turbo, mu-law 8kHz out)
```

Every external dependency sits behind an interface (`STT`, `TTS`, `LLM`, `Telephony`, `Pipeline`)
defined in `voiceagent/contracts.py`, so Deepgram can swap for another STT, ElevenLabs for another
TTS, and Claude for another model with a one-line config change. The session loop never changes.

The conversation is a state machine: DIALING to GREETING to VALUE_PROP to OBJECTION or BOOKING to
END. Objections loop back to the pitch up to a configurable limit; a booking signal books the
meeting; an opt-out phrase is a compliance hard stop.

## Three ways to drive it

1. **YAML** is the contract: `examples/agent.yaml` holds the persona, script, objections, signals,
   pipeline mode, cost caps, and compliance policy. It is validated by Pydantic at load, before
   any call is dialed. Secrets never go here.
2. **CLI** for automation: `voiceagent dial`, `voiceagent run-list contacts.csv`, and a no-keys
   `voiceagent test-voice` that runs a fully mocked call so you can see a conversation end to end
   without spending anything.
3. **Demo web console** at `/ui`: edit and validate the config, pick the pipeline mode, and watch
   a mocked call's transcript, outcome, p50 latency, and cost. Localhost demo tool, no auth.

## Quick start (no keys, mocked)

```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"

# A full mocked conversation in your terminal, zero API keys:
python -m voiceagent.cli test-voice

# Or the demo web console:
python -m voiceagent.cli serve     # then open http://localhost:8000/ui
```

## Run the tests

The suite is deterministic and offline (scripted fake STT / TTS / LLM, no network, no real calls):

```bash
. .venv/bin/activate
python -m pytest -q
```

## Configuration

Everything is in `examples/agent.yaml`. Copy it, edit the persona and script, and point the CLI
at it with `--config`. Highlights:

```yaml
llm:
  model: claude-haiku-4-5    # swap to claude-sonnet-4-6 in one line
pipeline:
  mode: assembled            # assembled (Deepgram + Claude + ElevenLabs) or realtime (stub)
endpointing_ms: 300          # silence that marks the contact finished a turn
limits:
  per_call_usd: 0.15         # the call ends gracefully before it overspends
  per_campaign_usd: 25.00
compliance:                  # on by default
  disclose_in_greeting: true
  opt_out_phrases: ["remove me", "do not call"]
  calling_hours: { start: "09:00", end: "20:00", timezone: "America/New_York" }
```

## Compliance

Outbound sales calls carry legal weight (TCPA and similar), so compliance is on by default: an AI
disclosure line in the greeting, opt-out and do-not-call handling (a "remove me" utterance
suppresses the contact and is logged, and suppressed numbers are skipped before dialing), optional
recording consent, and timezone-aware calling-hour limits. All secrets stay in `.env`.

## Live test call (gated on your keys)

To place a real call you need accounts and keys (mostly free trials):

1. Twilio account, a Voice-capable phone number, and your `TWILIO_ACCOUNT_SID`,
   `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`. On a trial account, verify your own number first so
   the test call connects.
2. Deepgram `DEEPGRAM_API_KEY` (streaming STT).
3. ElevenLabs `ELEVENLABS_API_KEY` and a `voice_id` (Turbo model for latency).
4. Anthropic `ANTHROPIC_API_KEY` (Claude Haiku).
5. A public HTTPS/WSS URL for Twilio Media Streams. Locally, expose the server with ngrok and set
   `PUBLIC_BASE_URL` to the ngrok https URL.

```bash
cp .env.example .env        # fill in the six keys plus PUBLIC_BASE_URL
. .venv/bin/activate

# Terminal 1: the server
python -m voiceagent.cli serve
# Terminal 2: expose it
ngrok http 8000             # copy the https URL into PUBLIC_BASE_URL in .env

# Terminal 3: call your own (verified) phone
python -m voiceagent.cli dial "+1YOURNUMBER" --name "Your Name"
```

Keep the first campaign to one contact (you) until the latency and barge-in behave on a real call.

## Benchmarks — pending the first scripted live run

Real speech-to-speech numbers require a live call (gated on API keys), so this repo does not
print invented latency figures. The pipeline is instrumented to capture them on the first run:

| Metric | Target | Status |
|---|---|---|
| Speech-to-speech latency p50 / p95 | < 800 ms / < 1500 ms | instrumented, awaiting live run |
| Per-hop breakdown (endpoint→first token, first token→first audio) | — | instrumented, awaiting live run |
| Cost per 3-minute call | < $0.15 | cost meter built, awaiting live run |

The scripted benchmark run is item #1 in [V2_SUGGESTIONS.md](V2_SUGGESTIONS.md).

## Repo layout

```
voiceagent/
  config.py            agent.yaml to validated Pydantic models
  contracts.py         STT / TTS / LLM / Telephony / Pipeline / CostMeter interfaces
  engine.py            conversation state machine
  intent.py            objection / booking / end / opt-out detection
  compliance.py        disclosure, opt-out, consent, calling hours
  cost.py              per-call cost meter + campaign budget
  text.py              sentence aggregation for sentence-level streaming
  session.py           CallSession streaming loop + barge-in
  call_manager.py      contact loader + campaign runner
  builder.py           wiring factory (real providers or fakes)
  logger.py            CSV + webhook outcome logging
  cli.py               dial / run-list / test-voice / serve
  server.py            FastAPI Media Streams server
  web/                 demo console mounted at /ui
  stt/ tts/ llm/ telephony/ pipeline/   provider clients behind the interfaces
docs/research/         provider cheat-sheets used while building the clients
tests/                 deterministic, offline, fake-driven
```
