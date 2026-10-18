"""Call history: outcome, cost, latency, booking and transcript per call."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from .. import repo
from ..deps import WorkspaceAccess, workspace_access
from ..models import Call

router = APIRouter(tags=["calls"])


def _summary(c: Call) -> dict:
    return {
        "id": c.id,
        "agent_id": c.agent_id,
        "twilio_sid": c.twilio_sid,
        "direction": c.direction,
        "status": c.status,
        "contact_name": c.contact_name,
        "contact_phone": c.contact_phone,
        "outcome": c.outcome,
        "end_reason": c.end_reason,
        "duration_seconds": c.duration_seconds,
        "cost_usd": c.cost_usd,
        "booking_ref": c.booking_ref,
        "booked_slot": c.booked_slot,
        "latency_p50_ms": c.latency_p50_ms,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


@router.get("/workspaces/{workspace_id}/calls")
async def list_calls(
    access: WorkspaceAccess = Depends(workspace_access),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    return [_summary(c) for c in await access.repo.calls(limit=limit, offset=offset)]


@router.get("/workspaces/{workspace_id}/calls/{call_id}")
async def get_call(call_id: str, access: WorkspaceAccess = Depends(workspace_access)) -> dict:
    call = await access.repo.call(call_id)
    if call is None:
        raise HTTPException(404, "call not found")
    return {**_summary(call), "transcript": call.transcript, "error": call.error}


@router.delete("/workspaces/{workspace_id}/calls/{call_id}", status_code=204)
async def delete_call(call_id: str, access: WorkspaceAccess = Depends(workspace_access)) -> Response:
    access.require_write()
    if not await access.repo.delete_call(call_id):
        raise HTTPException(404, "call not found")
    await repo.audit(
        access.repo.db, "call.delete", user_id=access.auth.user.id, workspace_id=access.repo.workspace_id, target=call_id
    )
    await access.repo.db.commit()
    return Response(status_code=204)
