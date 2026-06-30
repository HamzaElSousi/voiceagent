"""agent.yaml -> validated Pydantic models.

Config-as-contract: the YAML is data, validated at load time so a bad script or a missing field
fails loudly before a single call is dialed (and before any telephony money is spent). Secrets
never live here. They come from the environment (.env); see settings_from_env().
"""

from __future__ import annotations

import os
from datetime import time
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator


class AgentConfigError(Exception):
    """Raised when agent.yaml is invalid. Wraps Pydantic errors in a readable message."""


# --------------------------------------------------------------------------------------------
# Sub-models
# --------------------------------------------------------------------------------------------


class AgentPersona(BaseModel):
    name: str
    voice_id: str = Field(..., description="ElevenLabs voice id")
    persona: str

    model_config = {"extra": "forbid"}


class LLMConfig(BaseModel):
    # "claude" uses Anthropic directly (ANTHROPIC_API_KEY). "openrouter" uses any OpenRouter model
    # (OPENROUTER_API_KEY) with the model id as the OpenRouter slug, e.g. "openai/gpt-4o-mini".
    provider: Literal["claude", "openrouter"] = "claude"
    model: str = "claude-haiku-4-5"
    max_tokens: int = Field(150, gt=0, le=2000)
    temperature: float = Field(0.6, ge=0.0, le=1.0)

    model_config = {"extra": "forbid"}


class PipelineConfig(BaseModel):
    mode: Literal["assembled", "realtime"] = "assembled"

    model_config = {"extra": "forbid"}


class Objection(BaseModel):
    trigger: str
    response: str

    model_config = {"extra": "forbid"}


class ScriptConfig(BaseModel):
    greeting: str
    value_prop: str
    objections: list[Objection] = Field(default_factory=list)
    max_objections: int = Field(2, ge=0, le=10)
    booking_signal: str
    booking_response: str
    end_signal: str

    model_config = {"extra": "forbid"}


class Limits(BaseModel):
    per_call_usd: float = Field(0.15, gt=0)
    per_campaign_usd: float = Field(25.00, gt=0)

    model_config = {"extra": "forbid"}


class CallingHours(BaseModel):
    start: time = time(9, 0)
    end: time = time(20, 0)
    timezone: str = "America/New_York"

    model_config = {"extra": "forbid"}

    @field_validator("start", "end", mode="before")
    @classmethod
    def _parse_hhmm(cls, v: object) -> object:
        if isinstance(v, str):
            hh, mm = v.split(":")
            return time(int(hh), int(mm))
        return v

    @field_validator("timezone")
    @classmethod
    def _known_tz(cls, v: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone {v!r}") from exc
        return v


class ComplianceConfig(BaseModel):
    ai_disclosure: str = (
        "Before we start, I want to let you know I'm an AI assistant calling on behalf of the company."
    )
    disclose_in_greeting: bool = True
    recording_consent: str = "This call may be recorded for quality. Is that okay?"
    require_recording_consent: bool = False
    opt_out_phrases: list[str] = Field(
        default_factory=lambda: ["remove me", "do not call", "take me off", "stop calling"]
    )
    do_not_call_file: str = "do_not_call.csv"
    calling_hours: CallingHours = Field(default_factory=CallingHours)

    model_config = {"extra": "forbid"}

    @field_validator("opt_out_phrases")
    @classmethod
    def _lowercase_phrases(cls, v: list[str]) -> list[str]:
        return [p.strip().lower() for p in v if p.strip()]


# --------------------------------------------------------------------------------------------
# Top-level config
# --------------------------------------------------------------------------------------------


VALID_OUTCOMES = {
    "booked_meeting",
    "callback_requested",
    "not_interested",
    "voicemail",
    "no_answer",
}


class AgentConfig(BaseModel):
    """The whole agent.yaml, validated."""

    agent: AgentPersona
    llm: LLMConfig = Field(default_factory=LLMConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    # Deepgram turn-end detection. endpointing_ms is the silence (ms) that marks speech_final,
    # the fast primary signal: lower is snappier, too low causes false turn-ends mid-sentence.
    # utterance_end_ms is the word-gap fallback (Deepgram minimum 1000) for noisy telephony.
    endpointing_ms: int = Field(250, ge=50, le=2000)
    utterance_end_ms: int = Field(1000, ge=1000, le=5000)
    # Let the contact interrupt the agent (full duplex). Requires real transcribed words past an
    # echo-guard window. Set false for robust half-duplex (agent finishes, then listens), useful
    # when the contact is on a speakerphone where echo can falsely trigger interruptions.
    barge_in: bool = True
    script: ScriptConfig
    outcomes: list[str] = Field(default_factory=lambda: sorted(VALID_OUTCOMES))
    limits: Limits = Field(default_factory=Limits)
    compliance: ComplianceConfig = Field(default_factory=ComplianceConfig)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _check_outcomes(self) -> "AgentConfig":
        unknown = set(self.outcomes) - VALID_OUTCOMES
        if unknown:
            raise ValueError(
                f"unknown outcomes {sorted(unknown)}; allowed: {sorted(VALID_OUTCOMES)}"
            )
        return self


# --------------------------------------------------------------------------------------------
# Loaders
# --------------------------------------------------------------------------------------------


def load_agent_config(path: str | Path) -> AgentConfig:
    """Load and validate agent.yaml. Raises AgentConfigError with a readable message on failure."""

    p = Path(path)
    if not p.exists():
        raise AgentConfigError(f"agent config not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise AgentConfigError(f"could not parse YAML in {p}: {exc}") from exc
    if not isinstance(raw, dict):
        raise AgentConfigError(f"{p} must be a YAML mapping at the top level")

    _reject_inline_secrets(raw)

    try:
        return AgentConfig.model_validate(raw)
    except ValidationError as exc:
        raise AgentConfigError(f"invalid agent config {p}:\n{exc}") from exc


# Key-name patterns that must never appear in agent.yaml; secrets belong in .env only.
# Kept specific so ordinary fields like `max_tokens` are not flagged.
_SECRET_KEY_HINTS = ("api_key", "apikey", "auth_token", "secret", "account_sid", "password")


def _reject_inline_secrets(raw: dict, _path: str = "") -> None:
    """Refuse to load a config that embeds secrets. Enforces the .env-only invariant."""

    for key, value in raw.items():
        lowered = str(key).lower()
        if any(hint in lowered for hint in _SECRET_KEY_HINTS):
            where = f"{_path}.{key}" if _path else key
            raise AgentConfigError(
                f"secret-like field {where!r} found in agent.yaml. "
                f"Secrets must live in .env, never in agent.yaml."
            )
        if isinstance(value, dict):
            _reject_inline_secrets(value, f"{_path}.{key}" if _path else str(key))


# --------------------------------------------------------------------------------------------
# Runtime secrets (from environment / .env), kept separate from the YAML contract
# --------------------------------------------------------------------------------------------


class Settings(BaseModel):
    """Runtime secrets and host config, sourced from environment variables. Never serialized
    into logs or agent.yaml. Fields are optional so the whole stack can be built and tested
    with fakes before any keys exist."""

    twilio_account_sid: Optional[str] = None
    twilio_auth_token: Optional[str] = None
    twilio_from_number: Optional[str] = None
    deepgram_api_key: Optional[str] = None
    elevenlabs_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    openrouter_api_key: Optional[str] = None
    public_base_url: Optional[str] = None
    host: str = "0.0.0.0"
    port: int = 8000

    def missing_for_live_call(self) -> list[str]:
        """Names of the env vars still needed before a live call can be placed."""

        required = {
            "TWILIO_ACCOUNT_SID": self.twilio_account_sid,
            "TWILIO_AUTH_TOKEN": self.twilio_auth_token,
            "TWILIO_FROM_NUMBER": self.twilio_from_number,
            "DEEPGRAM_API_KEY": self.deepgram_api_key,
            "ELEVENLABS_API_KEY": self.elevenlabs_api_key,
            "ANTHROPIC_API_KEY": self.anthropic_api_key,
            "PUBLIC_BASE_URL": self.public_base_url,
        }
        return [name for name, value in required.items() if not value]


def settings_from_env() -> Settings:
    """Read secrets from the environment. Loads .env if python-dotenv is present."""

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    return Settings(
        twilio_account_sid=os.getenv("TWILIO_ACCOUNT_SID") or None,
        twilio_auth_token=os.getenv("TWILIO_AUTH_TOKEN") or None,
        twilio_from_number=os.getenv("TWILIO_FROM_NUMBER") or None,
        deepgram_api_key=os.getenv("DEEPGRAM_API_KEY") or None,
        elevenlabs_api_key=os.getenv("ELEVENLABS_API_KEY") or None,
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY") or None,
        public_base_url=os.getenv("PUBLIC_BASE_URL") or None,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
    )
