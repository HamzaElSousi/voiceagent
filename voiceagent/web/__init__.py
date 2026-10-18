"""Demo Web UI for VoiceAgent (the third config surface, for demos).

Mounted on the main FastAPI app by voiceagent.server. No new server, no auth: a localhost demo
tool to edit and validate agent.yaml, pick the pipeline mode, and watch a mocked call's
transcript, outcome, and per-hop latency without any API keys. The live demo run uses the same
/stream endpoint as production once keys are in place.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from ..config import AgentConfig, settings_from_env

_HERE = Path(__file__).resolve().parent
_INDEX = _HERE / "templates" / "index.html"


class ConfigPayload(BaseModel):
    yaml_text: str


class DemoPayload(BaseModel):
    yaml_text: str
    turns: list[str] = []


def _validate_yaml(text: str) -> tuple[bool, str, AgentConfig | None]:
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        return False, f"YAML parse error: {exc}", None
    try:
        cfg = AgentConfig.model_validate(raw)
    except Exception as exc:
        return False, str(exc), None
    return True, "Config is valid.", cfg


def mount_web_ui(app: FastAPI) -> None:
    """Attach the demo UI routes to an existing FastAPI app."""

    @app.get("/ui", response_class=HTMLResponse)
    async def ui_index() -> str:
        if _INDEX.exists():
            return _INDEX.read_text(encoding="utf-8")
        return "<h1>VoiceAgent</h1><p>Demo UI template missing.</p>"

    @app.get("/ui/config")
    async def ui_get_config() -> JSONResponse:
        path = getattr(app.state, "config_path", "examples/agent.yaml")
        p = Path(path)
        text = p.read_text(encoding="utf-8") if p.exists() else ""
        return JSONResponse({"yaml_text": text, "path": str(path)})

    @app.post("/ui/validate")
    async def ui_validate(payload: ConfigPayload) -> JSONResponse:
        ok, message, cfg = _validate_yaml(payload.yaml_text)
        body = {"valid": ok, "message": message}
        if ok and cfg is not None:
            body["pipeline_mode"] = cfg.pipeline.mode
            body["llm_model"] = cfg.llm.model
            body["objections"] = len(cfg.script.objections)
        return JSONResponse(body)

    @app.post("/ui/demo")
    async def ui_demo(payload: DemoPayload) -> JSONResponse:
        """Run a fully mocked call with the submitted config and return the transcript, outcome,
        cost, and per-hop latency. Zero API keys."""

        ok, message, cfg = _validate_yaml(payload.yaml_text)
        if not ok or cfg is None:
            return JSONResponse({"error": message}, status_code=400)

        result = await _run_mock_demo(cfg, payload.turns)
        return JSONResponse(result)


async def _run_mock_demo(cfg: AgentConfig, turns: list[str]) -> dict:
    # Imported here so the web package has no hard dependency on the test fakes at import time.
    from tests.fakes import FakeLLM, FakeMediaStream, FakeSTT, FakeTTS

    from ..builder import build_session

    if not turns:
        turns = ["yeah sure who is this", "okay sounds good lets do it"]
    contact = {
        "name": "Demo Contact",
        "phone": "+10000000000",
        "email": "demo@example.com",
        "suggested_time": "Tuesday at 2pm",
    }
    media = FakeMediaStream(inbound_frames=[b"\xff" * 160] * 4)
    session = build_session(
        cfg,
        settings_from_env(),
        contact,
        media,
        llm=FakeLLM(["Sure, happy to explain a bit more."]),
        # No SPEECH_START in the mock: the contact listens to the greeting before replying, so
        # the demo shows a clean back-and-forth. Barge-in is exercised in the test suite.
        stt=FakeSTT.from_turns(turns, include_speech_start=False),
        tts=FakeTTS(),
    )
    session.silence_timeout = 0.3
    session.max_silences = 1
    result = await session.run()
    latencies = [s.endpoint_to_first_audio_ms for s in result.latencies if s.endpoint_to_first_audio_ms]
    latencies.sort()
    p50 = latencies[len(latencies) // 2] if latencies else 0.0
    return {
        "outcome": result.outcome.value,
        "end_reason": result.end_reason,
        "duration_seconds": result.duration_seconds,
        "p50_latency_ms": round(p50, 1),
        "cost_usd": result.cost.total_usd,
        "transcript": [
            {"speaker": t.speaker, "text": t.text} for t in result.transcript
        ],
    }
