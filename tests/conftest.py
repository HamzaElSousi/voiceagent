"""Shared fixtures for the VoiceAgent test suite. Zero network, scripted fakes only."""

from __future__ import annotations

import pathlib

import pytest

from tests.fakes import FakeLLM, FakeMediaStream, FakeSTT, FakeTTS
from voiceagent.compliance import ComplianceGate
from voiceagent.config import load_agent_config
from voiceagent.contracts import CostRates
from voiceagent.cost import DollarCostMeter
from voiceagent.engine import ConversationEngine
from voiceagent.intent import IntentClassifier
from voiceagent.pipeline.assembled import AssembledPipeline
from voiceagent.session import CallSession

EXAMPLE_YAML = pathlib.Path(__file__).resolve().parents[1] / "examples" / "agent.yaml"

DEFAULT_CONTACT = {
    "name": "John Smith",
    "email": "john@example.com",
    "suggested_time": "Tuesday at 2pm",
    "phone": "+14155551234",
}


@pytest.fixture
def cfg():
    return load_agent_config(EXAMPLE_YAML)


@pytest.fixture
def dnc_path(tmp_path):
    return tmp_path / "do_not_call.csv"


@pytest.fixture
def contact():
    return dict(DEFAULT_CONTACT)


@pytest.fixture
def make_engine(cfg, dnc_path, contact):
    """Factory: build a ConversationEngine wired to a FakeLLM and a fresh cost meter."""

    def _make(llm_responses=None, classifier_llm=None, per_call_cap=0.15):
        meter = DollarCostMeter(per_call_cap_usd=per_call_cap, rates=CostRates())
        compliance = ComplianceGate(cfg.compliance, dnc_path=dnc_path)
        classifier = IntentClassifier(cfg.script, cfg.compliance, llm=classifier_llm)
        engine = ConversationEngine(
            cfg, FakeLLM(llm_responses or []), classifier, compliance, contact, cost_meter=meter
        )
        return engine, meter, compliance

    return _make


@pytest.fixture
def make_session(cfg, dnc_path, contact):
    """Factory: build a full CallSession over AssembledPipeline + fakes."""

    def _make(
        turns,
        llm_responses=None,
        llm=None,
        include_speech_start=False,
        frames_per_call=3,
        per_call_cap=0.15,
        silence_timeout=0.3,
        max_silences=1,
    ):
        meter = DollarCostMeter(per_call_cap_usd=per_call_cap, rates=CostRates())
        compliance = ComplianceGate(cfg.compliance, dnc_path=dnc_path)
        classifier = IntentClassifier(cfg.script, cfg.compliance, llm=None)
        engine = ConversationEngine(
            cfg, llm or FakeLLM(llm_responses or []), classifier, compliance, contact, cost_meter=meter
        )
        stt = FakeSTT.from_turns(turns, include_speech_start=include_speech_start)
        tts = FakeTTS(frames_per_call=frames_per_call)
        media = FakeMediaStream(inbound_frames=[b"\xff" * 160] * 4)
        pipeline = AssembledPipeline(stt, tts, cost_meter=meter)
        session = CallSession(
            engine,
            pipeline,
            media,
            meter,
            contact=contact,
            silence_timeout=silence_timeout,
            max_silences=max_silences,
            max_turns=20,
            events_dir=None,  # no per-call timeline files during tests
            prewarm_llm=False,  # a warmup call would consume a scripted FakeLLM response
        )
        return session, engine, tts, media, meter

    return _make


async def drain(reply):
    """Collect an AgentReply's text stream into a string."""

    return "".join([d async for d in reply.text_stream])
