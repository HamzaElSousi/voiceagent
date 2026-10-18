"""The /voice/amd callback: Twilio signature validation and routing a machine result to the live
session (or holding it until the media stream connects)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from voiceagent.config import Settings
from voiceagent.server import create_app

BASE = "https://voice.example.com"


class _Session:
    def __init__(self):
        self.machine = None

    def notify_machine(self, answered_by="machine"):
        self.machine = answered_by


def _client(token=None):
    app = create_app()
    app.state.settings = Settings(twilio_auth_token=token, public_base_url=BASE)
    return app, TestClient(app)


def test_machine_result_reaches_live_session():
    app, client = _client()
    session = _Session()
    app.state.sessions["CA1"] = session
    r = client.post("/voice/amd", data={"CallSid": "CA1", "AnsweredBy": "machine_end_beep"})
    assert r.status_code == 204
    assert session.machine == "machine_end_beep"


def test_human_answer_is_ignored():
    app, client = _client()
    session = _Session()
    app.state.sessions["CA1"] = session
    client.post("/voice/amd", data={"CallSid": "CA1", "AnsweredBy": "human"})
    assert session.machine is None


def test_early_result_is_held_for_the_stream():
    app, client = _client()
    client.post("/voice/amd", data={"CallSid": "CA9", "AnsweredBy": "machine_end_silence"})
    assert app.state.amd_early["CA9"] == "machine_end_silence"


def test_signature_is_enforced_when_token_set():
    app, client = _client(token="secret_token")
    form = {"CallSid": "CA1", "AnsweredBy": "machine_end_beep"}
    assert client.post("/voice/amd", data=form).status_code == 403
    sig = RequestValidator("secret_token").compute_signature(BASE + "/voice/amd", form)
    r = client.post("/voice/amd", data=form, headers={"X-Twilio-Signature": sig})
    assert r.status_code == 204
