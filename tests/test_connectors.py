"""Connectors (generic MCP, webhook, CSV), field mapping, URL safety, phone normalization and the
outbound eligibility rules. The MCP server is an in-test JSON-RPC fake over httpx.MockTransport."""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone

import httpx
import pytest

from voiceagent import phones
from voiceagent.app import eligibility
from voiceagent.connectors.base import ConnectorError
from voiceagent.connectors.csvimport import parse_csv
from voiceagent.connectors.mapping import extract_items, get_path, map_lead, render_args
from voiceagent.connectors.mcp import McpConnector
from voiceagent.connectors.urlsafety import check_url
from voiceagent.connectors.webhook import SIGNATURE_HEADER, TIMESTAMP_HEADER, WebhookConnector, verify

NY_LEAD = "+12125550101"
LA_LEAD = "+13105550102"


# --------------------------------------------------------------------------------------------
# A fake MCP server
# --------------------------------------------------------------------------------------------


class FakeMcpServer:
    """Speaks just enough MCP (JSON-RPC over Streamable HTTP) for the connector."""

    def __init__(self, *, sse: bool = False, stateful: bool = True, token: str = "mcpops_secret"):
        self.sse = sse
        self.stateful = stateful
        self.token = token
        self.calls: list[tuple[str, dict]] = []
        self.headers: list[dict] = []
        self.leads = [
            {"id": 7, "full_name": "Ada", "phone": "(212) 555-0101", "call_consent_at": "2026-10-01T15:00:00Z"},
            {"id": 8, "full_name": "Bob", "phone": "310 555 0102", "call_consent_at": None},
            {"id": 9, "full_name": "No Phone", "phone": ""},
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(dict(request.headers))
        if request.headers.get("x-api-key") != self.token and request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401)
        msg = json.loads(request.content)
        if "id" not in msg:
            return httpx.Response(202)
        method, params = msg["method"], msg.get("params") or {}
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "fake-crm"}, "capabilities": {}}
        elif method == "tools/list":
            result = {"tools": [{"name": "leads_call_queue", "description": "leads"}, {"name": "leads_log_call"}]}
        elif method == "tools/call":
            self.calls.append((params["name"], params.get("arguments") or {}))
            if params["name"] == "leads_call_queue":
                result = {"content": [{"type": "text", "text": json.dumps({"leads": self.leads})}]}
            elif params["name"] == "leads_log_call":
                result = {"content": [{"type": "text", "text": "logged"}]}
            else:
                result = {"content": [{"type": "text", "text": "unknown tool"}], "isError": True}
        else:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "nope"}})
        body = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        headers = {"Mcp-Session-Id": "sess-1"} if self.stateful and method == "initialize" else {}
        if self.sse:
            text = f"event: message\ndata: {json.dumps(body)}\n\n"
            return httpx.Response(200, text=text, headers={**headers, "content-type": "text/event-stream"})
        return httpx.Response(200, json=body, headers=headers)


MCP_CONFIG = {
    "url": "https://crm.example.com/mcp",
    "auth_header": "X-API-Key",
    "lead_tool": "leads_call_queue",
    "items_path": "$.leads",
    "field_map": {"external_id": "$.id", "name": "$.full_name", "consent_at": "$.call_consent_at"},
    "outcome_tool": "leads_log_call",
    "outcome_args": {"lead_id": "{{lead.external_id}}", "outcome": "{{outcome}}", "note": "AI call: {{end_reason}}"},
}


def _mcp(server: FakeMcpServer, config=None) -> McpConnector:
    client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    return McpConnector(config or MCP_CONFIG, "mcpops_secret", client=client, allow_private=True)


async def test_mcp_fetch_leads_maps_fields():
    server = FakeMcpServer()
    leads = await _mcp(server).fetch_leads()
    assert [(lead.external_id, lead.name, lead.phone) for lead in leads] == [
        ("7", "Ada", "(212) 555-0101"),
        ("8", "Bob", "310 555 0102"),
    ]  # "No Phone" dropped
    assert leads[0].consent_at == "2026-10-01T15:00:00Z"
    assert leads[1].consent_at is None


async def test_mcp_session_and_protocol_headers():
    server = FakeMcpServer(stateful=True)
    await _mcp(server).fetch_leads()
    later = server.headers[-1]
    assert later["mcp-session-id"] == "sess-1"
    assert later["mcp-protocol-version"] == "2025-06-18"
    assert later["x-api-key"] == "mcpops_secret"


async def test_mcp_sse_responses():
    leads = await _mcp(FakeMcpServer(sse=True)).fetch_leads()
    assert len(leads) == 2


async def test_mcp_push_outcome_renders_args():
    server = FakeMcpServer()
    payload = {"outcome": "booked_meeting", "end_reason": "booking confirmed", "lead": {"external_id": "7"}}
    await _mcp(server).push_outcome(payload)
    assert server.calls[-1] == (
        "leads_log_call",
        {"lead_id": "7", "outcome": "booked_meeting", "note": "AI call: booking confirmed"},
    )


async def test_mcp_describe_lists_tools():
    info = await _mcp(FakeMcpServer()).describe()
    assert info["server"]["name"] == "fake-crm"
    assert [t["name"] for t in info["tools"]] == ["leads_call_queue", "leads_log_call"]


async def test_mcp_bad_credential_is_clear_error():
    server = FakeMcpServer(token="other")
    with pytest.raises(ConnectorError, match="rejected the credential"):
        await _mcp(server).fetch_leads()


async def test_mcp_tool_error_raises():
    cfg = dict(MCP_CONFIG, lead_tool="missing_tool")
    with pytest.raises(ConnectorError, match="failed"):
        await _mcp(FakeMcpServer(), cfg).fetch_leads()


async def test_mcp_error_never_leaks_credential():
    server = FakeMcpServer(token="other")
    with pytest.raises(ConnectorError) as exc:
        await _mcp(server).fetch_leads()
    assert "mcpops_secret" not in str(exc.value)


# --------------------------------------------------------------------------------------------
# Webhook
# --------------------------------------------------------------------------------------------


async def test_webhook_fetch_pages_and_push_is_signed():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.headers["authorization"] == "Bearer whsec_1"
            if request.url.params.get("cursor") == "p2":
                return httpx.Response(200, json={"leads": [{"id": "b", "phone": LA_LEAD}]})
            return httpx.Response(200, json={"leads": [{"id": "a", "phone": NY_LEAD}], "next_cursor": "p2"})
        seen["body"] = request.content
        seen["ts"] = request.headers[TIMESTAMP_HEADER]
        seen["sig"] = request.headers[SIGNATURE_HEADER]
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    hook = WebhookConnector(
        {"leads_url": "https://crm.example.com/leads", "outcome_url": "https://crm.example.com/outcome",
         "field_map": {"external_id": "$.id"}},
        "whsec_1",
        client=client,
        allow_private=True,
    )
    leads = await hook.fetch_leads()
    assert [lead.external_id for lead in leads] == ["a", "b"]
    await hook.push_outcome({"outcome": "booked_meeting"})
    assert verify("whsec_1", seen["ts"], seen["body"], seen["sig"])
    assert not verify("wrong", seen["ts"], seen["body"], seen["sig"])


async def test_webhook_failure_raises_for_retry():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    hook = WebhookConnector({"outcome_url": "https://crm.example.com/o"}, "s", client=client, allow_private=True)
    with pytest.raises(ConnectorError, match="503"):
        await hook.push_outcome({"outcome": "x"})


def test_webhook_signature_expires():
    body = b"{}"
    old = str(int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()))
    from voiceagent.connectors.webhook import sign

    assert not verify("s", old, body, sign("s", old, body))


# --------------------------------------------------------------------------------------------
# URL safety (SSRF)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["https://127.0.0.1/mcp", "https://10.0.0.5/x", "https://169.254.169.254/latest/meta-data", "https://[::1]/x"],
)
async def test_private_targets_refused(url):
    with pytest.raises(ConnectorError, match="private"):
        await check_url(url, allow_private=False)


@pytest.mark.parametrize("url", ["http://crm.example.com/x", "ftp://crm.example.com", "not a url"])
async def test_bad_schemes_refused(url):
    with pytest.raises(ConnectorError):
        await check_url(url, allow_private=False)


async def test_private_allowed_by_operator():
    await check_url("http://127.0.0.1:8000/mcp", allow_private=True)


# --------------------------------------------------------------------------------------------
# Mapping + CSV
# --------------------------------------------------------------------------------------------


def test_get_path():
    data = {"a": {"b": [{"c": 5}]}}
    assert get_path(data, "$.a.b[0].c") == 5
    assert get_path(data, "$.a.x") is None
    assert get_path(data, "$") is data
    with pytest.raises(ConnectorError):
        get_path(data, "a.b")


def test_extract_items_wrappers():
    assert extract_items([{"x": 1}]) == [{"x": 1}]
    assert extract_items({"data": [{"x": 1}]}) == [{"x": 1}]
    with pytest.raises(ConnectorError):
        extract_items({"leads": "nope"}, "$.leads")


def test_map_lead_defaults_external_id_to_phone():
    lead = map_lead({"phone": NY_LEAD}, {})
    assert lead.external_id == NY_LEAD


def test_render_args_keeps_types():
    ctx = {"lead": {"external_id": 42}, "outcome": "booked_meeting"}
    assert render_args({"id": "{{lead.external_id}}", "n": "x {{outcome}}"}, ctx) == {"id": 42, "n": "x booked_meeting"}


def test_parse_csv_with_consent_columns():
    text = "Full Name,Phone Number,Email,Consent Date,Consent Source\nAda,(212) 555-0101,a@x.com,2026-10-01,web form\nNo Phone,,b@x.com,,\n"
    leads, skipped = parse_csv(text)
    assert skipped == 1
    assert leads[0].name == "Ada" and leads[0].consent_at == "2026-10-01" and leads[0].consent_evidence == "web form"


def test_parse_csv_needs_phone_column():
    with pytest.raises(ConnectorError, match="phone"):
        parse_csv("name,email\nA,a@x.com\n")


# --------------------------------------------------------------------------------------------
# Phones + eligibility
# --------------------------------------------------------------------------------------------


def test_normalize_and_zones():
    assert phones.normalize("(212) 555-0101") == NY_LEAD
    assert phones.normalize("+1 555 555 0100") is None
    assert phones.normalize("hello") is None
    assert phones.timezones_for(LA_LEAD) == ["America/Los_Angeles"]


# 2026-10-19 13:00 UTC = 9:00 New York = 6:00 Los Angeles.
NOW = datetime(2026, 10, 19, 13, 0, tzinfo=timezone.utc)
CONSENT = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _check(phone=NY_LEAD, consent=CONSENT, dnc=frozenset(), now=NOW, start=time(9), end=time(20), **kw):
    return eligibility.check(phone=phone, consent_at=consent, dnc=set(dnc), now=now, hours_start=start, hours_end=end, **kw)


def test_eligible_with_consent_in_hours():
    assert _check().ok


def test_no_consent_never_dialed():
    r = _check(consent=None)
    assert not r.ok and r.reason == "no_consent"


def test_dnc_blocks():
    assert _check(dnc={NY_LEAD}).reason == "dnc"


def test_invalid_phone_blocks():
    assert _check(phone="+15555550100").reason == "invalid_phone"


def test_callee_local_time_not_caller_time():
    # 9:00 in New York is fine, but it is 6:00 in Los Angeles: too early to call there.
    r = _check(phone=LA_LEAD)
    assert not r.ok and r.reason == "outside_hours" and "Los_Angeles" in r.detail


def test_tcpa_bounds_override_wider_agent_hours():
    # Agent allows 6:00-23:00, but 7:30 local is before the 8:00 TCPA floor.
    early = datetime(2026, 10, 19, 11, 30, tzinfo=timezone.utc)  # 7:30 New York
    assert _check(now=early, start=time(6), end=time(23)).reason == "outside_hours"


def test_lead_timezone_override():
    assert _check(phone=LA_LEAD, lead_timezone="America/New_York").ok


def test_multi_zone_number_must_fit_every_zone():
    r = _check(phone="+18005551234")
    assert not r.ok and r.reason == "outside_hours"


def test_max_attempts():
    assert _check(attempts=3, max_attempts=3).reason == "max_attempts"
