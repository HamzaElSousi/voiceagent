"""Milestone 4: campaigns dial eligible leads, Twilio callbacks close the loop, inbound calls are
routed per workspace, and the media stream runs a platform call with the workspace's own agent and
keys. Twilio is a recording fake; the clock is controlled; zero network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from twilio.request_validator import RequestValidator

from tests.test_platform import csrf, first_workspace, run, signup
from voiceagent.app import campaigns as campaigns_mod
from voiceagent.app.calltoken import sign_call, verify_call
from voiceagent.app.models import Call, Campaign, Lead, utcnow
from voiceagent.app.settings import instance_settings
from voiceagent.app.templates import agent_template

BASE = "https://voice.example.com"
TWILIO_TOKEN = "twilio_auth_token_value"
KEYS = {
    "twilio_account_sid": "AC00000000000000000000000000000000",
    "twilio_auth_token": TWILIO_TOKEN,
    "twilio_from_number": "+12125550199",
    "deepgram_api_key": "dg_key_123456",
    "elevenlabs_api_key": "el_key_123456",
    "anthropic_api_key": "sk-ant-key-123456",
}


def daytime_zone(now: datetime) -> str:
    """A fixed-offset zone where it is midday right now, so hours checks pass whatever the clock."""

    k = 12 - now.astimezone(timezone.utc).hour
    return "Etc/GMT" if k == 0 else f"Etc/GMT{-k:+d}"


def night_zone(now: datetime) -> str:
    k = 3 - now.astimezone(timezone.utc).hour  # 03:00 local
    k = k + 24 if k < -12 else k - 24 if k > 14 else k
    return "Etc/GMT" if k == 0 else f"Etc/GMT{-k:+d}"


class RecordingTelephony:
    def __init__(self, fail: bool = False):
        self.calls: list[dict] = []
        self.fail = fail

    def __call__(self, sid, token):
        self.creds = (sid, token)
        return self

    async def place_call(self, to, from_, stream_url, parameters=None, amd_callback_url=None, status_callback_url=None):
        if self.fail:
            raise RuntimeError("Twilio said no")
        self.calls.append(dict(to=to, from_=from_, stream_url=stream_url, parameters=parameters,
                               amd=amd_callback_url, status=status_callback_url))
        return f"CA{len(self.calls):032d}"


@pytest.fixture
def ws(platform, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("COOKIE_SECURE", "false")
    instance_settings.cache_clear()
    signup(platform)
    wsid = first_workspace(platform)
    for name, value in KEYS.items():
        platform.put(f"/api/workspaces/{wsid}/keys/{name}", json={"value": value}, headers=csrf(platform))
    return wsid


def make_agent(platform, wsid, mode="outbound") -> dict:
    cfg = agent_template(mode)
    r = platform.post(f"/api/workspaces/{wsid}/agents", json={"name": f"{mode} agent", "config": cfg}, headers=csrf(platform))
    assert r.status_code == 201, r.text
    return r.json()


def import_leads(platform, wsid, rows: list[tuple[str, str, str, str]]):
    """rows: (name, phone, consent_at, timezone)"""

    csv_text = "name,phone,consent_at,timezone\n" + "".join(f"{n},{p},{c},{t}\n" for n, p, c, t in rows)
    r = platform.post(f"/api/workspaces/{wsid}/leads/import", json={"csv": csv_text}, headers=csrf(platform))
    assert r.status_code == 200, r.text


def make_campaign(platform, wsid, agent_id, **extra) -> dict:
    body = {"name": "Fall outreach", "agent_id": agent_id, **extra}
    r = platform.post(f"/api/workspaces/{wsid}/campaigns", json=body, headers=csrf(platform))
    assert r.status_code == 201, r.text
    c = r.json()
    platform.post(f"/api/workspaces/{wsid}/campaigns/{c['id']}/start", headers=csrf(platform))
    return c


def do_tick(platform, telephony, *, now=None, max_concurrent=3):
    async def _t():
        async with platform.db.sessionmaker() as s:
            return await campaigns_mod.tick(
                s, platform.box, public_base_url=BASE, now=now or utcnow(),
                max_concurrent=max_concurrent, factory=telephony,
            )

    return run(platform, _t)


def db_all(platform, model, *where):
    async def _q():
        async with platform.db.sessionmaker() as s:
            return list(await s.scalars(select(model).where(*where)))

    return run(platform, _q)


def twilio_post(platform, path, form, token=TWILIO_TOKEN):
    sig = RequestValidator(token).compute_signature(BASE + path, form)
    return platform.post(path, data=form, headers={"X-Twilio-Signature": sig})


CONSENT = "2026-10-01"


# --------------------------------------------------------------------------------------------
# Campaign tick
# --------------------------------------------------------------------------------------------


def test_tick_dials_only_consented_leads_and_claims_them(platform, ws):
    zone = daytime_zone(utcnow())
    agent = make_agent(platform, ws)
    import_leads(platform, ws, [("Ada", "(212) 555-0101", CONSENT, zone), ("Bob", "(212) 555-0102", "", zone)])
    make_campaign(platform, ws, agent["id"], concurrency=5)
    tel = RecordingTelephony()
    stats = do_tick(platform, tel)
    assert stats["dialed"] == 1
    placed = tel.calls[0]
    assert placed["to"] == "+12125550101" and placed["from_"] == KEYS["twilio_from_number"]
    assert tel.creds == (KEYS["twilio_account_sid"], TWILIO_TOKEN)  # the workspace's own account
    assert placed["stream_url"] == "wss://voice.example.com/stream"
    params = placed["parameters"]
    assert verify_call(params["call_id"], params["token"])
    assert placed["status"] == f"{BASE}/voice/status?call_id={params['call_id']}"
    assert placed["amd"] == f"{BASE}/voice/amd?call_id={params['call_id']}"
    ada = db_all(platform, Lead, Lead.name == "Ada")[0]
    assert ada.attempts == 1
    call = db_all(platform, Call)[0]
    assert call.status == "dialing" and call.twilio_sid.startswith("CA")


def test_concurrency_limits(platform, ws):
    zone = daytime_zone(utcnow())
    agent = make_agent(platform, ws)
    import_leads(platform, ws, [(f"L{i}", f"(212) 555-01{10 + i}", CONSENT, zone) for i in range(3)])
    make_campaign(platform, ws, agent["id"], concurrency=1)
    tel = RecordingTelephony()
    assert do_tick(platform, tel)["dialed"] == 1
    assert do_tick(platform, tel)["dialed"] == 0  # first call still in flight
    assert len(tel.calls) == 1


def test_instance_wide_concurrency(platform, ws):
    zone = daytime_zone(utcnow())
    agent = make_agent(platform, ws)
    import_leads(platform, ws, [(f"L{i}", f"(212) 555-01{10 + i}", CONSENT, zone) for i in range(3)])
    make_campaign(platform, ws, agent["id"], concurrency=5)
    assert do_tick(platform, RecordingTelephony(), max_concurrent=2)["dialed"] == 2


def test_outside_callee_hours_not_dialed(platform, ws):
    agent = make_agent(platform, ws)
    import_leads(platform, ws, [("Owl", "(212) 555-0101", CONSENT, night_zone(utcnow()))])
    make_campaign(platform, ws, agent["id"])
    assert do_tick(platform, RecordingTelephony())["dialed"] == 0


def test_dnc_lead_not_dialed(platform, ws):
    agent = make_agent(platform, ws)
    import_leads(platform, ws, [("Ada", "(212) 555-0101", CONSENT, daytime_zone(utcnow()))])
    platform.post(f"/api/workspaces/{ws}/dnc", json={"phone": "+12125550101"}, headers=csrf(platform))
    make_campaign(platform, ws, agent["id"])
    assert do_tick(platform, RecordingTelephony())["dialed"] == 0


def test_missing_keys_pauses_campaign(platform, ws):
    agent = make_agent(platform, ws)
    platform.delete(f"/api/workspaces/{ws}/keys/deepgram_api_key", headers=csrf(platform))
    c = make_campaign(platform, ws, agent["id"])
    do_tick(platform, RecordingTelephony())
    got = platform.get(f"/api/workspaces/{ws}/campaigns/{c['id']}").json()
    assert got["status"] == "paused" and "DEEPGRAM_API_KEY" in got["status_reason"]


def test_no_leads_finishes_campaign(platform, ws):
    agent = make_agent(platform, ws)
    c = make_campaign(platform, ws, agent["id"])
    do_tick(platform, RecordingTelephony())
    got = platform.get(f"/api/workspaces/{ws}/campaigns/{c['id']}").json()
    assert got["status"] == "done" and got["status_reason"] == "no more leads to call"


def test_budget_reached_finishes_campaign(platform, ws):
    agent = make_agent(platform, ws)
    import_leads(platform, ws, [("Ada", "(212) 555-0101", CONSENT, daytime_zone(utcnow()))])
    c = make_campaign(platform, ws, agent["id"], budget_usd=0.5)

    async def _spend():
        async with platform.db.sessionmaker() as s:
            s.add(Call(workspace_id=ws, campaign_id=c["id"], status="done", cost_usd=0.6))
            await s.commit()

    run(platform, _spend)
    assert do_tick(platform, RecordingTelephony())["dialed"] == 0
    assert platform.get(f"/api/workspaces/{ws}/campaigns/{c['id']}").json()["status_reason"] == "budget reached"


def test_repeated_dial_errors_pause_campaign(platform, ws):
    zone = daytime_zone(utcnow())
    agent = make_agent(platform, ws)
    import_leads(platform, ws, [(f"L{i}", f"(212) 555-01{10 + i}", CONSENT, zone) for i in range(4)])
    c = make_campaign(platform, ws, agent["id"], concurrency=5)
    stats = do_tick(platform, RecordingTelephony(fail=True))
    assert stats["errors"] == 3
    got = platform.get(f"/api/workspaces/{ws}/campaigns/{c['id']}").json()
    assert got["status"] == "paused" and "dial errors" in got["status_reason"]


def test_inbound_agent_cannot_run_campaign(platform, ws):
    agent = make_agent(platform, ws, mode="inbound")
    r = platform.post(f"/api/workspaces/{ws}/campaigns", json={"name": "x", "agent_id": agent["id"]}, headers=csrf(platform))
    assert r.status_code == 422


def test_stale_calls_are_reaped(platform, ws):
    async def _flow():
        async with platform.db.sessionmaker() as s:
            s.add(Call(workspace_id=ws, status="dialing", updated_at=utcnow() - timedelta(hours=1)))
            await s.commit()
            n = await campaigns_mod.reap_stale(s, utcnow())
            await s.commit()
            return n

    assert run(platform, _flow) == 1
    assert db_all(platform, Call)[0].outcome == "error"


# --------------------------------------------------------------------------------------------
# Twilio callbacks
# --------------------------------------------------------------------------------------------


def _dial_one(platform, ws, **campaign):
    agent = make_agent(platform, ws)
    import_leads(platform, ws, [("Ada", "(212) 555-0101", CONSENT, daytime_zone(utcnow()))])
    c = make_campaign(platform, ws, agent["id"], **campaign)
    tel = RecordingTelephony()
    do_tick(platform, tel)
    return c, tel.calls[0]["parameters"]["call_id"], agent


def test_status_no_answer_records_outcome_and_retries_later(platform, ws):
    c, call_id, _agent = _dial_one(platform, ws, retry_delay_minutes=60, max_attempts=2)
    r = twilio_post(platform, f"/voice/status?call_id={call_id}", {"CallSid": "CA1", "CallStatus": "no-answer"})
    assert r.status_code == 204
    call = db_all(platform, Call, Call.id == call_id)[0]
    assert call.status == "done" and call.outcome == "no_answer"
    assert db_all(platform, Lead)[0].status == "called"
    tel = RecordingTelephony()
    assert do_tick(platform, tel)["dialed"] == 0                                   # retry delay
    assert do_tick(platform, tel, now=utcnow() + timedelta(minutes=61))["dialed"] == 1
    twilio_post(platform, f"/voice/status?call_id={tel.calls[0]['parameters']['call_id']}",
                {"CallSid": "CA2", "CallStatus": "busy"})
    assert do_tick(platform, tel, now=utcnow() + timedelta(minutes=200))["dialed"] == 0  # max attempts
    assert platform.get(f"/api/workspaces/{ws}/campaigns/{c['id']}").json()["status"] == "done"


def test_status_callback_requires_workspace_signature(platform, ws):
    _c, call_id, _a = _dial_one(platform, ws)
    form = {"CallSid": "CA1", "CallStatus": "no-answer"}
    assert twilio_post(platform, f"/voice/status?call_id={call_id}", form, token="wrong").status_code == 403
    assert platform.post(f"/voice/status?call_id={call_id}", data=form).status_code == 403


def test_status_after_stream_does_not_overwrite(platform, ws):
    _c, call_id, _a = _dial_one(platform, ws)

    async def _streaming():
        async with platform.db.sessionmaker() as s:
            call = await s.get(Call, call_id)
            call.status, call.outcome = "done", "booked_meeting"
            await s.commit()

    run(platform, _streaming)
    twilio_post(platform, f"/voice/status?call_id={call_id}", {"CallSid": "CA1", "CallStatus": "completed"})
    assert db_all(platform, Call, Call.id == call_id)[0].outcome == "booked_meeting"


def test_amd_callback_for_platform_call(platform, ws):
    _c, call_id, _a = _dial_one(platform, ws)

    class S:
        machine = None

        def notify_machine(self, answered_by="machine"):
            S.machine = answered_by

    platform.app.state.sessions["CA9"] = S()
    form = {"CallSid": "CA9", "AnsweredBy": "machine_end_beep"}
    assert twilio_post(platform, f"/voice/amd?call_id={call_id}", form, token="wrong").status_code == 403
    assert twilio_post(platform, f"/voice/amd?call_id={call_id}", form).status_code == 204
    assert S.machine == "machine_end_beep"


# --------------------------------------------------------------------------------------------
# Inbound
# --------------------------------------------------------------------------------------------


def test_inbound_routes_to_numbers_agent(platform, ws):
    agent = make_agent(platform, ws, mode="inbound")
    r = platform.post(f"/api/workspaces/{ws}/numbers", json={"e164": "+12125550199", "inbound_agent_id": agent["id"]},
                      headers=csrf(platform))
    assert r.status_code == 201
    form = {"CallSid": "CAin1", "From": "+13105550102", "To": "+12125550199"}
    resp = twilio_post(platform, "/voice/inbound/acme", form)
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("application/xml")
    xml = resp.text
    assert '<Stream url="wss://voice.example.com/stream">' in xml
    call = db_all(platform, Call)[0]
    assert call.direction == "inbound" and call.agent_id == agent["id"] and call.contact_phone == "+13105550102"
    assert f'name="call_id" value="{call.id}"' in xml
    assert f'name="token" value="{sign_call(call.id)}"' in xml


def test_inbound_signature_and_unknown_workspace(platform, ws):
    make_agent(platform, ws, mode="inbound")
    form = {"CallSid": "CAin1", "From": "+13105550102", "To": "+12125550199"}
    assert twilio_post(platform, "/voice/inbound/acme", form, token="wrong").status_code == 403
    assert twilio_post(platform, "/voice/inbound/nope", form).status_code == 404


def test_inbound_without_agent_says_not_set_up(platform, ws):
    form = {"CallSid": "CAin1", "From": "+13105550102", "To": "+12125550199"}
    resp = twilio_post(platform, "/voice/inbound/acme", form)
    assert "<Say>" in resp.text and "<Hangup/>" in resp.text
    assert db_all(platform, Call) == []


# --------------------------------------------------------------------------------------------
# Media stream: a platform call end to end (fake providers, real persistence)
# --------------------------------------------------------------------------------------------


def test_platform_stream_runs_workspace_agent_and_saves_result(platform, ws, monkeypatch):
    from tests.fakes import FakeLLM, FakeMediaStream, FakeSTT, FakeTTS
    from voiceagent import builder, server
    from voiceagent.app.voice import load_platform_call

    _c, call_id, agent = _dial_one(platform, ws)
    seen = {}

    def fake_build_session(config, settings, contact, media):
        seen["settings"], seen["contact"], seen["mode"] = settings, contact, config.mode
        session = builder.build_session(
            config, settings, contact, media,
            llm=FakeLLM(["I'm Alex. Would you be open to a quick 15-minute demo this week?"]),
            stt=FakeSTT.from_turns(["who is this", "sounds good let's do it", "no that's all"], include_speech_start=False),
            tts=FakeTTS(), prewarm_llm=False,
        )
        session.silence_timeout = 0.3
        session.events_dir = None
        return session

    monkeypatch.setattr(server, "build_session", fake_build_session)

    async def _go():
        params = {"call_id": call_id, "token": sign_call(call_id)}
        media = FakeMediaStream(inbound_frames=[b"\xff" * 160] * 4, call_sid="CA00000000000000000000000000000001")
        await server._run_platform_call(platform.app, media, params)

    run(platform, _go)
    assert seen["settings"].deepgram_api_key == KEYS["deepgram_api_key"]  # the workspace's own keys
    assert seen["contact"]["name"] == "Ada"
    call = db_all(platform, Call, Call.id == call_id)[0]
    assert call.status == "done" and call.outcome == "booked_meeting"
    assert any("15-minute demo" in t["text"] for t in call.transcript)
    assert db_all(platform, Lead)[0].status == "booked"

    async def _forged():
        return await load_platform_call({"call_id": call_id, "token": "forged"})

    assert run(platform, _forged) is None


# --------------------------------------------------------------------------------------------
# Test call, numbers, worker jobs
# --------------------------------------------------------------------------------------------


def test_test_call_dials_owner_and_respects_dnc(platform, ws):
    tel = RecordingTelephony()
    platform.app.state.telephony_factory = tel
    agent = make_agent(platform, ws)
    r = platform.post(f"/api/workspaces/{ws}/agents/{agent['id']}/test-call", json={"phone": "(212) 555-0150"}, headers=csrf(platform))
    assert r.status_code == 201, r.text
    assert tel.calls[0]["to"] == "+12125550150"
    platform.post(f"/api/workspaces/{ws}/dnc", json={"phone": "(212) 555-0151"}, headers=csrf(platform))
    r = platform.post(f"/api/workspaces/{ws}/agents/{agent['id']}/test-call", json={"phone": "(212) 555-0151"}, headers=csrf(platform))
    assert r.status_code == 409


def test_number_configures_twilio_webhook(platform, ws):
    seen = {}

    async def configure(settings, e164, url):
        seen.update(token=settings.twilio_auth_token, e164=e164, url=url)
        return "PN123"

    platform.app.state.configure_number = configure
    r = platform.post(f"/api/workspaces/{ws}/numbers", json={"e164": "(212) 555-0199", "configure_twilio": True},
                      headers=csrf(platform))
    assert r.json()["twilio_sid"] == "PN123"
    assert seen == {"token": TWILIO_TOKEN, "e164": "+12125550199", "url": f"{BASE}/voice/inbound/acme"}
    dup = platform.post(f"/api/workspaces/{ws}/numbers", json={"e164": "+12125550199"}, headers=csrf(platform))
    assert dup.status_code == 409


def test_retention_deletes_old_calls(platform, ws):
    from voiceagent.app.worker import enforce_retention

    async def _flow():
        async with platform.db.sessionmaker() as s:
            s.add(Call(workspace_id=ws, status="done", created_at=utcnow() - timedelta(days=200)))
            s.add(Call(workspace_id=ws, status="done"))
            await s.commit()
        return await enforce_retention(platform.db)

    assert run(platform, _flow) == 1
    assert len(db_all(platform, Call)) == 1


def test_worker_syncs_due_connectors(platform, ws, monkeypatch):
    from tests.test_connectors import MCP_CONFIG, FakeMcpServer
    from tests.test_platform_crm import _patch_mcp
    from voiceagent.app.worker import sync_due_connectors

    _patch_mcp(monkeypatch, FakeMcpServer())
    platform.post(f"/api/workspaces/{ws}/connectors",
                  json={"type": "mcp", "name": "CRM", "config": MCP_CONFIG, "secret": "mcpops_secret"},
                  headers=csrf(platform))

    async def _sync():
        return await sync_due_connectors(platform.db, platform.box)

    assert run(platform, _sync) == 1
    assert run(platform, _sync) == 0   # not due again for 15 minutes
    assert len(db_all(platform, Lead)) == 2


def test_campaign_api_pause_and_delete(platform, ws):
    agent = make_agent(platform, ws)
    c = make_campaign(platform, ws, agent["id"])
    assert platform.delete(f"/api/workspaces/{ws}/campaigns/{c['id']}", headers=csrf(platform)).status_code == 409
    paused = platform.post(f"/api/workspaces/{ws}/campaigns/{c['id']}/pause", headers=csrf(platform)).json()
    assert paused["status"] == "paused"
    assert platform.delete(f"/api/workspaces/{ws}/campaigns/{c['id']}", headers=csrf(platform)).status_code == 204
    assert db_all(platform, Campaign) == []
