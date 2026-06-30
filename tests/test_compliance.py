"""Compliance tests: disclosure, opt-out + DNC persistence, calling hours, recording consent."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from voiceagent.compliance import ComplianceGate, normalize_phone


def test_disclosure_prepended(cfg, dnc_path):
    gate = ComplianceGate(cfg.compliance, dnc_path=dnc_path)
    out = gate.apply_disclosure("Hi, is this John?")
    assert out.startswith(cfg.compliance.ai_disclosure)
    assert "Hi, is this John?" in out


def test_disclosure_off(cfg, dnc_path):
    cfg.compliance.disclose_in_greeting = False
    gate = ComplianceGate(cfg.compliance, dnc_path=dnc_path)
    assert gate.apply_disclosure("Hello") == "Hello"


def test_opt_out_phrases(cfg, dnc_path):
    gate = ComplianceGate(cfg.compliance, dnc_path=dnc_path)
    assert gate.is_opt_out("please REMOVE ME now")
    assert gate.is_opt_out("do not call again")
    assert not gate.is_opt_out("tell me more")


def test_suppress_persists_and_is_idempotent(cfg, dnc_path):
    gate = ComplianceGate(cfg.compliance, dnc_path=dnc_path)
    assert not gate.is_suppressed("+1 (415) 555-1234")
    gate.suppress("+14155551234", reason="opt_out")
    gate.suppress("+14155551234", reason="opt_out")  # idempotent
    assert gate.is_suppressed("+1 (415) 555-1234")
    # A fresh gate reading the same file still sees the suppression.
    gate2 = ComplianceGate(cfg.compliance, dnc_path=dnc_path)
    assert gate2.is_suppressed("14155551234")


def test_normalize_phone():
    assert normalize_phone("+1 (415) 555-1234") == "+14155551234"
    assert normalize_phone("415-555-1234") == "4155551234"


def test_calling_hours_window(cfg, dnc_path):
    gate = ComplianceGate(cfg.compliance, dnc_path=dnc_path)
    tz = ZoneInfo(cfg.compliance.calling_hours.timezone)
    inside = datetime(2026, 6, 28, 12, 0, tzinfo=tz)   # noon, allowed
    outside = datetime(2026, 6, 28, 6, 0, tzinfo=tz)   # 6am, before 9am
    assert gate.within_calling_hours(inside)
    assert not gate.within_calling_hours(outside)


def test_can_dial_gate(cfg, dnc_path):
    gate = ComplianceGate(cfg.compliance, dnc_path=dnc_path)
    tz = ZoneInfo(cfg.compliance.calling_hours.timezone)
    noon = datetime(2026, 6, 28, 12, 0, tzinfo=tz)
    ok, reason = gate.can_dial("+14155551234", now=noon)
    assert ok and reason == ""
    gate.suppress("+14155551234")
    ok2, reason2 = gate.can_dial("+14155551234", now=noon)
    assert not ok2 and "do-not-call" in reason2


def test_recording_consent_gate(cfg, dnc_path):
    gate = ComplianceGate(cfg.compliance, dnc_path=dnc_path)
    assert gate.recording_consent_line() is None  # off by default
    cfg.compliance.require_recording_consent = True
    gate2 = ComplianceGate(cfg.compliance, dnc_path=dnc_path)
    assert gate2.recording_consent_line() == cfg.compliance.recording_consent
