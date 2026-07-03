"""Wiring factory: build a CallSession (and its pipeline/engine) from config + settings.

This is the single place that knows how to assemble the provider clients behind their interfaces
and select the pipeline mode. The server (live) and the tests (fakes) both go through here, so the
construction logic lives in one spot. Provider clients are imported lazily so this module imports
without any API keys; only building a live session touches the provider SDKs.
"""

from __future__ import annotations

from typing import Optional

from .compliance import ComplianceGate
from .config import AgentConfig, Settings
from .contracts import CostMeter, LLM, MediaStream, Pipeline, STT, TTS
from .cost import DollarCostMeter
from .engine import ConversationEngine
from .intent import IntentClassifier
from .pipeline.assembled import AssembledPipeline
from .pipeline.realtime import RealtimePipeline
from .session import CallSession


class MissingKeyError(RuntimeError):
    """Raised when a live provider is requested but its key is not set."""


def build_llm(config: AgentConfig, settings: Settings) -> LLM:
    if config.llm.provider == "openrouter":
        if not settings.openrouter_api_key:
            raise MissingKeyError("OPENROUTER_API_KEY is not set")
        from .llm.openrouter import OpenRouterLLM

        return OpenRouterLLM(
            api_key=settings.openrouter_api_key,
            model=config.llm.model,
            max_tokens=config.llm.max_tokens,
            temperature=config.llm.temperature,
        )

    if not settings.anthropic_api_key:
        raise MissingKeyError("ANTHROPIC_API_KEY is not set")
    from .llm.claude import ClaudeLLM

    return ClaudeLLM(
        api_key=settings.anthropic_api_key,
        model=config.llm.model,
        max_tokens=config.llm.max_tokens,
        temperature=config.llm.temperature,
    )


def build_stt(config: AgentConfig, settings: Settings) -> STT:
    if not settings.deepgram_api_key:
        raise MissingKeyError("DEEPGRAM_API_KEY is not set")
    from .stt.deepgram import DeepgramSTT

    return DeepgramSTT(
        api_key=settings.deepgram_api_key,
        endpointing_ms=config.endpointing_ms,
        utterance_end_ms=config.utterance_end_ms,
    )


def build_tts(config: AgentConfig, settings: Settings) -> TTS:
    if not settings.elevenlabs_api_key:
        raise MissingKeyError("ELEVENLABS_API_KEY is not set")
    from .tts.elevenlabs import ElevenLabsTTS

    return ElevenLabsTTS(api_key=settings.elevenlabs_api_key, voice_id=config.agent.voice_id)


def build_pipeline(
    config: AgentConfig,
    settings: Settings,
    cost_meter: CostMeter,
    *,
    stt: Optional[STT] = None,
    tts: Optional[TTS] = None,
) -> Pipeline:
    """Select and construct the pipeline per config.pipeline.mode. Pass stt/tts to inject fakes."""

    if config.pipeline.mode == "realtime":
        return RealtimePipeline(api_key=settings.anthropic_api_key or "", cost_meter=cost_meter)
    return AssembledPipeline(
        stt or build_stt(config, settings),
        tts or build_tts(config, settings),
        cost_meter=cost_meter,
    )


def build_engine(
    config: AgentConfig,
    contact: dict,
    cost_meter: CostMeter,
    *,
    llm: Optional[LLM] = None,
    compliance: Optional[ComplianceGate] = None,
    classifier_llm: Optional[LLM] = None,
    settings: Optional[Settings] = None,
) -> ConversationEngine:
    real_llm = llm or build_llm(config, settings or Settings())
    gate = compliance or ComplianceGate(config.compliance)
    classifier = IntentClassifier(config.script, config.compliance, llm=classifier_llm)
    return ConversationEngine(config, real_llm, classifier, gate, contact, cost_meter=cost_meter)


def build_session(
    config: AgentConfig,
    settings: Settings,
    contact: dict,
    media: MediaStream,
    *,
    llm: Optional[LLM] = None,
    stt: Optional[STT] = None,
    tts: Optional[TTS] = None,
    compliance: Optional[ComplianceGate] = None,
    prewarm_llm: bool = True,
) -> CallSession:
    """Assemble a ready-to-run CallSession. Inject llm/stt/tts to drive it with fakes in tests;
    omit them to build real provider clients from settings (live). Pass prewarm_llm=False for
    mocked runs so the warmup call does not consume a scripted FakeLLM response."""

    meter = DollarCostMeter(per_call_cap_usd=config.limits.per_call_usd)
    engine = build_engine(
        config, contact, meter, llm=llm, compliance=compliance, settings=settings
    )
    pipeline = build_pipeline(config, settings, meter, stt=stt, tts=tts)
    return CallSession(
        engine,
        pipeline,
        media,
        meter,
        contact=contact,
        silence_timeout=8.0,
        max_silences=2,
        barge_in_enabled=config.barge_in,
        prewarm_llm=prewarm_llm,
    )
