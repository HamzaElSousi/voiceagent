"""Server + demo UI tests via FastAPI TestClient. Also the realtime pipeline stub + selection."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import EXAMPLE_YAML
from voiceagent.builder import build_pipeline
from voiceagent.config import load_agent_config
from voiceagent.cost import DollarCostMeter
from voiceagent.pipeline.realtime import RealtimePipeline
from voiceagent.server import create_app


@pytest.fixture
def client():
    return TestClient(create_app(str(EXAMPLE_YAML)))


def test_health(monkeypatch):
    # Build a clean-env app so the assertion does not depend on the developer's real .env.
    # settings_from_env() reloads .env via load_dotenv, so neutralize it and clear the keys.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    for var in (
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_FROM_NUMBER",
        "DEEPGRAM_API_KEY",
        "ELEVENLABS_API_KEY",
        "ANTHROPIC_API_KEY",
        "PUBLIC_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    clean_client = TestClient(create_app(str(EXAMPLE_YAML)))
    r = clean_client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert "ANTHROPIC_API_KEY" in r.json()["missing_for_live_call"]


def test_ui_loads(client):
    r = client.get("/ui")
    assert r.status_code == 200
    assert "VoiceAgent" in r.text


def test_ui_validate_good_config(client):
    text = EXAMPLE_YAML.read_text(encoding="utf-8")
    r = client.post("/ui/validate", json={"yaml_text": text})
    body = r.json()
    assert body["valid"] is True
    assert body["pipeline_mode"] == "assembled"
    assert body["llm_model"] == "claude-haiku-4-5"


def test_ui_validate_bad_config(client):
    r = client.post("/ui/validate", json={"yaml_text": "agent: {name: x}"})
    assert r.json()["valid"] is False


def test_ui_demo_runs_mocked_call(client):
    text = EXAMPLE_YAML.read_text(encoding="utf-8")
    r = client.post("/ui/demo", json={"yaml_text": text, "turns": ["yeah who is this", "okay sounds good lets do it"]})
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "booked_meeting"
    assert body["cost_usd"] > 0
    assert len(body["transcript"]) > 0


def test_realtime_pipeline_selected_by_config():
    cfg = load_agent_config(EXAMPLE_YAML)
    cfg.pipeline.mode = "realtime"
    from voiceagent.config import Settings

    pipe = build_pipeline(cfg, Settings(), DollarCostMeter())
    assert isinstance(pipe, RealtimePipeline)


async def test_realtime_pipeline_start_raises_clearly():
    pipe = RealtimePipeline()
    with pytest.raises(NotImplementedError) as exc:
        await pipe.start(media=None)  # type: ignore[arg-type]
    assert "realtime" in str(exc.value).lower()
