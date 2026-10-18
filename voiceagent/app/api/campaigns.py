"""Campaigns (outbound runs), phone numbers (inbound routing), and the "call me now" test."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ... import phones
from ...config import AgentConfig
from .. import repo
from ..campaigns import campaign_spent
from ..crypto import SecretBox
from ..deps import WorkspaceAccess, secret_box, workspace_access
from ..dialer import configure_inbound_webhook, place_call, twilio_factory
from ..leads import dnc_set
from ..models import Call, Campaign, Connector, PhoneNumber, Workspace, utcnow
from ..settings import instance_settings

router = APIRouter(tags=["campaigns"])


class CampaignIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    agent_id: str
    connector_id: Optional[str] = None
    budget_usd: float = Field(25.0, gt=0, le=100000)
    concurrency: int = Field(1, ge=1, le=20)
    max_attempts: int = Field(3, ge=1, le=10)
    retry_delay_minutes: int = Field(240, ge=10, le=10080)


class CampaignPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    budget_usd: Optional[float] = Field(None, gt=0, le=100000)
    concurrency: Optional[int] = Field(None, ge=1, le=20)
    max_attempts: Optional[int] = Field(None, ge=1, le=10)
    retry_delay_minutes: Optional[int] = Field(None, ge=10, le=10080)


async def _campaign(access: WorkspaceAccess, campaign_id: str) -> Campaign:
    c = await access.repo.db.scalar(
        select(Campaign).where(Campaign.workspace_id == access.repo.workspace_id, Campaign.id == campaign_id)
    )
    if c is None:
        raise HTTPException(404, "campaign not found")
    return c


async def _out(access: WorkspaceAccess, c: Campaign) -> dict:
    db = access.repo.db
    counts = dict(
        (await db.execute(
            select(Call.outcome, func.count()).where(Call.campaign_id == c.id).group_by(Call.outcome)
        )).all()
    )
    return {
        "id": c.id,
        "name": c.name,
        "agent_id": c.agent_id,
        "connector_id": c.connector_id,
        "status": c.status,
        "status_reason": c.status_reason,
        "budget_usd": c.budget_usd,
        "spent_usd": round(await campaign_spent(db, c.id), 4),
        "concurrency": c.concurrency,
        "max_attempts": c.max_attempts,
        "retry_delay_minutes": c.retry_delay_minutes,
        "outcomes": {k or "in_progress": v for k, v in counts.items()},
        "calls": sum(counts.values()),
    }


@router.get("/workspaces/{workspace_id}/campaigns")
async def list_campaigns(access: WorkspaceAccess = Depends(workspace_access)) -> list[dict]:
    rows = await access.repo.db.scalars(
        select(Campaign).where(Campaign.workspace_id == access.repo.workspace_id).order_by(Campaign.created_at.desc())
    )
    return [await _out(access, c) for c in rows]


@router.post("/workspaces/{workspace_id}/campaigns", status_code=201)
async def create_campaign(body: CampaignIn, access: WorkspaceAccess = Depends(workspace_access)) -> dict:
    access.require_write()
    agent = await access.repo.agent(body.agent_id)
    if agent is None:
        raise HTTPException(404, "agent not found")
    if agent.mode != "outbound":
        raise HTTPException(422, "campaigns need an outbound agent")
    if body.connector_id:
        conn = await access.repo.db.scalar(
            select(Connector).where(Connector.workspace_id == access.repo.workspace_id, Connector.id == body.connector_id)
        )
        if conn is None:
            raise HTTPException(404, "connector not found")
    c = Campaign(workspace_id=access.repo.workspace_id, **body.model_dump())
    access.repo.db.add(c)
    await access.repo.db.flush()
    await repo.audit(access.repo.db, "campaign.create", user_id=access.auth.user.id,
                     workspace_id=access.repo.workspace_id, target=c.name)
    await access.repo.db.commit()
    return await _out(access, c)


@router.get("/workspaces/{workspace_id}/campaigns/{campaign_id}")
async def get_campaign(campaign_id: str, access: WorkspaceAccess = Depends(workspace_access)) -> dict:
    return await _out(access, await _campaign(access, campaign_id))


@router.patch("/workspaces/{workspace_id}/campaigns/{campaign_id}")
async def update_campaign(campaign_id: str, body: CampaignPatch, access: WorkspaceAccess = Depends(workspace_access)) -> dict:
    access.require_write()
    c = await _campaign(access, campaign_id)
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(c, field, value)
    await access.repo.db.commit()
    return await _out(access, c)


@router.post("/workspaces/{workspace_id}/campaigns/{campaign_id}/start")
async def start_campaign(campaign_id: str, access: WorkspaceAccess = Depends(workspace_access)) -> dict:
    access.require_write()
    c = await _campaign(access, campaign_id)
    c.status, c.status_reason = "running", ""
    await repo.audit(access.repo.db, "campaign.start", user_id=access.auth.user.id,
                     workspace_id=access.repo.workspace_id, target=c.name)
    await access.repo.db.commit()
    return await _out(access, c)


@router.post("/workspaces/{workspace_id}/campaigns/{campaign_id}/pause")
async def pause_campaign(campaign_id: str, access: WorkspaceAccess = Depends(workspace_access)) -> dict:
    # Any member may pause: stopping calls is always safe.
    c = await _campaign(access, campaign_id)
    c.status, c.status_reason = "paused", f"paused by {access.auth.user.email}"
    await repo.audit(access.repo.db, "campaign.pause", user_id=access.auth.user.id,
                     workspace_id=access.repo.workspace_id, target=c.name)
    await access.repo.db.commit()
    return await _out(access, c)


@router.delete("/workspaces/{workspace_id}/campaigns/{campaign_id}", status_code=204)
async def delete_campaign(campaign_id: str, access: WorkspaceAccess = Depends(workspace_access)) -> Response:
    access.require_write()
    c = await _campaign(access, campaign_id)
    if c.status == "running":
        raise HTTPException(409, "pause the campaign before deleting it")
    await access.repo.db.delete(c)
    await access.repo.db.commit()
    return Response(status_code=204)


# -- test call ---------------------------------------------------------------------------------


class TestCallIn(BaseModel):
    phone: str = Field(min_length=3, max_length=40)


@router.post("/workspaces/{workspace_id}/agents/{agent_id}/test-call", status_code=201)
async def test_call(
    agent_id: str,
    body: TestCallIn,
    request: Request,
    access: WorkspaceAccess = Depends(workspace_access),
    box: SecretBox = Depends(secret_box),
) -> dict:
    """Have the agent call YOUR phone now. For trying an agent, not for calling leads: it is
    audited, honours the DNC list, and is not attached to any lead or CRM."""

    access.require_write()
    agent = await access.repo.agent(agent_id)
    if agent is None:
        raise HTTPException(404, "agent not found")
    e164 = phones.normalize(body.phone)
    if e164 is None:
        raise HTTPException(422, "not a valid phone number")
    db, ws = access.repo.db, access.repo.workspace_id
    if e164 in await dnc_set(db, ws):
        raise HTTPException(409, "that number is on the do-not-call list")
    settings = await access.repo.provider_settings(box, instance_settings().public_base_url)
    missing = settings.missing_for_live_call()
    if missing:
        raise HTTPException(422, "missing keys: " + ", ".join(missing))
    cfg = AgentConfig.from_dict(agent.config)
    call = Call(workspace_id=ws, agent_id=agent.id, direction="outbound", status="dialing",
                contact_name=access.auth.user.name or "there", contact_phone=e164)
    db.add(call)
    await repo.audit(db, "agent.test_call", user_id=access.auth.user.id, workspace_id=ws, target=e164)
    await db.commit()
    factory = getattr(request.app.state, "telephony_factory", None) or twilio_factory
    try:
        call.twilio_sid = await place_call(call, None, settings, detect_voicemail=cfg.voicemail.detect, factory=factory)
    except Exception as exc:
        call.status, call.outcome, call.error = "failed", "error", repr(exc)[:1000]
        await db.commit()
        raise HTTPException(502, f"Twilio refused the call: {exc}") from exc
    call.updated_at = utcnow()
    await db.commit()
    return {"call_id": call.id, "twilio_sid": call.twilio_sid}


# -- phone numbers (inbound routing) ----------------------------------------------------------


class NumberIn(BaseModel):
    e164: str = Field(min_length=3, max_length=40)
    inbound_agent_id: Optional[str] = None
    configure_twilio: bool = False


def _number_out(n: PhoneNumber) -> dict:
    return {"id": n.id, "e164": n.e164, "twilio_sid": n.twilio_sid, "inbound_agent_id": n.inbound_agent_id}


@router.get("/workspaces/{workspace_id}/numbers")
async def list_numbers(access: WorkspaceAccess = Depends(workspace_access)) -> list[dict]:
    return [_number_out(n) for n in await access.repo.phone_numbers()]


@router.post("/workspaces/{workspace_id}/numbers", status_code=201)
async def add_number(
    body: NumberIn, request: Request, access: WorkspaceAccess = Depends(workspace_access),
    box: SecretBox = Depends(secret_box),
) -> dict:
    access.require_write()
    e164 = phones.normalize(body.e164)
    if e164 is None:
        raise HTTPException(422, "not a valid phone number")
    db, ws_id = access.repo.db, access.repo.workspace_id
    if await db.scalar(select(PhoneNumber.id).where(PhoneNumber.e164 == e164)):
        raise HTTPException(409, "that number is already registered on this instance")
    if body.inbound_agent_id:
        agent = await access.repo.agent(body.inbound_agent_id)
        if agent is None or agent.mode != "inbound":
            raise HTTPException(422, "inbound_agent_id must be an inbound agent in this workspace")
    number = PhoneNumber(workspace_id=ws_id, e164=e164, inbound_agent_id=body.inbound_agent_id)
    if body.configure_twilio:
        base = instance_settings().public_base_url
        if not base:
            raise HTTPException(422, "PUBLIC_BASE_URL is not set on this instance")
        ws = await db.get(Workspace, ws_id)
        settings = await access.repo.provider_settings(box, base)
        configure = getattr(request.app.state, "configure_number", None) or configure_inbound_webhook
        try:
            number.twilio_sid = await configure(settings, e164, f"{base.rstrip('/')}/voice/inbound/{ws.slug}")
        except Exception as exc:
            raise HTTPException(502, f"could not configure the number on Twilio: {exc}") from exc
    db.add(number)
    await repo.audit(db, "number.add", user_id=access.auth.user.id, workspace_id=ws_id, target=e164)
    await db.commit()
    return _number_out(number)


@router.delete("/workspaces/{workspace_id}/numbers/{number_id}", status_code=204)
async def remove_number(number_id: str, access: WorkspaceAccess = Depends(workspace_access)) -> Response:
    access.require_write()
    n = await access.repo.db.scalar(
        select(PhoneNumber).where(PhoneNumber.workspace_id == access.repo.workspace_id, PhoneNumber.id == number_id)
    )
    if n is None:
        raise HTTPException(404, "number not found")
    await access.repo.db.delete(n)
    await access.repo.db.commit()
    return Response(status_code=204)
