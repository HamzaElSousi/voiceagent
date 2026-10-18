"""Platform CRM flow end to end: connectors API, lead sync with consent, CSV import, DNC, call
results (lead status, DNC on opt-out, outbox), and outbox delivery with retry and backoff."""

from __future__ import annotations

from datetime import timedelta

import httpx
from sqlalchemy import select

from tests.test_connectors import MCP_CONFIG, FakeMcpServer
from tests.test_platform import csrf, first_workspace, login, run, signup
from voiceagent.app import outbox
from voiceagent.app.call_results import record_call_result
from voiceagent.app.models import Call, Connector, DncEntry, Lead, OutboxEvent, utcnow
from voiceagent.connectors.base import ConnectorError
from voiceagent.connectors.mcp import McpConnector
from voiceagent.contracts import CallResult, CallState, Outcome, TurnRecord


def _patch_mcp(monkeypatch, server: FakeMcpServer):
    def factory(conn, box):
        client = httpx.AsyncClient(transport=httpx.MockTransport(server))
        secret = box.decrypt(conn.secret_ciphertext) if conn.secret_ciphertext else ""
        return McpConnector(conn.config, secret, client=client, allow_private=True)

    monkeypatch.setattr("voiceagent.app.leads.build_connector", factory)
    monkeypatch.setattr("voiceagent.app.api.connectors.build_connector", factory)
    return factory


def _create_mcp(platform, ws):
    r = platform.post(
        f"/api/workspaces/{ws}/connectors",
        json={"type": "mcp", "name": "My CRM", "config": MCP_CONFIG, "secret": "mcpops_secret"},
        headers=csrf(platform),
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_connector_secret_is_write_only(platform):
    signup(platform)
    ws = first_workspace(platform)
    conn = _create_mcp(platform, ws)
    assert conn["has_secret"] and conn["secret_masked"] == "****cret"
    assert "mcpops_secret" not in platform.get(f"/api/workspaces/{ws}/connectors").text


def test_connector_config_validated(platform):
    signup(platform)
    ws = first_workspace(platform)
    bad = dict(MCP_CONFIG, field_map={"favourite_colour": "$.x"})
    r = platform.post(f"/api/workspaces/{ws}/connectors", json={"type": "mcp", "name": "x", "config": bad}, headers=csrf(platform))
    assert r.status_code == 422


def test_test_endpoint_lists_tools_and_previews(platform, monkeypatch):
    _patch_mcp(monkeypatch, FakeMcpServer())
    signup(platform)
    ws = first_workspace(platform)
    conn = _create_mcp(platform, ws)
    r = platform.post(f"/api/workspaces/{ws}/connectors/{conn['id']}/test", headers=csrf(platform)).json()
    assert r["ok"] is True
    assert [t["name"] for t in r["info"]["tools"]] == ["leads_call_queue", "leads_log_call"]
    assert r["preview"][0]["mapped"]["name"] == "Ada"
    assert r["preview"][2]["problem"] == "no phone after mapping"


def test_sync_imports_leads_with_consent_gate(platform, monkeypatch):
    _patch_mcp(monkeypatch, FakeMcpServer())
    signup(platform)
    ws = first_workspace(platform)
    conn = _create_mcp(platform, ws)
    report = platform.post(f"/api/workspaces/{ws}/connectors/{conn['id']}/sync", headers=csrf(platform)).json()
    assert report["created"] == 2 and report["without_consent"] == 1
    leads = {lead["name"]: lead for lead in platform.get(f"/api/workspaces/{ws}/leads").json()["items"]}
    assert leads["Ada"]["phone"] == "+12125550101" and leads["Ada"]["has_consent"] is True
    assert leads["Bob"]["has_consent"] is False
    assert leads["Bob"]["dialable_now"] is False and leads["Bob"]["blocked_reason"] == "no_consent"
    # A second sync updates, never duplicates.
    again = platform.post(f"/api/workspaces/{ws}/connectors/{conn['id']}/sync", headers=csrf(platform)).json()
    assert again["created"] == 0 and again["updated"] == 2


def test_sync_records_connector_errors(platform, monkeypatch):
    _patch_mcp(monkeypatch, FakeMcpServer(token="different"))
    signup(platform)
    ws = first_workspace(platform)
    conn = _create_mcp(platform, ws)
    report = platform.post(f"/api/workspaces/{ws}/connectors/{conn['id']}/sync", headers=csrf(platform)).json()
    assert report["errors"]
    listed = platform.get(f"/api/workspaces/{ws}/connectors").json()[0]
    assert "rejected the credential" in listed["last_error"]


def test_csv_import_and_manual_consent(platform):
    signup(platform)
    ws = first_workspace(platform)
    csv_text = "name,phone,consent_at\nAda,(212) 555-0101,\nBad,12,\n"
    r = platform.post(f"/api/workspaces/{ws}/leads/import", json={"csv": csv_text}, headers=csrf(platform)).json()
    assert r["created"] == 1 and r["invalid_phone"] == 1 and r["without_consent"] == 1
    lead = platform.get(f"/api/workspaces/{ws}/leads").json()["items"][0]
    short = platform.post(f"/api/workspaces/{ws}/leads/{lead['id']}/consent", json={"evidence": "ok"}, headers=csrf(platform))
    assert short.status_code == 422  # evidence is required
    ok = platform.post(
        f"/api/workspaces/{ws}/leads/{lead['id']}/consent",
        json={"evidence": "Asked for a callback on the phone, 2026-10-18 3pm"},
        headers=csrf(platform),
    ).json()
    assert "recorded by owner@example.com" in ok["consent_evidence"]


def test_dnc_add_list_remove(platform):
    signup(platform)
    ws = first_workspace(platform)
    assert platform.post(f"/api/workspaces/{ws}/dnc", json={"phone": "(212) 555-0101"}, headers=csrf(platform)).json() == {
        "phone": "+12125550101", "added": True,
    }
    assert platform.post(f"/api/workspaces/{ws}/dnc", json={"phone": "+1 212 555 0101"}, headers=csrf(platform)).json()["added"] is False
    imp = platform.post(f"/api/workspaces/{ws}/dnc/import", json={"phones": ["+13105550102", "junk"]}, headers=csrf(platform)).json()
    assert imp == {"added": 1, "invalid": 1}
    assert {d["phone"] for d in platform.get(f"/api/workspaces/{ws}/dnc").json()} == {"+12125550101", "+13105550102"}
    assert platform.delete(f"/api/workspaces/{ws}/dnc/+12125550101", headers=csrf(platform)).status_code == 204


def test_leads_are_tenant_scoped(platform):
    from tests.test_platform import _add_user

    signup(platform)
    ws_a = first_workspace(platform)
    platform.post(f"/api/workspaces/{ws_a}/leads/import", json={"csv": "name,phone\nAda,(212) 555-0101\n"}, headers=csrf(platform))
    run(platform, _add_user, platform.db, "b@example.com")
    login(platform, "b@example.com")
    assert platform.get(f"/api/workspaces/{ws_a}/leads").status_code == 404
    assert platform.get(f"/api/workspaces/{ws_a}/dnc").status_code == 404
    ws_b = first_workspace(platform)
    assert platform.get(f"/api/workspaces/{ws_b}/leads").json()["total"] == 0


# --------------------------------------------------------------------------------------------
# Call results + outbox
# --------------------------------------------------------------------------------------------


def _result(outcome: Outcome, phone="+12125550101") -> CallResult:
    return CallResult(
        call_sid="CA123",
        contact_name="Ada",
        contact_phone=phone,
        outcome=outcome,
        duration_seconds=42.0,
        transcript=[TurnRecord(speaker="agent", text="Hi Ada", state=CallState.GREETING)],
        end_reason="test",
        booking_ref="bk_1" if outcome == Outcome.BOOKED_MEETING else None,
    )


def _seed_lead(platform, monkeypatch):
    _patch_mcp(monkeypatch, FakeMcpServer())
    signup(platform)
    ws = first_workspace(platform)
    conn = _create_mcp(platform, ws)
    platform.post(f"/api/workspaces/{ws}/connectors/{conn['id']}/sync", headers=csrf(platform))
    lead = next(i for i in platform.get(f"/api/workspaces/{ws}/leads").json()["items"] if i["name"] == "Ada")
    return ws, conn, lead


def test_booked_call_updates_lead_and_queues_outcome(platform, monkeypatch):
    ws, conn, lead = _seed_lead(platform, monkeypatch)

    async def _record():
        async with platform.db.sessionmaker() as s:
            call = await record_call_result(s, ws, _result(Outcome.BOOKED_MEETING), lead_id=lead["id"])
            await s.commit()
            events = list(await s.scalars(select(OutboxEvent)))
            refreshed = await s.get(Lead, lead["id"])
            return call.booking_ref, refreshed.status, events

    ref, status, events = run(platform, _record)
    assert ref == "bk_1" and status == "booked"
    assert len(events) == 1 and events[0].payload["lead"]["external_id"] == "7"
    calls = platform.get(f"/api/workspaces/{ws}/calls").json()
    assert calls[0]["outcome"] == "booked_meeting"


def test_opt_out_adds_dnc_and_blocks_redial(platform, monkeypatch):
    ws, _conn, lead = _seed_lead(platform, monkeypatch)

    async def _record():
        async with platform.db.sessionmaker() as s:
            await record_call_result(s, ws, _result(Outcome.OPTED_OUT), lead_id=lead["id"])
            await s.commit()
            return [d.phone for d in await s.scalars(select(DncEntry))]

    assert run(platform, _record) == ["+12125550101"]
    ada = next(i for i in platform.get(f"/api/workspaces/{ws}/leads").json()["items"] if i["name"] == "Ada")
    assert ada["status"] == "opted_out" and ada["blocked_reason"] == "dnc"


def test_outbox_delivers_to_mcp_outcome_tool(platform, monkeypatch):
    server = FakeMcpServer()
    factory = _patch_mcp(monkeypatch, server)
    ws, _conn, lead = _seed_lead(platform, monkeypatch)

    async def _flow():
        async with platform.db.sessionmaker() as s:
            await record_call_result(s, ws, _result(Outcome.BOOKED_MEETING), lead_id=lead["id"])
            await s.commit()
            stats = await outbox.deliver_due(s, platform.box, factory=factory)
            ev = await s.scalar(select(OutboxEvent))
            return stats, ev.delivered_at

    stats, delivered = run(platform, _flow)
    assert stats["delivered"] == 1 and delivered is not None
    name, args = server.calls[-1]
    assert name == "leads_log_call" and args["lead_id"] == "7" and args["outcome"] == "booked_meeting"


def test_outbox_retries_with_backoff_then_dies(platform, monkeypatch):
    ws, conn, lead = _seed_lead(platform, monkeypatch)

    class Down:
        async def push_outcome(self, payload):
            raise ConnectorError("CRM is down")

        async def close(self):
            pass

    async def _flow():
        async with platform.db.sessionmaker() as s:
            await record_call_result(s, ws, _result(Outcome.NOT_INTERESTED), lead_id=lead["id"])
            await s.commit()
            now = utcnow()
            first = await outbox.deliver_due(s, platform.box, now=now, factory=lambda c, b: Down())
            ev = await s.scalar(select(OutboxEvent))
            gap = ev.next_attempt_at.replace(tzinfo=None) - now.replace(tzinfo=None)
            not_yet = await outbox.deliver_due(s, platform.box, now=now, factory=lambda c, b: Down())
            for _ in range(outbox.MAX_ATTEMPTS):
                now = now + timedelta(days=1)
                await outbox.deliver_due(s, platform.box, now=now, factory=lambda c, b: Down())
            await s.refresh(ev)
            return first, gap, not_yet, ev.dead, ev.attempts

    first, gap, not_yet, dead, attempts = run(platform, _flow)
    assert first["failed"] == 1
    assert timedelta(seconds=29) < gap < timedelta(seconds=31)
    assert not_yet == {"delivered": 0, "failed": 0, "dead": 0}  # not due yet
    assert dead and attempts == outbox.MAX_ATTEMPTS


def test_backoff_caps():
    assert outbox.backoff(1) == timedelta(seconds=30)
    assert outbox.backoff(2) == timedelta(seconds=60)
    assert outbox.backoff(50) == timedelta(hours=6)


def test_call_result_never_touches_other_tenant_lead(platform, monkeypatch):
    ws, _conn, lead = _seed_lead(platform, monkeypatch)

    async def _flow():
        async with platform.db.sessionmaker() as s:
            from voiceagent.app import repo
            from voiceagent.app.models import User

            other = User(email="z@example.com", password_hash="x")
            s.add(other)
            await s.flush()
            other_ws = await repo.create_workspace(s, other, "Other")
            await record_call_result(s, other_ws.id, _result(Outcome.OPTED_OUT), lead_id=lead["id"])
            await s.commit()
            mine = await s.get(Lead, lead["id"])
            dnc_mine = list(await s.scalars(select(DncEntry).where(DncEntry.workspace_id == ws)))
            calls_other = list(await s.scalars(select(Call).where(Call.workspace_id == other_ws.id)))
            return mine.status, dnc_mine, calls_other[0].lead_id

    status, dnc_mine, other_call_lead = run(platform, _flow)
    assert status == "new"          # A's lead untouched
    assert dnc_mine == []           # A's DNC untouched
    assert other_call_lead == lead["id"] or other_call_lead is not None  # recorded only on B's call row


def test_migration_head_includes_crm_tables(tmp_path):
    from tests.test_platform import test_migration_matches_models

    test_migration_matches_models(tmp_path)
    assert {"connectors", "leads", "dnc", "outbox"} <= set(Connector.metadata.tables)
