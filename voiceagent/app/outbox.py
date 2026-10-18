"""Outbox: call outcomes are written here in the same transaction as the call record, then
delivered to the CRM by the worker with exponential backoff. A CRM that is down for hours still
gets every outcome once it is back; after MAX_ATTEMPTS an event is marked dead for the dashboard."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..connectors.base import ConnectorError
from .crypto import SecretBox
from .leads import ConnectorFactory, build_connector
from .models import Call, Connector, Lead, OutboxEvent, utcnow

logger = logging.getLogger("voiceagent.outbox")

MAX_ATTEMPTS = 12
BASE_DELAY = 30.0        # seconds; doubles each attempt
MAX_DELAY = 6 * 3600.0


def backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(MAX_DELAY, BASE_DELAY * (2 ** max(0, attempts - 1))))


def outcome_payload(call: Call, lead: Optional[Lead]) -> dict:
    summary = " / ".join(
        f"{t.get('speaker')}: {t.get('text')}" for t in (call.transcript or [])[-6:]
    )[:1500]
    return {
        "event": "call.completed",
        "call_id": call.id,
        "direction": call.direction,
        "outcome": call.outcome,
        "end_reason": call.end_reason,
        "duration_seconds": round(call.duration_seconds or 0.0, 1),
        "booking_ref": call.booking_ref,
        "booked_slot": call.booked_slot,
        "summary": summary,
        "occurred_at": (call.updated_at or utcnow()).isoformat(),
        "lead": {
            "id": lead.id,
            "external_id": lead.external_id,
            "name": lead.name,
            "phone": lead.phone,
            "email": lead.email,
        }
        if lead
        else None,
    }


async def enqueue(db: AsyncSession, workspace_id: str, connector_id: str, payload: dict) -> OutboxEvent:
    ev = OutboxEvent(workspace_id=workspace_id, connector_id=connector_id, payload=payload)
    db.add(ev)
    await db.flush()
    return ev


async def deliver_due(
    db: AsyncSession,
    box: SecretBox,
    *,
    now: Optional[datetime] = None,
    limit: int = 50,
    factory: Optional[ConnectorFactory] = None,
) -> dict:
    """Deliver due events. Commits after each one so a crash mid-batch never re-sends delivered
    events. Returns counts for logging."""

    now = now or utcnow()
    due = list(
        await db.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.delivered_at.is_(None), OutboxEvent.dead.is_(False), OutboxEvent.next_attempt_at <= now)
            .order_by(OutboxEvent.next_attempt_at)
            .limit(limit)
        )
    )
    stats = {"delivered": 0, "failed": 0, "dead": 0}
    for ev in due:
        conn = await db.get(Connector, ev.connector_id)
        if conn is None or not conn.enabled:
            ev.dead, ev.last_error = True, "connector removed or disabled"
            stats["dead"] += 1
            await db.commit()
            continue
        connector = None
        try:
            connector = (factory or build_connector)(conn, box)
            await connector.push_outcome(ev.payload)
            ev.delivered_at = utcnow()
            stats["delivered"] += 1
        except (ConnectorError, ValueError) as exc:
            ev.attempts += 1
            ev.last_error = str(exc)[:1000]
            if ev.attempts >= MAX_ATTEMPTS:
                ev.dead = True
                stats["dead"] += 1
            else:
                ev.next_attempt_at = now + backoff(ev.attempts)
                stats["failed"] += 1
            logger.warning("outbox delivery failed (%s attempt %d): %s", ev.id, ev.attempts, ev.last_error)
        finally:
            close = getattr(connector, "close", None)
            if close is not None:
                await close()
        await db.commit()
    return stats
