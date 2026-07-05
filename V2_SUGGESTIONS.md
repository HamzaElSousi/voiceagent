# VoiceAgent — V2+ Suggestions

**Current state:** full streaming pipeline (Deepgram STT → Claude → ElevenLabs TTS over Twilio Media Streams) built and verified against mocked providers — 18 test files, barge-in, compliance gates, cost metering, Dockerfile. The gap between "built" and "proven" is one live call.

## Prioritized suggestions

1. **The scripted benchmark run** — Effort: S
   *What:* One command (`python -m voiceagent.bench --number +1...`) that places a real call, captures p50/p95 speech-to-speech latency, per-hop timings, and actual cost, then writes them into the README table.
   *Why:* This single run converts the project's biggest weakness (no measured numbers) into its strongest interview line ("sub-800ms measured, here's the per-hop breakdown"). Everything is already instrumented.

2. **CI pipeline** — Effort: S
   *What:* GitHub Actions: pytest (all-mocked suite runs offline by design), ruff, and a Docker build check on every push.
   *Why:* 18 test files with no green badge is credibility left on the table; the suite already needs zero secrets.

3. **Call recording + transcript review UI** — Effort: M
   *What:* Persist per-call transcripts with timing annotations; a minimal web page listing calls with playback of what the agent heard/said and where barge-ins fired.
   *Why:* Debugging voice agents from CSV logs is archaeology; this is also the demo asset for the portfolio.

4. **Latency budget enforcement** — Effort: M
   *What:* Per-hop latency budgets in agent.yaml with automatic degradation (shorter model context, faster TTS voice) when a hop blows its budget mid-call.
   *Why:* Turns the 800ms wall from an aspiration into an enforced contract — a systems-engineering story no wrapper project has.

5. **Eval suite for conversation quality** — Effort: M
   *What:* Feed recorded transcripts through EvalKit (sibling repo): objection-handling correctness, compliance-line presence, booking-intent detection accuracy.
   *Why:* Connects two portfolio projects into one pipeline and answers "how do you know the agent is any good?"

6. **Inbound mode** — Effort: L
   *What:* Accept inbound Twilio calls with the same pipeline (answering service / booking line use case).
   *Why:* Doubles the addressable use cases with mostly-shared code.

## Quick wins (do this weekend)
- Wire the CI workflow (the tests already run offline)
- Run the scripted benchmark call and fill the README table
- Record one demo call and link the audio in the README
