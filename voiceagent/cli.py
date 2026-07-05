"""VoiceAgent CLI: dial, run-list, test-voice, serve.

test-voice is fully mocked and needs no API keys (a great first run). dial and run-list are live
and require the keys in .env plus a publicly reachable server (ngrok). serve runs the FastAPI app.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import typer

from .builder import build_session
from .call_manager import CampaignRunner, load_contacts
from .config import load_agent_config, settings_from_env
from .contracts import CallResult, Outcome
from .logger import build_outcome_sink

app = typer.Typer(add_completion=False, help="AI outbound sales agent. Own your cost.")


def _print_result(result: CallResult) -> None:
    typer.echo("")
    typer.echo("Transcript:")
    for turn in result.transcript:
        typer.echo(f"  {turn.speaker:>7}: {turn.text}")
    typer.echo("")
    typer.echo(f"Outcome:   {result.outcome.value}  ({result.end_reason})")
    typer.echo(f"Duration:  {result.duration_seconds:.2f}s")
    latencies = [s.endpoint_to_first_audio_ms for s in result.latencies if s.endpoint_to_first_audio_ms]
    if latencies:
        latencies.sort()
        typer.echo(f"Latency:   p50 {latencies[len(latencies)//2]:.0f}ms speech-to-speech ({len(latencies)} turns)")
    typer.echo(f"Cost:      ${result.cost.total_usd:.4f}")
    if result.error:
        typer.echo(f"Errors:    {result.error}")


@app.command("test-voice")
def test_voice(
    config: str = typer.Option("examples/agent.yaml", help="Path to agent.yaml"),
    script: Optional[str] = typer.Option(
        None, help='Pipe-separated contact turns, e.g. "hi who is this|sounds good lets do it"'
    ),
) -> None:
    """Run a fully mocked call (no API keys). Drives the real pipeline with fake providers."""

    from tests.fakes import FakeLLM, FakeMediaStream, FakeSTT, FakeTTS

    cfg = load_agent_config(config)
    settings = settings_from_env()
    turns = (
        [t.strip() for t in script.split("|") if t.strip()]
        if script
        else ["yeah sure who is this", "okay sounds good lets do it"]
    )
    contact = {
        "name": "Test Contact",
        "phone": "+10000000000",
        "email": "test@example.com",
        "suggested_time": "Tuesday at 2pm",
    }
    media = FakeMediaStream(inbound_frames=[b"\xff" * 160] * 4)
    session = build_session(
        cfg,
        settings,
        contact,
        media,
        # First reply offers the demo, so a later "let's do it" is a real booking (booking is
        # gated on the meeting actually being offered). Mirrors how the live LLM drives the pitch.
        llm=FakeLLM(
            [
                "I'm Alex with Acme, we help teams cut project delays. "
                "Would you be open to a quick 15-minute demo this week?",
                "Great, I'll get that set up.",
            ]
        ),
        # No SPEECH_START in the mock: the contact listens to the greeting before replying, so
        # the demo shows a clean back-and-forth. Barge-in is exercised in the test suite.
        stt=FakeSTT.from_turns(turns, include_speech_start=False),
        tts=FakeTTS(),
        # Mocked run: skip LLM pre-warm so it doesn't consume a scripted FakeLLM response.
        prewarm_llm=False,
    )
    # A short silence timeout keeps the mocked run snappy.
    session.silence_timeout = 0.3
    session.max_silences = 1
    result = asyncio.run(session.run())
    _print_result(result)
    typer.echo("")
    typer.echo(
        "Note: this is a mocked run, so duration and latency are not real. Real timing is "
        "measured with perf_counter on a live call (see SETUP_TODO.md)."
    )
    raise typer.Exit(0)


@app.command("serve")
def serve(
    host: Optional[str] = typer.Option(None, help="Bind host (default from .env or 0.0.0.0)"),
    port: Optional[int] = typer.Option(None, help="Bind port (default from .env or 8000)"),
) -> None:
    """Run the FastAPI Media Streams server (point Twilio at wss://<public-host>/stream)."""

    import time as _time

    import uvicorn

    # Preload heavy provider SDKs BEFORE uvicorn starts accepting calls. On WSL2's /mnt/c (9p)
    # filesystem `import anthropic` alone costs ~14s of blocking I/O; if that lazy import fires on
    # the async event loop during the first call it freezes the whole loop for ~14s, which dead-airs
    # the greeting AND stalls the Deepgram keepalive until its socket idle-times-out (so the agent
    # then hears nothing for the rest of the call). Paying it here, once, at boot avoids all of that.
    typer.echo("Preloading provider SDKs (one-time, slow on /mnt/c)...")
    _t = _time.perf_counter()
    for _mod in ("anthropic", "httpx", "websockets"):
        try:
            __import__(_mod)
        except Exception as _exc:  # a missing optional provider must not stop the server
            typer.echo(f"  (skipped {_mod}: {_exc})")
    typer.echo(f"Preloaded in {_time.perf_counter() - _t:.1f}s. Starting server...")

    settings = settings_from_env()
    uvicorn.run(
        "voiceagent.server:app",
        host=host or settings.host,
        port=port or settings.port,
    )


def _stream_url(public_base_url: str) -> str:
    base = public_base_url.rstrip("/")
    base = base.replace("https://", "wss://").replace("http://", "ws://")
    return f"{base}/stream"


def _require_live_keys(settings) -> None:
    missing = settings.missing_for_live_call()
    if missing:
        typer.echo("Cannot place a live call. These environment variables are not set:")
        for var in missing:
            typer.echo(f"  - {var}")
        typer.echo("Fill them in .env (see .env.example), then try again.")
        raise typer.Exit(1)


def _live_call_runner(settings, stream_url: str):
    """Return a CallRunner that places a Twilio call per contact.

    Correlation note: place_call returns a call SID (CA...) from the Twilio REST
    API. That exact same SID appears in the Twilio start event that the server
    receives over the /stream WebSocket, so both sides are keyed on the same
    identifier. Contact info (name, phone, email, suggested_time) is forwarded
    inside the TwiML as <Parameter> tags; the server reads them from
    start["customParameters"] via _contact_from_params and uses them to
    personalize the session. The CLI records the provisional CallResult (call
    placed, no transcript yet); the server records the full result including
    transcript and cost. Both rows share the same call_sid, enabling reconciliation
    by matching call_sid in the outcomes CSV.
    """

    from .telephony.twilio import TwilioMediaStream, TwilioTelephony, build_stream_twiml  # noqa: F401

    telephony = TwilioTelephony(settings.twilio_account_sid, settings.twilio_auth_token)

    async def runner(contact: dict) -> CallResult:
        sid = await telephony.place_call(
            to=contact["phone"],
            from_=settings.twilio_from_number,
            stream_url=stream_url,
            parameters={
                "name": contact.get("name", ""),
                "phone": contact["phone"],
                "email": contact.get("email", ""),
                "suggested_time": contact.get("suggested_time", ""),
            },
        )
        typer.echo(f"  placed call {sid} -> {contact.get('name','')} {contact['phone']}")
        return CallResult(
            call_sid=sid,
            contact_name=contact.get("name", ""),
            contact_phone=contact["phone"],
            outcome=Outcome.NO_ANSWER,  # provisional; the server records the full outcome
            duration_seconds=0.0,
            end_reason=(
                "call placed; full transcript and cost logged by server under the same "
                f"call_sid {sid}; reconcile by matching call_sid in outcomes CSV"
            ),
        )

    return runner


@app.command("dial")
def dial(
    phone: str = typer.Argument(..., help="E.164 number to dial, e.g. +14155551234"),
    name: str = typer.Option("there", help="Contact name"),
    email: str = typer.Option("", help="Contact email"),
    time: str = typer.Option("later this week", help="Suggested meeting time"),
    config: str = typer.Option("examples/agent.yaml", help="Path to agent.yaml"),
) -> None:
    """Place one live call (requires keys and a running, publicly reachable server)."""

    settings = settings_from_env()
    _require_live_keys(settings)
    load_agent_config(config)  # validate before spending
    stream_url = _stream_url(settings.public_base_url)
    runner = _live_call_runner(settings, stream_url)
    contact = {"name": name, "phone": phone, "email": email, "suggested_time": time}
    asyncio.run(runner(contact))
    typer.echo("")
    typer.echo("The server must be running and reachable at:")
    typer.echo(f"  {stream_url}")
    typer.echo("Start it with `voiceagent serve` and expose it with ngrok if testing locally.")


@app.command("run-list")
def run_list(
    csv_path: str = typer.Argument(..., help="Path to contacts CSV"),
    config: str = typer.Option("examples/agent.yaml", help="Path to agent.yaml"),
    csv_out: str = typer.Option("call_logs/outcomes.csv", help="Where to write outcomes"),
) -> None:
    """Run a contact list as a campaign (requires keys and a running server)."""

    settings = settings_from_env()
    _require_live_keys(settings)
    cfg = load_agent_config(config)
    contacts = asyncio.run(load_contacts(csv_path))
    typer.echo(f"Loaded {len(contacts)} contacts from {csv_path}")
    stream_url = _stream_url(settings.public_base_url)
    runner = _live_call_runner(settings, stream_url)
    sink = build_outcome_sink(csv_path=csv_out)
    campaign = CampaignRunner(cfg, runner, outcome_sink=sink)
    summary = asyncio.run(campaign.run_list(contacts))
    typer.echo("")
    typer.echo(f"Dialed {summary.dialed}, skipped {summary.skipped}, spend ${summary.total_usd:.2f}")
    for outcome, count in sorted(summary.outcomes.items()):
        typer.echo(f"  {outcome}: {count}")


if __name__ == "__main__":
    app()
