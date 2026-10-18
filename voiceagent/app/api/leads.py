"""Leads (synced or imported) and the workspace do-not-call list."""

from __future__ import annotations

from datetime import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from ... import phones
from ...connectors.base import ConnectorError
from ...connectors.csvimport import parse_csv
from .. import eligibility, repo
from ..deps import WorkspaceAccess, workspace_access
from ..leads import add_dnc, dnc_set, upsert_leads
from ..models import Connector, DncEntry, Lead, utcnow

router = APIRouter(tags=["leads"])


class CsvImportIn(BaseModel):
    csv: str = Field(min_length=1, max_length=5_000_000)
    connector_id: Optional[str] = None


class ConsentIn(BaseModel):
    # Recording consent by hand needs evidence: who agreed, when, and how.
    evidence: str = Field(min_length=10, max_length=2000)


class DncIn(BaseModel):
    phone: str = Field(min_length=3, max_length=40)


class DncImportIn(BaseModel):
    phones: list[str] = Field(max_length=10000)


def _lead_out(lead: Lead, elig: eligibility.Eligibility) -> dict:
    return {
        "id": lead.id,
        "connector_id": lead.connector_id,
        "external_id": lead.external_id,
        "name": lead.name,
        "phone": lead.phone,
        "email": lead.email,
        "company": lead.company,
        "timezone": lead.timezone,
        "has_consent": lead.consent_at is not None,
        "consent_at": lead.consent_at.isoformat() if lead.consent_at else None,
        "consent_evidence": lead.consent_evidence,
        "status": lead.status,
        "attempts": lead.attempts,
        "last_called_at": lead.last_called_at.isoformat() if lead.last_called_at else None,
        # Eligible right now under the TCPA bounds (the campaign also applies its agent's hours).
        "dialable_now": elig.ok,
        "blocked_reason": elig.reason or None,
    }


@router.get("/workspaces/{workspace_id}/leads")
async def list_leads(
    access: WorkspaceAccess = Depends(workspace_access),
    status: Optional[str] = None,
    q: Optional[str] = Query(None, max_length=200),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    db, ws = access.repo.db, access.repo.workspace_id
    stmt = select(Lead).where(Lead.workspace_id == ws)
    if status:
        stmt = stmt.where(Lead.status == status)
    if q:
        like = f"%{q.lower().replace('%', '').replace('_', '')}%"
        stmt = stmt.where(func.lower(Lead.name).like(like) | func.lower(Lead.email).like(like) | Lead.phone.like(like))
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = await db.scalars(stmt.order_by(Lead.created_at.desc()).limit(limit).offset(offset))
    dnc = await dnc_set(db, ws)
    now = utcnow()
    items = []
    for lead in rows:
        elig = eligibility.check(
            phone=lead.phone, consent_at=lead.consent_at, dnc=dnc, now=now,
            hours_start=time(0, 0), hours_end=time(23, 59), lead_timezone=lead.timezone,
            attempts=lead.attempts,
        )
        items.append(_lead_out(lead, elig))
    return {"total": int(total or 0), "items": items}


@router.post("/workspaces/{workspace_id}/leads/import")
async def import_csv(body: CsvImportIn, access: WorkspaceAccess = Depends(workspace_access)) -> dict:
    access.require_write()
    db, ws = access.repo.db, access.repo.workspace_id
    if body.connector_id:
        conn = await db.scalar(select(Connector).where(Connector.workspace_id == ws, Connector.id == body.connector_id))
        if conn is None or conn.type != "csv":
            raise HTTPException(404, "CSV connector not found")
    try:
        records, skipped = parse_csv(body.csv)
    except ConnectorError as exc:
        raise HTTPException(422, str(exc)) from exc
    report = await upsert_leads(db, ws, body.connector_id, records)
    await repo.audit(db, "leads.import", user_id=access.auth.user.id, workspace_id=ws, target=f"{len(records)} rows")
    await db.commit()
    return {**report.as_dict(), "skipped_without_phone": skipped}


@router.post("/workspaces/{workspace_id}/leads/{lead_id}/consent")
async def record_consent(lead_id: str, body: ConsentIn, access: WorkspaceAccess = Depends(workspace_access)) -> dict:
    access.require_write()
    db, ws = access.repo.db, access.repo.workspace_id
    lead = await db.scalar(select(Lead).where(Lead.workspace_id == ws, Lead.id == lead_id))
    if lead is None:
        raise HTTPException(404, "lead not found")
    lead.consent_at = utcnow()
    lead.consent_evidence = f"{body.evidence.strip()} (recorded by {access.auth.user.email})"
    await repo.audit(db, "lead.consent", user_id=access.auth.user.id, workspace_id=ws, target=lead.id)
    await db.commit()
    return {"id": lead.id, "consent_at": lead.consent_at.isoformat(), "consent_evidence": lead.consent_evidence}


@router.delete("/workspaces/{workspace_id}/leads/{lead_id}", status_code=204)
async def delete_lead(lead_id: str, access: WorkspaceAccess = Depends(workspace_access)) -> Response:
    access.require_write()
    db, ws = access.repo.db, access.repo.workspace_id
    result = await db.execute(delete(Lead).where(Lead.workspace_id == ws, Lead.id == lead_id))
    if not result.rowcount:
        raise HTTPException(404, "lead not found")
    await db.commit()
    return Response(status_code=204)


# -- do-not-call -----------------------------------------------------------------------------


@router.get("/workspaces/{workspace_id}/dnc")
async def list_dnc(access: WorkspaceAccess = Depends(workspace_access)) -> list[dict]:
    rows = await access.repo.db.scalars(
        select(DncEntry).where(DncEntry.workspace_id == access.repo.workspace_id).order_by(DncEntry.created_at.desc())
    )
    return [
        {"phone": d.phone, "reason": d.reason, "source": d.source, "created_at": d.created_at.isoformat()}
        for d in rows
    ]


@router.post("/workspaces/{workspace_id}/dnc", status_code=201)
async def add_to_dnc(body: DncIn, access: WorkspaceAccess = Depends(workspace_access)) -> dict:
    # Any member may add a number: suppressing is always safe.
    db, ws = access.repo.db, access.repo.workspace_id


    e164 = phones.normalize(body.phone)
    if e164 is None:
        raise HTTPException(422, "not a valid phone number")
    added = await add_dnc(db, ws, e164, reason="manual", source=access.auth.user.email)
    await repo.audit(db, "dnc.add", user_id=access.auth.user.id, workspace_id=ws, target=e164)
    await db.commit()
    return {"phone": e164, "added": added}


@router.post("/workspaces/{workspace_id}/dnc/import")
async def import_dnc(body: DncImportIn, access: WorkspaceAccess = Depends(workspace_access)) -> dict:
    db, ws = access.repo.db, access.repo.workspace_id
    added = invalid = 0
    for raw in body.phones:


        if phones.normalize(raw) is None:
            invalid += 1
        elif await add_dnc(db, ws, raw, reason="import", source=access.auth.user.email):
            added += 1
    await repo.audit(db, "dnc.import", user_id=access.auth.user.id, workspace_id=ws, target=f"{added} added")
    await db.commit()
    return {"added": added, "invalid": invalid}


@router.delete("/workspaces/{workspace_id}/dnc/{phone}", status_code=204)
async def remove_from_dnc(phone: str, access: WorkspaceAccess = Depends(workspace_access)) -> Response:
    # Removing a number re-enables calls to someone who opted out: owners/admins only, audited.
    access.require_write()
    db, ws = access.repo.db, access.repo.workspace_id


    e164 = phones.normalize(phone) or phone
    result = await db.execute(delete(DncEntry).where(DncEntry.workspace_id == ws, DncEntry.phone == e164))
    if not result.rowcount:
        raise HTTPException(404, "number not on the list")
    await repo.audit(db, "dnc.remove", user_id=access.auth.user.id, workspace_id=ws, target=e164)
    await db.commit()
    return Response(status_code=204)
