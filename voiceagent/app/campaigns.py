"""The campaign scheduler: one `tick()` dials whatever is eligible right now.

Safety properties:
  - a lead is CLAIMED (attempts + 1, a Call row in "dialing") and committed before Twilio is asked
    to dial, so a crash or restart can never dial the same lead twice;
  - concurrency is counted from the database (calls in dialing / in_progress), so it holds across
    the web and worker processes;
  - every lead passes eligibility.check (consent, DNC, callee-local hours, attempts) at dial time;
  - a campaign stops itself on budget, missing keys, or a misconfigured agent, with a reason.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .. import phones
from ..config import AgentConfig, AgentConfigError
from . import eligibility
from .crypto import SecretBox
from .dialer import TelephonyFactory, place_call, twilio_factory
from .leads import dnc_set
from .models import Agent, Call, Campaign, Lead, utcnow
from .repo import WorkspaceRepo

logger = logging.getLogger("voiceagent.campaigns")

ACTIVE = ("dialing", "in_progress")
STALE_AFTER = timedelta(minutes=15)   # no stream and no status callback by then: give up on it
RETRYABLE = ("new", "called")         # "called" = no answer / voicemail / hung up last time
MAX_DIAL_ERRORS = 3                   # consecutive provider errors before a campaign pauses


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    from datetime import timezone

    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)  # SQLite drops tzinfo
    return dt


async def active_calls(db: AsyncSession, now: datetime, *, campaign_id: Optional[str] = None) -> int:
    stmt = select(func.count()).select_from(Call).where(
        Call.status.in_(ACTIVE), Call.updated_at > now - STALE_AFTER
    )
    if campaign_id:
        stmt = stmt.where(Call.campaign_id == campaign_id)
    return int(await db.scalar(stmt) or 0)


async def reap_stale(db: AsyncSession, now: datetime) -> int:
    result = await db.execute(
        update(Call)
        .where(Call.status.in_(ACTIVE), Call.updated_at <= now - STALE_AFTER)
        .values(status="failed", outcome="error", end_reason="no media stream or final status received")
    )
    return int(result.rowcount or 0)


async def campaign_spent(db: AsyncSession, campaign_id: str) -> float:
    return float(await db.scalar(select(func.coalesce(func.sum(Call.cost_usd), 0.0)).where(Call.campaign_id == campaign_id)) or 0.0)


def _base_leads(campaign: Campaign):
    stmt = select(Lead).where(
        Lead.workspace_id == campaign.workspace_id,
        Lead.status.in_(RETRYABLE),
        Lead.consent_at.is_not(None),
        Lead.attempts < campaign.max_attempts,
    )
    if campaign.connector_id:
        stmt = stmt.where(Lead.connector_id == campaign.connector_id)
    return stmt


async def _stop(campaign: Campaign, status: str, reason: str) -> None:
    campaign.status = status
    campaign.status_reason = reason[:200]
    logger.info("campaign %s -> %s (%s)", campaign.id, status, reason)


async def tick(
    db: AsyncSession,
    box: SecretBox,
    *,
    public_base_url: Optional[str],
    now: Optional[datetime] = None,
    max_concurrent: int = 3,
    factory: TelephonyFactory = twilio_factory,
) -> dict:
    now = now or utcnow()
    stats = {"dialed": 0, "errors": 0, "reaped": await reap_stale(db, now)}
    await db.commit()
    instance_active = await active_calls(db, now)

    campaigns = list(
        await db.scalars(select(Campaign).where(Campaign.status == "running").order_by(Campaign.created_at))
    )
    for campaign in campaigns:
        if instance_active >= max_concurrent:
            break
        agent = await db.get(Agent, campaign.agent_id)
        if agent is None or agent.workspace_id != campaign.workspace_id or not agent.is_active:
            await _stop(campaign, "paused", "agent missing or inactive")
            await db.commit()
            continue
        try:
            cfg = AgentConfig.from_dict(agent.config)
        except AgentConfigError as exc:
            await _stop(campaign, "paused", f"agent config invalid: {exc}")
            await db.commit()
            continue
        if cfg.mode != "outbound":
            await _stop(campaign, "paused", "campaigns need an outbound agent")
            await db.commit()
            continue
        settings = await WorkspaceRepo(db, campaign.workspace_id).provider_settings(box, public_base_url)
        missing = settings.missing_for_live_call()
        if cfg.booking.provider == "calcom" and not settings.calcom_api_key:
            missing.append("CALCOM_API_KEY")
        if missing:
            await _stop(campaign, "paused", "missing keys: " + ", ".join(missing))
            await db.commit()
            continue
        if await campaign_spent(db, campaign.id) >= campaign.budget_usd:
            await _stop(campaign, "done", "budget reached")
            await db.commit()
            continue

        running_here = await active_calls(db, now, campaign_id=campaign.id)
        capacity = min(campaign.concurrency - running_here, max_concurrent - instance_active)
        retry_cutoff = now - timedelta(minutes=campaign.retry_delay_minutes)
        busy_leads = select(Call.lead_id).where(Call.status.in_(ACTIVE), Call.lead_id.is_not(None))
        due = list(
            await db.scalars(
                _base_leads(campaign)
                .where(or_(Lead.last_called_at.is_(None), Lead.last_called_at <= retry_cutoff))
                .where(Lead.id.not_in(busy_leads))
                .order_by(Lead.attempts, Lead.created_at)
                .limit(200)
            )
        )
        if not due:
            remaining = await db.scalar(select(func.count()).select_from(_base_leads(campaign).subquery()))
            if not remaining and running_here == 0:
                await _stop(campaign, "done", "no more leads to call")
                await db.commit()
            continue
        if capacity <= 0:
            continue

        dnc = await dnc_set(db, campaign.workspace_id)
        hours = cfg.compliance.calling_hours
        errors = 0
        for lead in due:
            if capacity <= 0 or instance_active >= max_concurrent:
                break
            verdict = eligibility.check(
                phone=lead.phone, consent_at=_aware(lead.consent_at), dnc=dnc, now=now,
                hours_start=hours.start, hours_end=hours.end, lead_timezone=lead.timezone,
                fallback_tz=hours.timezone, attempts=lead.attempts, max_attempts=campaign.max_attempts,
            )
            if not verdict.ok:
                continue
            # Claim first, dial second.
            call = Call(
                workspace_id=campaign.workspace_id,
                agent_id=agent.id,
                lead_id=lead.id,
                campaign_id=campaign.id,
                direction="outbound",
                status="dialing",
                contact_name=lead.name,
                contact_phone=lead.phone,
            )
            db.add(call)
            lead.attempts += 1
            lead.last_called_at = now
            await db.commit()
            try:
                call.twilio_sid = await place_call(
                    call, lead, settings, detect_voicemail=cfg.voicemail.detect, factory=factory
                )
                call.updated_at = utcnow()
                stats["dialed"] += 1
                capacity -= 1
                instance_active += 1
                errors = 0
            except Exception as exc:  # provider rejected the call: record, maybe pause
                call.status, call.outcome = "failed", "error"
                call.error = repr(exc)[:1000]
                call.end_reason = "could not place the call"
                stats["errors"] += 1
                errors += 1
                logger.warning("dial failed for %s: %r", phones.mask(lead.phone), exc)
                if errors >= MAX_DIAL_ERRORS:
                    await _stop(campaign, "paused", f"{errors} dial errors in a row: {exc!r}"[:200])
            await db.commit()
            if campaign.status != "running":
                break
    return stats
