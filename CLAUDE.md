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
- `voiceagent/booking/` - real booking during the call: `flow.py` (BookingFlow: offer slots, pick,
  email capture, reserve), `calcom.py` (Cal.com API v2), `slots.py` / `email.py` (pure helpers),
  `demo.py` (offline DemoBooker for `test-voice --book`). The engine codes against `Booker`.
- `voiceagent/app/` - the platform (milestone 2): `models.py` (SQLAlchemy async), `repo.py`
  (`WorkspaceRepo` adds workspace_id to every tenant query), `crypto.py` (Fernet for BYO keys),
  `security.py` (argon2 in a thread, sessions, CSRF, rate limit), `api/` (JSON API at /api),
  `migrations/` (Alembic; `voiceagent db-upgrade`). SQLite in dev/tests, Postgres in production.
- `voiceagent/connectors/` - vendor-neutral CRM connectors (milestone 3): `mcp.py` (generic MCP
  Streamable HTTP client + field mapping), `webhook.py` (paged GET, HMAC-signed POST), `csvimport.py`,
  `mapping.py` (`$.a.b` paths, `{{placeholder}}` args), `urlsafety.py` (SSRF guard). Integrator docs:
  `docs/CONNECTORS.md`.
- `voiceagent/app/eligibility.py` + `voiceagent/phones.py` - who may be dialed: consent, DNC, E.164,
  callee-local hours inside TCPA 8am-9pm (every candidate zone). `app/leads.py` (sync/import/DNC),
  `app/outbox.py` (retry with backoff), `app/call_results.py` (Call row + lead status + DNC + outbox).
- Calls on the platform (milestone 4): `app/campaigns.py` (`tick()`: claim lead, then dial; DB-counted
  concurrency; budget/keys/agent checks), `app/dialer.py` (Twilio with the workspace's own account),
  `app/calltoken.py` (HMAC per-call token carried in stream params), `app/voice.py` (`/voice/inbound/
  {slug}`, `/voice/status`, `/voice/amd`, `load_platform_call`), `app/worker.py` (`voiceagent worker`:
  campaigns, outbox, connector sync, retention). `/stream` takes the platform branch when the start
  params carry `call_id`; otherwise it is the CLI dev mode from `agent.yaml`.
- `web/` - the React dashboard (milestone 5): Vite + TypeScript + Tailwind, pages in `web/src/pages`,
  API client `web/src/api.ts` (CSRF header from the `va_csrf` cookie). `npm run dev` proxies to
  `voiceagent serve --port 8080`; `npm run build` -> `web/dist`, served by FastAPI at `/` with an SPA
  fallback (`server._mount_dashboard`, registered last so it never shadows /api, /voice, /ui).
- `voiceagent/builder.py` - wires a `CallSession` from config + settings (inject fakes for tests).
- `voiceagent/server.py` - FastAPI: `WS /stream` (Twilio), `/health`, demo UI at `/ui`.
- `voiceagent/cli.py` - `test-voice` (mocked, no keys), `serve`, `dial`, `run-list`, plus instance
  admin: `init-instance`, `db-upgrade`, `create-admin`, `reset-password`, `worker`.
- `tests/` - deterministic, fake-driven, zero network. `tests/fakes.py` is the substrate.
- `call_logs/events/<call_sid>.jsonl` - per-call event timeline (the main live-debugging tool).

## How to run
- Self-hosted (production): `docker compose up -d` (see `docs/SELF_HOSTING.md`); entrypoint `docker/entrypoint.sh`
  runs `db-upgrade` then `serve` (app) or `worker`.
- Mocked call, no keys: `python -m voiceagent.cli test-voice` (add `--book` for the slot flow)
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

## Real-time rules (hard-won; do NOT relearn these the hard way)
The single loop drives audio in/out on tight deadlines. Anything that stalls it for >~200ms breaks
the call: it dead-airs the agent AND idle-kills the Deepgram socket (its keepalive stops), so the
call goes deaf for the rest of its life. See `ISSUES.md` F16.
- NEVER do blocking work on the event loop: no sync imports, sync file/network I/O, or CPU-heavy
  work inside `async def` on the call path. Wrap unavoidable blocking calls in `asyncio.to_thread`,
  or do them at startup before accepting calls (`cli.py serve` preloads `anthropic`/`httpx`).
- **`/mnt/c` (WSL2 9p) is very slow for file I/O**: `import anthropic` alone is ~14s cold there.
  This is why lazy provider imports must be preloaded/threaded. Best real fix: run from WSL-native
  ext4 (`~/`), not `/mnt/c`. Measure a suspected loop stall with a heartbeat task (worst gap).
- Latency numbers only mean something when the LLM is actually on the path. In M2 the "130ms" was a
  static script line; real LLM-driven turns floor around ~0.8-1.2s (LLM ~0.5s + TTS ~0.4s) + ~250ms
  endpointing. Do not chase sub-500ms for live LLM turns; it is not physical for this 3-hop chain.
- Each call builds its own provider clients, so per-call warmups matter: `session._prewarm_llm`
  warms the LLM behind the greeting. The greeting itself must not depend on a cold path.

## Current focus
Turning VoiceAgent into an open-source, self-hosted platform (plan: workspaces, BYO keys, MCP /
webhook / CSV connectors, Cal.com, Docker Compose + Caddy on any VPS). Milestone 1 (per-call config,
`mode: inbound|outbound`, Cal.com booking during the call, async AMD voicemail) and milestone 2
(DB, auth, workspaces, encrypted BYO keys, agents + calls API) and milestone 3 (MCP / webhook / CSV
connectors, outbox, consent + DNC + callee-local hours) and milestone 4 (campaign worker, platform
stream, inbound/status/AMD per workspace, test calls, numbers, retention), milestone 5 (React
dashboard) and milestone 6 (Docker Compose + Caddy self-hosting, `docs/SELF_HOSTING.md`, GHCR release
workflow, README) are in. Image smoke-tested against Postgres. Remaining: deploy to the user's VPS,
then connect Social-automate as the first tenant (work in the Social-automate repo + gitignored
`local/`, never in this repo).
Repo rule: nothing tenant-specific (e.g. Social-automate) is
ever committed; private config goes in gitignored `local/`.

## Key pointers
- Known issues and decisions: `ISSUES.md`
- Progress checklist: `TASKS.md`
- Spec and success criteria: `PRD.md`
- Live-call account/key setup: `SETUP_TODO.md`
