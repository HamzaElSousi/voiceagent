# VoiceAgent - Next-Session Kickoff

Everything needed to start the VoiceAgent build cold in a new session. The full spec,
architecture, state machine, and latency budget are in `PRD.md`.

VoiceAgent is the real-time one: an AI outbound sales agent that makes real phone calls
(Twilio Media Streams -> Deepgram STT -> Claude Haiku -> ElevenLabs TTS), configured by a
single `agent.yaml`, targeting sub-800ms speech-to-speech latency at roughly $0.05 per call.
It is the first project in the series where latency is a hard wall, not a nice-to-have.

---

## Kickoff prompt (paste this to start the session)

> Start the VoiceAgent build. Read `voiceagent/PRD.md` first (full spec: architecture, the
> conversation state machine, the latency budget, repo structure, success criteria), then
> propose the build phases and begin.
>
> **Before writing any code or a detailed plan, use the `AskUserQuestion` tool to confirm the
> open decisions in the "Decisions to confirm" section below.** Ask them in one batched call,
> wait for my answers, and treat my choices as binding for the rest of the build.
>
> **Manage the main context using the "Orchestration rules" in this file.** Lock the shared
> contracts (the STT / TTS / LLM / telephony interfaces, the Pydantic config models, the
> `CallSession` protocol) in this main session first, then delegate bounded module work to
> subagents that build against the fixed contract and return a compact summary, not a file
> dump. Keep this session focused on decisions, contracts, and integration.
>
> Stack baseline (from the PRD, and subject to the questions above): **Twilio Media Streams** for telephony, **Deepgram** streaming STT,
> **Claude Haiku** for the LLM (lowest time-to-first-token), **ElevenLabs Turbo** for TTS, a
> **FastAPI + asyncio** server, and a YAML-configured agent validated with Pydantic. One
> `CallSession` per call; stream end to end so TTS on sentence 1 starts while the LLM is still
> writing sentence 2.
>
> Build everything that does NOT need live API keys or a phone number: the ConversationEngine
> and state machine, intent / objection / booking / end-signal detection, `agent.yaml` parsing
> (Pydantic), the provider abstractions (STT / TTS / LLM interfaces) and their WebSocket
> clients behind mockable seams, the Twilio Media Streams WebSocket handler, the CSV contact
> loader, a per-call cost tracker with a hard budget cap, the Typer CLI (`dial`, `run-list`,
> `test-voice`), Docker, and tests driven by fake STT / TTS / LLM clients (zero network, zero
> real calls in CI).
>
> Apply the portfolio best practices in `KICKOFF.md`: provider-agnostic Strategy-pattern
> interfaces, config-as-contract validated at load, a hard per-call cost cap that ends the
> call gracefully, fail-soft on any provider error (never leave dead air), compliance designed
> in (AI disclosure, opt-out / do-not-call handling, recording consent), and the
> scripted-fake-client test pattern. Use **asyncio** (this is I/O-bound real-time, unlike
> AgentForge's thread pool), and design the **barge-in** cancellation path from the start.
>
> I will set up the accounts and keys per the "Manual todos" below (Twilio number, Deepgram,
> ElevenLabs, Anthropic). When the pipeline is ready, hand me the exact steps to place the keys
> and run one live test call to my own phone; that live run is where the real timing and
> barge-in bugs surface (it always is). Then we measure the latency budget honestly and fill
> the README.
>
> No em dashes in any docs or the README. Do not run pytest automatically; give me the command.

---

## Decisions to confirm (the build agent asks these via AskUserQuestion)

At the start of the build, the agent batches these into a single `AskUserQuestion` call (up to
four questions per call) and waits. Recommended option is listed first in each. These are the
forks that change the architecture, so they are verified before any code is written, not assumed.

**Q1. LLM for the agent turns** (latency vs reasoning)
- *Claude Haiku (recommended)* - lowest time-to-first-token, cheap, fine for short sales turns
- *Claude Sonnet* - better objection handling, but higher latency and cost, risks the 800ms budget
- *Configurable, Haiku default* - ship the interface, default to Haiku, one-line swap later

**Q2. Pipeline approach** (control vs simplicity)
- *Assembled best-of-breed (recommended)* - Deepgram + Claude + ElevenLabs, full control of the
  latency budget; this is the learning goal of the project
- *Unified Realtime API* (OpenAI Realtime / Gemini Live) - one socket, fewer hops, but less control
  and far less to learn about the streaming problem
- *Both behind one interface* - future-proof, more work up front

**Q3. First milestone** (de-risk integration vs clean units)
- *Thin vertical slice (recommended)* - one hardcoded script, one live call to your own phone, end
  to end, before building every module; surfaces the real-timing bugs earliest
- *All modules then integrate* - cleaner unit tests first, integration risk deferred to the end

**Q4. Compliance posture** (safe-by-default vs demo-speed)
- *Full compliance baked in (recommended)* - AI disclosure line, opt-out / do-not-call handling,
  recording consent, and calling-hour limits, all on by default
- *Minimal for the demo* - disclosure line only, the rest stubbed as clearly-marked TODOs

If my answer to any question implies others (for example, picking the Realtime API changes the STT
and TTS choices), the agent confirms the follow-on change rather than guessing.

---

## Orchestration rules (managing the main context)

The goal: keep this main session lean and authoritative, and push bounded, context-heavy, or
parallelizable work to subagents. A bloated orchestrator loses the plot; a disciplined one stays
in control of the contracts and the integration.

**The main session owns** the decisions (the AskUserQuestion answers), the shared contracts, the
running plan, and integration. It does not read large docs or generate big files directly when a
subagent can do that and hand back a summary.

**Contracts before fan-out (the AgentForge habit).** Write the interfaces first, in the main
session: the `STT` / `TTS` / `LLM` / telephony protocols, the Pydantic `agent.yaml` models, and the
`CallSession` loop signature. Lock them. Only then is parallel module work safe, because every
subagent codes against a fixed signature and the pieces snap together on first import.

**When to spawn a subagent** (otherwise do it inline; every spawn starts cold and re-derives context):
- **Research** a provider's specifics (Deepgram streaming params and endpointing, Twilio Media
  Streams frame format and mu-law encoding, ElevenLabs WebSocket protocol, Anthropic streaming).
  The subagent reads the docs and returns a one-page cheat-sheet, not the raw pages.
- **Explore** (read-only) for broad searches across the repo or for "where is X" questions.
- **Implement a single module** once its contract is fixed: hand the subagent the interface, the
  relevant file paths, and the fake-client test it must pass. It builds the module and returns a
  compact summary plus the diff. Good candidates: `stt/deepgram.py`, `tts/elevenlabs.py`,
  `llm/claude.py`, `intent.py`, `logger.py`.

**What stays in the main session** (do not delegate): the `CallSession` streaming loop and barge-in
cancellation (the integration core), final wiring, and any decision that changes a contract.

**How subagents report.** Compact summaries only: what changed, file paths, key decisions, and
gotchas. Never a full file dump back into the main context. If a subagent needs to read something
large (verbose docs, a long transcript), it reads it and returns only the distilled result.

**Parallelism.** Fan out independent modules concurrently; serialize anything that touches the
shared session loop or a shared contract. This mirrors the project's own concurrency rule: parallel
where the work is truly independent, sequential where it shares state.

**Cold-start discipline.** Each subagent gets the contract, the file paths, and the acceptance test
in its prompt. Assume it has none of this conversation's history. Verify a subagent's output against
its acceptance test before integrating; do not trust a summary blindly.

**Verification.** Running the test suite or a lint pass can be delegated, but the main session reads
the result and owns the green/red call. Per the standing rule, give me the pytest command rather than
running it automatically.

---

## Best practices to carry forward

Distilled from DocChat, ReviewBot, AgentForge, EvalKit, and LoRAForge. These are the habits
that made each project production-minded instead of demo-ware; they all transfer to voice.

### Carried from the whole series

1. **Provider-agnostic Strategy-pattern interfaces.** Like AgentForge's `LLMClient` and
   LoRAForge's serve layer: put one interface in front of each external dependency (an `STT`,
   a `TTS`, an `LLM` interface) so Deepgram can swap for Whisper, ElevenLabs for Cartesia, and
   Claude for a local model, as a one-line config change. The session loop never changes.
2. **Cost control is first-class, not an afterthought.** AgentForge's hard dollar cap, applied
   to voice: track per-call cost across all four meters (Deepgram seconds, Claude tokens,
   ElevenLabs characters, Twilio minutes), enforce a per-call and per-campaign cap, and end the
   call gracefully with a logged reason before it overspends. The PRD's whole pitch is cost, so
   the meter has to be real.
3. **Config-as-contract: validate at load, before spending.** Like AgentForge pipelines and the
   LoRAForge notebook config: `agent.yaml` is data, validated by Pydantic at load time so a bad
   script or a missing field fails before a single call is dialed (and before any telephony
   money is spent).
4. **Test non-determinism deterministically.** The reusable pattern across every project: a
   scripted fake client. Here that means fake STT (canned transcripts), fake TTS (canned audio
   frames), and a fake LLM (canned responses), so the state machine, intent detection, and
   turn-taking are tested with zero network and zero real calls in CI. Real APIs only in a
   manual end-to-end. Assert on outcomes and state transitions, not timing.
5. **Fail-soft / graceful degradation.** ReviewBot never failed CI on an LLM error; EvalKit
   records a failing case instead of crashing. For voice the bar is higher: if STT, TTS, or the
   LLM errors or times out mid-call, never leave dead air. Recover, say a safe filler line, or
   end the call cleanly and log the reason. A provider hiccup must not kill the session.
6. **Build from scratch to understand the stack.** AgentForge wrote its own ReAct loop;
   LoRAForge assembled its own train-to-serve pipeline. Here: assemble Twilio + Deepgram +
   ElevenLabs yourself rather than using Vapi or Retell, so you actually understand the
   streaming and latency problem, which is the entire point of the project.
7. **Honest measurement, including the misses.** LoRAForge reported where it fell short of its
   targets. Measure each hop of the latency budget with `perf_counter`, report real p50 / p95
   speech-to-speech, and say plainly when a call exceeds 800ms and why. The per-hop breakdown is
   the signal, not a single rounded number.
8. **Verify live, not just mocked.** AgentForge's first real run surfaced three integration bugs
   the ~250-test mocked suite could not. Expect the same here: barge-in, audio-format, and
   sample-rate bugs only appear on a real call. Plan a live test call to your own phone as a
   first-class milestone, not an afterthought.
9. **Match the concurrency primitive to the workload.** LoRAForge and AgentForge lesson stated
   the other way: AgentForge used threads because its clients were sync and I/O-bound. Voice is
   natively async streaming over WebSockets, so **asyncio is the right primitive** here, one
   event loop driving many concurrent `CallSession`s. Do not bolt threads onto an async stack.
10. **Headless and automatable; minimize the manual surface.** Like driving LoRAForge's GPU run
    through the Kaggle API: the campaign runs headlessly from the CLI (`run-list contacts.csv`),
    and the only manual steps are one-time account and key setup.

### New for voice (the real-time domain)

11. **Stream everything; never wait for a full step.** The PRD's key insight: start TTS on the
    first sentence while the LLM writes the rest, and forward audio to Twilio as it arrives. Any
    step that waits for the previous one to fully finish blows the latency budget.
12. **Barge-in (interruption) is a core feature, not an edge case.** When the contact starts
    speaking mid-response, cancel the in-flight LLM generation and TTS immediately and start
    listening. Design this cancellation path up front (cooperative cancellation via an asyncio
    event or task-cancel, the async cousin of AgentForge's `threading.Event`), because retrofitting
    it later means rewriting the session loop.
13. **Endpoint detection (VAD) decides turn-taking.** Use Deepgram's endpointing (about 300ms of
    silence) to know when the user finished, but make the threshold configurable; too short
    interrupts the contact, too long feels laggy.
14. **Latency budget is a design constraint from line one.** Pick the lowest-latency options on
    purpose (Haiku for TTFT, ElevenLabs Turbo), keep the LLM responses short (the persona caps at
    ~2 sentences), and measure each hop so a regression is obvious.
15. **Compliance is a safety layer, like SSRF was for AgentForge.** Outbound sales calls carry
    legal weight (TCPA and similar). Bake in: a clear AI-disclosure line in the greeting,
    call-recording consent where required, opt-out handling (an "remove me" / "do not call"
    utterance suppresses the contact and logs it), and configurable calling-hour limits. Keep all
    secrets in `.env`, never in `agent.yaml`.

---

## Your manual todos (the live-call blocker)

VoiceAgent needs real accounts and keys to place a call, the same way LoRAForge needed a GPU.
I can build and test the entire pipeline with mocked providers without any of these; you only
need them for the live test call and real campaigns.

### Accounts + keys (~20-30 min, mostly free trials)
- [ ] **Twilio** account, then buy a phone number with **Voice** capability, and note
      `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`. Media Streams needs a
      public HTTPS/WSS URL, so plan to expose the local server with **ngrok** (or a deployed host)
      during testing.
- [ ] **Deepgram** account + `DEEPGRAM_API_KEY` (streaming STT; generous free credit).
- [ ] **ElevenLabs** account + `ELEVENLABS_API_KEY`, and pick a `voice_id` (Turbo model for
      latency).
- [ ] **Anthropic** account + `ANTHROPIC_API_KEY` (Claude Haiku).
- [ ] A **phone to receive the test call** (your own number) so the first live run is to yourself.

### Notes
- These go in `.env` (gitignored), never in `agent.yaml`.
- Trial accounts often restrict outbound calls to **verified numbers**; verify your own number
  in Twilio first so the test call connects.
- Keep the first campaign tiny (one contact: you) until the latency and barge-in behave.

---

## What I build without any keys (the cold-start scope)

- `config.py`: `agent.yaml` -> Pydantic models, validated at load
- `engine.py` + state machine: GREETING -> VALUE_PROP -> OBJECTION / BOOKING -> END
- `intent.py`: objection / booking / end-signal detection (regex first, LLM fallback)
- `stt/`, `tts/`, `llm/`, `telephony/`: provider interfaces + WebSocket clients behind mockable seams
- `session.py`: one `CallSession`, the streaming loop, barge-in cancellation
- `call_manager.py`: CSV contact loader, dial queue, per-call cost tracker + budget cap
- `cli.py`: `dial`, `run-list`, `test-voice`
- `logger.py`: CSV + webhook outcome logging
- `tests/`: fake STT / TTS / LLM, deterministic state-machine and intent tests, zero live calls
- `Dockerfile`, `docker-compose.yml`, `.env.example`, `README.md`

Then you place the keys, we run one live call to your phone, fix the real-timing bugs, measure
the latency budget honestly, and fill the README.
