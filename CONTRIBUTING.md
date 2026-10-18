# Contributing to VoiceAgent

Thanks for helping out. VoiceAgent is a self-hosted AI voice agent, so changes touch real phone calls,
real money and real people. Please read this before opening a PR.

## Getting set up

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m voiceagent.cli test-voice      # full mocked call, no API keys needed
python -m pytest -q                      # deterministic, zero network
```

## Ground rules

- **Tests never touch the network.** Providers sit behind the Protocols in `voiceagent/contracts.py`;
  use the fakes in `tests/fakes.py`. A PR that needs live keys to pass will not be merged.
- **Never block the event loop on the call path.** No sync file or network I/O, sync imports or
  CPU-heavy work inside `async def` code that runs during a call. Use `asyncio.to_thread` or do it
  before the call starts. A stall over ~200ms dead-airs the agent and can deafen the call.
- **Compliance guards are not optional.** AI disclosure, opt-out handling, the do-not-call list,
  calling hours and the outbound consent gate must stay on. PRs that add a way to disable them will
  be closed.
- **No secrets or real data in the repo.** Keys live in `.env` or the dashboard. Never commit real
  phone numbers, transcripts or call logs. Use the reserved fictional range (`+1 212 555 0100` to `0199`) in examples and tests.
- Keep PRs focused. Add or update tests for behaviour changes, and update docs when you change
  config, CLI flags or the API.

## Reporting bugs

Open an issue with the bug template. For live-call bugs, attach the relevant lines from
`call_logs/events/<call_sid>.jsonl` with phone numbers and names redacted.

Security issues: see [SECURITY.md](SECURITY.md). Please do not file them as public issues.
