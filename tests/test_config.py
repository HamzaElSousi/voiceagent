"""Config-as-contract tests: agent.yaml validates, and bad configs fail loudly at load."""

from __future__ import annotations

import pytest

from voiceagent.config import (
    AgentConfigError,
    Settings,
    load_agent_config,
)


def test_example_config_loads(cfg):
    assert cfg.agent.name == "Alex"
    assert cfg.llm.model == "claude-haiku-4-5"
    assert cfg.pipeline.mode == "assembled"
    assert cfg.endpointing_ms == 250
    assert cfg.utterance_end_ms == 1000
    assert cfg.limits.per_call_usd == 0.30
    assert cfg.limits.per_campaign_usd == 25.0
    assert len(cfg.script.objections) == 3
    assert "remove me" in cfg.compliance.opt_out_phrases
    assert cfg.compliance.calling_hours.timezone == "America/New_York"


def test_missing_file_raises():
    with pytest.raises(AgentConfigError):
        load_agent_config("/nonexistent/agent.yaml")


def test_inline_secret_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "agent:\n  name: X\n  voice_id: v\n  persona: p\n  api_key: leaked\n"
        "script:\n  greeting: g\n  value_prop: v\n  booking_signal: y\n"
        "  booking_response: r\n  end_signal: e\n"
    )
    with pytest.raises(AgentConfigError) as exc:
        load_agent_config(bad)
    assert "secret" in str(exc.value).lower()


def test_unknown_outcome_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "agent:\n  name: X\n  voice_id: v\n  persona: p\n"
        "script:\n  greeting: g\n  value_prop: v\n  booking_signal: y\n"
        "  booking_response: r\n  end_signal: e\n"
        "outcomes: [booked_meeting, teleport]\n"
    )
    with pytest.raises(AgentConfigError):
        load_agent_config(bad)


def test_extra_field_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "agent:\n  name: X\n  voice_id: v\n  persona: p\n"
        "script:\n  greeting: g\n  value_prop: v\n  booking_signal: y\n"
        "  booking_response: r\n  end_signal: e\n"
        "nonsense_top_level: 1\n"
    )
    with pytest.raises(AgentConfigError):
        load_agent_config(bad)


def test_max_tokens_not_flagged_as_secret(cfg):
    # Regression: 'max_tokens' contains 'token' but must not trip secret rejection.
    assert cfg.llm.max_tokens == 64


def test_settings_missing_for_live_call_lists_all_when_empty(monkeypatch):
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
    s = Settings()
    missing = s.missing_for_live_call()
    assert "ANTHROPIC_API_KEY" in missing
    assert "TWILIO_ACCOUNT_SID" in missing
    assert len(missing) == 7
