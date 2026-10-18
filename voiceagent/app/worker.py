"""The background worker (`voiceagent worker`, its own container in docker compose).

Every few seconds: dial for running campaigns, deliver the outbox. Less often: sync connectors
that are due, and enforce each workspace's data-retention window. Each job is isolated so one
failure (a CRM timing out) never stops the others.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from datetime import timedelta
from pathlib import Path
from typing import Optional

from sqlalchemy import delete, select

from .campaigns import tick
from .crypto import SecretBox
from .db import Database, get_database
from .leads import sync_connector
from .models import Call, Connector, Workspace, utcnow
from .outbox import deliver_due
from .settings import instance_settings

logger = logging.getLogger("voiceagent.worker")

DEFAULT_SYNC_MINUTES = 15
RETENTION_EVERY_SECONDS = 3600


def max_concurrent_calls() -> int:
    return max(1, int(os.getenv("MAX_CONCURRENT_CALLS", "3")))


async def sync_due_connectors(db: Database, box: SecretBox) -> int:
    synced = 0
    now = utcnow()
    async with db.sessionmaker() as s:
        conns = list(await s.scalars(
            select(Connector).where(Connector.enabled.is_(True), Connector.type.in_(("mcp", "webhook")))
        ))
    for conn in conns:
        minutes = int((conn.config or {}).get("sync_minutes") or DEFAULT_SYNC_MINUTES)
        last = conn.last_sync_at
        if last is not None and last.tzinfo is None:
            from datetime import timezone

            last = last.replace(tzinfo=timezone.utc)
        if last is not None and now - last < timedelta(minutes=minutes):
            continue
        async with db.sessionmaker() as s:
            fresh = await s.get(Connector, conn.id)
            if fresh is None:
                continue
            try:
                report = await sync_connector(s, fresh, box)
                await s.commit()
                synced += 1
                if report.errors:
                    logger.warning("sync %s: %s", conn.id, report.errors[0])
            except Exception:
                logger.exception("sync %s failed", conn.id)
    return synced


async def enforce_retention(db: Database, events_root: str = "call_logs/events") -> int:
    """Delete calls (and their event timelines) older than each workspace's retention window."""

    removed = 0
    async with db.sessionmaker() as s:
        for ws in list(await s.scalars(select(Workspace))):
            cutoff = utcnow() - timedelta(days=ws.retention_days)
            old = list(await s.scalars(select(Call).where(Call.workspace_id == ws.id, Call.created_at < cutoff)))
            for call in old:
                path = Path(events_root) / ws.id / f"{call.twilio_sid}.jsonl"
                if call.twilio_sid and path.exists():
                    await asyncio.to_thread(path.unlink)
            if old:
                await s.execute(delete(Call).where(Call.id.in_([c.id for c in old])))
                removed += len(old)
        await s.commit()
    return removed


async def remove_workspace_events(workspace_id: str, events_root: str = "call_logs/events") -> None:
    path = Path(events_root) / workspace_id
    if path.exists():
        await asyncio.to_thread(shutil.rmtree, path, True)


async def run_once(db: Database, box: SecretBox, *, do_sync: bool = True, do_retention: bool = False) -> dict:
    out: dict = {}
    settings = instance_settings()
    try:
        async with db.sessionmaker() as s:
            out["campaigns"] = await tick(
                s, box, public_base_url=settings.public_base_url, max_concurrent=max_concurrent_calls()
            )
    except Exception:
        logger.exception("campaign tick failed")
    try:
        async with db.sessionmaker() as s:
            out["outbox"] = await deliver_due(s, box)
    except Exception:
        logger.exception("outbox delivery failed")
    if do_sync:
        try:
            out["synced"] = await sync_due_connectors(db, box)
        except Exception:
            logger.exception("connector sync failed")
    if do_retention:
        try:
            out["retention_removed"] = await enforce_retention(db)
        except Exception:
            logger.exception("retention failed")
    return out


async def run_forever(interval: float = 5.0, stop: Optional[asyncio.Event] = None) -> None:
    db = get_database()
    box = SecretBox(instance_settings().encryption_key)
    stop = stop or asyncio.Event()
    last_sync = 0.0
    last_retention = 0.0
    logger.info("worker started (interval %.0fs, max %d concurrent calls)", interval, max_concurrent_calls())
    while not stop.is_set():
        now = time.monotonic()
        do_sync = now - last_sync >= 60
        do_retention = now - last_retention >= RETENTION_EVERY_SECONDS
        await run_once(db, box, do_sync=do_sync, do_retention=do_retention)
        if do_sync:
            last_sync = now
        if do_retention:
            last_retention = now
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    await db.dispose()
