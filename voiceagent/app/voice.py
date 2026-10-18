"""Twilio-facing routes for platform calls, and the per-call context the media stream loads.

  POST /voice/inbound/{workspace_slug}   "A call comes in" webhook -> TwiML that opens the stream
  POST /voice/status?call_id=...         final call status (busy / no-answer / failed / completed)
  POST /voice/amd?call_id=...            answering-machine result (async AMD)

Every request is checked against X-Twilio-Signature using the auth token of the workspace that owns
the call or number. The media stream itself is authorized by the signed call token (calltoken.py).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qsl
from xml.sax.saxutils import escape

from fastapi import APIRouter, Request, Response
from sqlalchemy import select
from twilio.request_validator import RequestValidator

from .. import phones
from ..config import AgentConfig, Settings
from ..contracts import CallResult, Outcome
from .call_results import record_call_result
from .calltoken import sign_call, verify_call
from .crypto import SecretBox
from .db import get_database
from .dialer import stream_url
from .models import Agent, Call, Lead, PhoneNumber, Workspace, utcnow
from .repo import WorkspaceRepo
from .settings import instance_settings

logger = logging.getLogger("voiceagent.voice")

router = APIRouter()

MACHINE_ANSWERS = {"machine_end_beep", "machine_end_silence", "machine_end_other", "fax"}
_NO_CONNECT = {"busy": Outcome.NO_ANSWER, "no-answer": Outcome.NO_ANSWER, "canceled": Outcome.NO_ANSWER,
               "failed": Outcome.ERROR}


def _box() -> SecretBox:
    return SecretBox(instance_settings().encryption_key)


async def _form(request: Request) -> dict:
    body = (await request.body()).decode("utf-8", "replace")
    return dict(parse_qsl(body, keep_blank_values=True))


def signature_ok(token: Optional[str], request: Request, form: dict, base_url: Optional[str] = None) -> bool:
    if not token:
        return False  # a platform workspace without an auth token cannot be verified: refuse
    base = (base_url or instance_settings().public_base_url or str(request.base_url)).rstrip("/")
    url = base + request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RequestValidator(token).validate(url, form, request.headers.get("X-Twilio-Signature", ""))


def _twiml(body: str) -> Response:
    return Response(content=f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>',
                    media_type="application/xml")


def _say_and_hang_up(text: str) -> Response:
    return _twiml(f"<Say>{escape(text)}</Say><Hangup/>")


# -- media stream context ---------------------------------------------------------------------


@dataclass
class PlatformCall:
    call_id: str
    workspace_id: str
    agent_id: Optional[str]
    lead_id: Optional[str]
    direction: str
    config: AgentConfig
    settings: Settings
    contact: dict


async def load_platform_call(params: dict) -> Optional[PlatformCall]:
    """Everything a stream needs, loaded once at the start handshake (never during the call).
    Returns None when the token is invalid or the call is not live."""

    call_id, token = params.get("call_id", ""), params.get("token", "")
    if not verify_call(call_id, token):
        logger.warning("stream refused: bad call token")
        return None
    db = get_database()
    async with db.sessionmaker() as s:
        call = await s.get(Call, call_id)
        if call is None or call.status not in ("dialing", "in_progress"):
            return None
        agent = await s.get(Agent, call.agent_id) if call.agent_id else None
        if agent is None or agent.workspace_id != call.workspace_id:
            return None
        settings = await WorkspaceRepo(s, call.workspace_id).provider_settings(
            _box(), instance_settings().public_base_url
        )
        lead = await s.get(Lead, call.lead_id) if call.lead_id else None
        call.status = "in_progress"
        call.updated_at = utcnow()
        await s.commit()
        contact = {
            "name": (lead.name if lead else call.contact_name) or "there",
            "phone": call.contact_phone,
            "email": lead.email if lead else "",
            "timezone": lead.timezone if lead else "",
            "suggested_time": "later this week",
        }
        return PlatformCall(
            call_id=call.id, workspace_id=call.workspace_id, agent_id=agent.id,
            lead_id=call.lead_id, direction=call.direction,
            config=AgentConfig.from_dict(agent.config), settings=settings, contact=contact,
        )


async def save_platform_result(ctx: PlatformCall, result: CallResult) -> None:
    async with get_database().sessionmaker() as s:
        await record_call_result(
            s, ctx.workspace_id, result, call_id=ctx.call_id, agent_id=ctx.agent_id,
            lead_id=ctx.lead_id, direction=ctx.direction,
        )
        await s.commit()


async def fail_platform_call(call_id: str, reason: str) -> None:
    async with get_database().sessionmaker() as s:
        call = await s.get(Call, call_id)
        if call is not None and call.status in ("dialing", "in_progress"):
            call.status, call.outcome, call.end_reason = "failed", "error", reason[:300]
            await s.commit()


# -- inbound ----------------------------------------------------------------------------------


@router.post("/voice/inbound/{workspace_slug}")
async def inbound(workspace_slug: str, request: Request) -> Response:
    form = await _form(request)
    async with get_database().sessionmaker() as s:
        ws = await s.scalar(select(Workspace).where(Workspace.slug == workspace_slug))
        if ws is None:
            return Response(status_code=404)
        repo = WorkspaceRepo(s, ws.id)
        settings = await repo.provider_settings(_box(), instance_settings().public_base_url)
        if not signature_ok(settings.twilio_auth_token, request, form):
            return Response(status_code=403)

        to = phones.normalize(form.get("To", "")) or form.get("To", "")
        caller = phones.normalize(form.get("From", "")) or form.get("From", "")
        number = await s.scalar(
            select(PhoneNumber).where(PhoneNumber.workspace_id == ws.id, PhoneNumber.e164 == to)
        )
        agent = None
        if number is not None and number.inbound_agent_id:
            agent = await repo.agent(number.inbound_agent_id)
        if agent is None:
            agent = await s.scalar(
                select(Agent).where(Agent.workspace_id == ws.id, Agent.mode == "inbound", Agent.is_active.is_(True))
                .order_by(Agent.created_at)
            )
        if agent is None:
            return _say_and_hang_up("Sorry, this line is not set up yet. Please try again later.")
        missing = [m for m in settings.missing_for_live_call() if m not in ("TWILIO_FROM_NUMBER",)]
        if missing:
            logger.warning("inbound call to %s refused, workspace missing %s", ws.slug, missing)
            return _say_and_hang_up("Sorry, we can't take your call right now. Please try again later.")

        lead = await s.scalar(select(Lead).where(Lead.workspace_id == ws.id, Lead.phone == caller)) if caller else None
        call = Call(
            workspace_id=ws.id, agent_id=agent.id, lead_id=lead.id if lead else None,
            twilio_sid=form.get("CallSid", ""), direction="inbound", status="in_progress",
            contact_name=lead.name if lead else "", contact_phone=caller[:20],
        )
        s.add(call)
        await s.commit()
        params = {
            "call_id": call.id, "token": sign_call(call.id), "phone": caller,
            "name": (lead.name if lead else "") or "there",
        }
    tags = "".join(f'<Parameter name="{escape(k)}" value="{escape(str(v))}"/>' for k, v in params.items())
    url = stream_url(instance_settings().public_base_url or str(request.base_url))
    return _twiml(f'<Connect><Stream url="{escape(url)}">{tags}</Stream></Connect>')


# -- status + AMD -----------------------------------------------------------------------------


async def _workspace_token(s, call: Call) -> Optional[str]:
    settings = await WorkspaceRepo(s, call.workspace_id).provider_settings(_box(), None)
    return settings.twilio_auth_token


@router.post("/voice/status")
async def status_callback(request: Request, call_id: str = "") -> Response:
    form = await _form(request)
    async with get_database().sessionmaker() as s:
        call = await s.get(Call, call_id) if call_id else None
        if call is None:
            return Response(status_code=404)
        if not signature_ok(await _workspace_token(s, call), request, form):
            return Response(status_code=403)
        twilio_status = form.get("CallStatus", "")
        if call.status == "dialing":
            # Never reached the media stream: record the outcome here.
            outcome = _NO_CONNECT.get(twilio_status, Outcome.ERROR)
            reason = {"busy": "line busy", "no-answer": "no answer", "canceled": "canceled",
                      "failed": "call failed"}.get(twilio_status, "answered but the stream never connected")
            result = CallResult(
                call_sid=form.get("CallSid", call.twilio_sid), contact_name=call.contact_name,
                contact_phone=call.contact_phone, outcome=outcome, duration_seconds=0.0, end_reason=reason,
            )
            await record_call_result(s, call.workspace_id, result, call_id=call.id,
                                     agent_id=call.agent_id, lead_id=call.lead_id, direction=call.direction)
            await s.commit()
    return Response(status_code=204)


def amd_router(sessions: dict, early: dict) -> APIRouter:
    """The AMD route needs the live-session registry owned by the server app."""

    r = APIRouter()

    @r.post("/voice/amd")
    async def amd(request: Request, call_id: str = "") -> Response:
        form = await _form(request)
        if call_id:
            async with get_database().sessionmaker() as s:
                call = await s.get(Call, call_id)
                if call is None:
                    return Response(status_code=404)
                token = await _workspace_token(s, call)
            if not signature_ok(token, request, form):
                return Response(status_code=403)
        else:
            # CLI dev mode: the single .env account.
            settings = request.app.state.settings
            if settings.twilio_auth_token and not signature_ok(
                settings.twilio_auth_token, request, form, base_url=settings.public_base_url
            ):
                return Response(status_code=403)
        sid, answered_by = form.get("CallSid", ""), form.get("AnsweredBy", "")
        if sid and answered_by in MACHINE_ANSWERS:
            session = sessions.get(sid)
            if session is not None:
                session.notify_machine(answered_by)
            else:
                early[sid] = answered_by
        return Response(status_code=204)

    return r
