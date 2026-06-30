"""CLI tests via Typer's CliRunner. test-voice runs the full mocked pipeline, zero keys."""

from __future__ import annotations

from typer.testing import CliRunner

from voiceagent.cli import app

runner = CliRunner()


def test_test_voice_books_meeting():
    result = runner.invoke(app, ["test-voice"])
    assert result.exit_code == 0, result.output
    assert "booked_meeting" in result.output
    assert "Cost:" in result.output


def test_test_voice_custom_script():
    result = runner.invoke(app, ["test-voice", "--script", "who is this|goodbye"])
    assert result.exit_code == 0, result.output
    assert "Outcome:" in result.output


def test_dial_without_keys_fails_clearly(monkeypatch):
    # Keep the test hermetic: stop settings_from_env() from reloading a populated .env on the
    # developer's machine, which would undo the delenv below.
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
    result = runner.invoke(app, ["dial", "+14155551234", "--name", "Test"])
    assert result.exit_code == 1
    assert "not set" in result.output
    assert "ANTHROPIC_API_KEY" in result.output
