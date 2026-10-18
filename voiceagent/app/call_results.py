"""Persist a finished call: the Call row, the lead's status, DNC on opt-out, and the outbox event
for the CRM, all in one transaction. Called by the server when a platform call ends (off the call
path: the audio is already done)."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..contracts import CallResult, Outcome
from . import outbox
from .leads import add_dnc
from .models import Call, Connector, Lead, utcnow

_LEAD_STATUS = {
    Outcome.BOOKED_MEETING: "booked",
    Outcome.OPTED_OUT: "opted_out",
    Outcome.NOT_INTERESTED: "not_interested",
    Outcome.ERROR: "failed",
}


def _p50(result: CallResult) -> Optional[float]:
    values = sorted(s.endpoint_to_first_audio_ms for s in result.latencies if s.endpoint_to_first_audio_ms)
    return round(values[len(values) // 2], 1) if values else None


async def record_call_result(
    db: AsyncSession,
    workspace_id: str,
    result: CallResult,
    *,
    call_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    lead_id: Optional[str] = None,
    direction: str = "outbound",
) -> Call:
    call = None
    if call_id:
        call = await db.scalar(select(Call).where(Call.workspace_id == workspace_id, Call.id == call_id))
    if call is None and result.call_sid:
        call = await db.scalar(
            select(Call).where(Call.workspace_id == workspace_id, Call.twilio_sid == result.call_sid)
        )
    if call is None:
        call = Call(workspace_id=workspace_id, direction=direction)
        db.add(call)
    call.agent_id = agent_id or call.agent_id
    call.lead_id = lead_id or call.lead_id
    call.twilio_sid = result.call_sid or call.twilio_sid
    call.status = "failed" if result.outcome == Outcome.ERROR else "done"
    call.contact_name = result.contact_name[:200]
    call.contact_phone = result.contact_phone[:20]
    call.outcome = result.outcome.value
    call.end_reason = result.end_reason[:300]
    call.duration_seconds = result.duration_seconds
    call.cost_usd = round(result.cost.total_usd, 4)
    call.booking_ref = result.booking_ref
    call.booked_slot = result.booked_slot
    call.transcript = [{"speaker": t.speaker, "text": t.text} for t in result.transcript]
    call.latency_p50_ms = _p50(result)
    call.error = result.error
    call.updated_at = utcnow()
    await db.flush()

    lead = await db.get(Lead, call.lead_id) if call.lead_id else None
    if lead is not None and lead.workspace_id != workspace_id:
        lead = None  # never touch another tenant's lead
    if lead is not None:
        lead.status = _LEAD_STATUS.get(result.outcome, "called")
        lead.last_called_at = utcnow()

    # Opt-out is honoured instantly and everywhere in the workspace, whatever the source.
    phone = (lead.phone if lead else "") or result.contact_phone
    if result.outcome == Outcome.OPTED_OUT and phone:
        await add_dnc(db, workspace_id, phone, reason="opt_out", source=f"call {call.id}")

    if lead is not None and lead.connector_id:
        conn = await db.get(Connector, lead.connector_id)
        if conn is not None and conn.enabled and conn.type != "csv":
            await outbox.enqueue(db, workspace_id, conn.id, outbox.outcome_payload(call, lead))
    await db.flush()
    return call
