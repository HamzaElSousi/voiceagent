# Connecting a CRM

VoiceAgent does not ship CRM-specific integrations. Any system can feed it leads and receive call
outcomes in one of three ways:

| Connector | Leads in | Outcomes out | Best for |
|---|---|---|---|
| **MCP** | a tool on your MCP server | a tool on your MCP server | CRMs that already expose an MCP server |
| **Webhook** | `GET` a JSON URL | signed `POST` to a URL | anything with a small HTTP endpoint |
| **CSV** | upload in the dashboard | none (read them in Calls) | one-off lists, spreadsheets |

Whatever the source, the same rules apply before anyone is dialed (see
[Compliance](#consent-and-compliance)).

## Lead fields

Every connector maps your data onto these fields. Only `phone` is required.

| Field | Meaning |
|---|---|
| `external_id` | Your CRM's id for the lead. Used to update rather than duplicate on each sync. Defaults to the phone number. |
| `phone` | Any common format; normalized to E.164. Invalid numbers are skipped. |
| `name`, `email`, `company` | Used by the agent ("Hi Ada") and for the calendar invite. |
| `timezone` | IANA zone (e.g. `America/Toronto`). Optional: inferred from the number when empty. |
| `consent_at` | **When** the person agreed to be called (ISO-8601 date or timestamp). No value means the lead is never dialed by an outbound campaign. |
| `consent_evidence` | **How** they agreed ("web form, 2026-10-01, ip 203.0.113.7"). |

## MCP connector

Point VoiceAgent at your MCP server (Streamable HTTP transport, stateless or stateful), then pick
two tools in the dashboard. **Test connection** lists your server's tools and previews three
mapped leads before anything is saved.

```json
{
  "url": "https://crm.example.com/mcp",
  "auth_header": "X-API-Key",
  "lead_tool": "leads_call_queue",
  "lead_args": {"limit": 50},
  "items_path": "$.leads",
  "field_map": {
    "external_id": "$.id",
    "name": "$.full_name",
    "phone": "$.phone",
    "consent_at": "$.call_consent_at",
    "consent_evidence": "$.call_consent_evidence"
  },
  "outcome_tool": "leads_log_call",
  "outcome_args": {
    "lead_id": "{{lead.external_id}}",
    "outcome": "{{outcome}}",
    "note": "AI call ({{duration_seconds}}s): {{end_reason}}. Booking: {{booking_ref}}"
  }
}
```

- The credential (API key or bearer token) is entered separately and stored encrypted. With
  `auth_header: "Authorization"` it is sent as `Bearer <key>`; any other header name sends the raw
  key.
- `lead_tool` should return the leads that are ready to call: a JSON list, or an object containing
  one (`items_path` points at it). Tool results are read from `structuredContent`, or from the text
  content parsed as JSON.
- `outcome_args` placeholders (`{{...}}`) are filled from the [outcome payload](#outcome-payload).
  A value that is exactly one placeholder keeps its type, so ids stay numbers.

## Webhook connector

```json
{
  "leads_url": "https://crm.example.com/voiceagent/leads",
  "outcome_url": "https://crm.example.com/voiceagent/outcome",
  "items_path": "$",
  "field_map": {"external_id": "$.id"}
}
```

**Leads:** `GET leads_url` with `Authorization: Bearer <secret>`. Return a list, or
`{"leads": [...], "next_cursor": "..."}`. VoiceAgent follows `?cursor=` until there is none.

**Outcomes:** `POST outcome_url` with the JSON [outcome payload](#outcome-payload) and two headers:

```
X-VoiceAgent-Timestamp: 1760000000
X-VoiceAgent-Signature: sha256=<hex HMAC-SHA256(secret, "<timestamp>.<raw body>")>
```

Verify the signature over the raw body and reject timestamps older than 5 minutes. In Python:

```python
import hashlib, hmac, time

def verify(secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    if abs(time.time() - int(timestamp)) > 300:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

Respond with any 2xx status. Anything else (or a timeout) is retried.

## CSV import

Upload a CSV with a header row. Column names are matched loosely (`Phone Number`, `phone`,
`mobile` all work). Recognized columns: `id`, `name`, `phone`, `email`, `company`, `timezone`,
`consent_at` (or `consent_date`), `consent_evidence` (or `consent_source`). Up to 5,000 rows per
import.

## Outcome payload

Sent once per finished call to the lead's connector:

```json
{
  "event": "call.completed",
  "call_id": "3f2c...",
  "direction": "outbound",
  "outcome": "booked_meeting",
  "end_reason": "booking confirmed",
  "duration_seconds": 94.2,
  "booking_ref": "bk_8ZkQ...",
  "booked_slot": "2026-10-21T14:00:00+00:00",
  "summary": "agent: ... / contact: ...",
  "occurred_at": "2026-10-19T15:03:11+00:00",
  "lead": {"id": "...", "external_id": "7", "name": "Ada", "phone": "+12125550101", "email": "ada@example.com"}
}
```

`outcome` is one of `booked_meeting`, `callback_requested`, `not_interested`, `voicemail`,
`no_answer`, `opted_out`, `hung_up`, `budget_exceeded`, `error`.

**Delivery is guaranteed, not instant.** Outcomes go through an outbox. If your endpoint is down,
delivery retries with exponential backoff (30s, 1m, 2m, ... capped at 6h, 12 attempts) before the
event is marked failed in the dashboard. Handle duplicates by `call_id`.

**Opt-outs:** when a contact says "remove me", the number goes on the workspace do-not-call list
immediately, and you receive `outcome: "opted_out"`. Mirror it in your CRM.

## Consent and compliance

These rules are built in and cannot be switched off:

- **Outbound calls need consent.** AI-voice calls count as "artificial voice" under the US TCPA
  (FCC ruling, February 2024), and Canada's CRTC rules restrict automated solicitation calls. A
  lead without `consent_at` is synced and visible, but never dialed. Publicly listed or scraped
  contact details are not consent. Collect it first, for example with a "request a call" link in
  your emails, and record when and how.
- **Do-not-call list** per workspace, checked before every dial, across all campaigns.
- **Calling hours use the callee's local time**, inside 8am to 9pm. When a number could be in
  several zones, it must be inside the window in all of them.
- **AI disclosure** is spoken at the start of every call.

This is not legal advice. You are responsible for the laws that apply to your calls.

## Network safety (self-hosters)

Connector URLs must be `https` and may not resolve to private, loopback or link-local addresses,
so a workspace cannot use the server to reach your internal network. If your CRM runs on the same
machine or LAN, the instance operator can set `ALLOW_PRIVATE_CONNECTOR_URLS=true`.
