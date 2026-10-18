"""Leads: build connectors from stored config, sync leads in, import CSVs, manage the DNC list."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import phones
from ..connectors.base import ConnectorError, CrmConnector, LeadRecord
from ..connectors.mcp import McpConnector
from ..connectors.webhook import WebhookConnector
from .crypto import SecretBox
from .models import Connector, DncEntry, Lead, utcnow

CONNECTOR_TYPES = ("mcp", "webhook", "csv")

ConnectorFactory = Callable[[Connector, SecretBox], CrmConnector]


def build_connector(conn: Connector, box: SecretBox) -> CrmConnector:
    secret = box.decrypt(conn.secret_ciphertext) if conn.secret_ciphertext else ""
    if conn.type == "mcp":
        return McpConnector(conn.config or {}, secret)
    if conn.type == "webhook":
        return WebhookConnector(conn.config or {}, secret)
    raise ConnectorError(f"{conn.type} connectors do not sync; import a CSV instead")


def parse_consent(value: Optional[str]) -> Optional[datetime]:
    """A consent timestamp, or None. Only real dates count: "yes" or "true" is not evidence of
    when or how consent was given."""

    if not value:
        return None
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            d = date.fromisoformat(text[:10])
        except ValueError:
            return None
        dt = datetime(d.year, d.month, d.day)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if dt > utcnow():
        return None  # consent "in the future" is a data error, not consent
    return dt


@dataclass
class SyncReport:
    created: int = 0
    updated: int = 0
    invalid_phone: int = 0
    without_consent: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "created": self.created,
            "updated": self.updated,
            "invalid_phone": self.invalid_phone,
            "without_consent": self.without_consent,
            "errors": self.errors,
        }


async def upsert_leads(
    db: AsyncSession, workspace_id: str, connector_id: Optional[str], records: Iterable[LeadRecord]
) -> SyncReport:
    report = SyncReport()
    for rec in records:
        e164 = phones.normalize(rec.phone)
        if e164 is None:
            report.invalid_phone += 1
            continue
        consent = parse_consent(rec.consent_at)
        if consent is None:
            report.without_consent += 1
        lead = await db.scalar(
            select(Lead).where(
                Lead.workspace_id == workspace_id,
                Lead.connector_id == connector_id if connector_id else Lead.connector_id.is_(None),
                Lead.external_id == rec.external_id,
            )
        )
        if lead is None:
            lead = Lead(workspace_id=workspace_id, connector_id=connector_id, external_id=rec.external_id)
            db.add(lead)
            report.created += 1
        else:
            report.updated += 1
        lead.phone = e164
        lead.name = rec.name[:200]
        lead.email = rec.email[:320]
        lead.company = rec.company[:200]
        lead.timezone = rec.timezone[:64]
        # The CRM is the source of truth for consent, including withdrawing it.
        lead.consent_at = consent
        lead.consent_evidence = rec.consent_evidence[:2000]
        lead.raw = rec.raw
    await db.flush()
    return report


async def sync_connector(
    db: AsyncSession, conn: Connector, box: SecretBox, factory: Optional[ConnectorFactory] = None
) -> SyncReport:
    connector = (factory or build_connector)(conn, box)
    try:
        records = await connector.fetch_leads()
    except ConnectorError as exc:
        conn.last_error = str(exc)[:1000]
        conn.last_sync_at = utcnow()
        await db.flush()
        return SyncReport(errors=[str(exc)])
    finally:
        close = getattr(connector, "close", None)
        if close is not None:
            await close()
    report = await upsert_leads(db, conn.workspace_id, conn.id, records)
    conn.last_error = None
    conn.last_sync_at = utcnow()
    await db.flush()
    return report


# -- DNC -------------------------------------------------------------------------------------


async def dnc_set(db: AsyncSession, workspace_id: str) -> set[str]:
    rows = await db.scalars(select(DncEntry.phone).where(DncEntry.workspace_id == workspace_id))
    return set(rows)


async def add_dnc(db: AsyncSession, workspace_id: str, phone: str, reason: str = "manual", source: str = "") -> bool:
    """Add a number (normalized). Returns False if it was already listed or is not a number."""

    e164 = phones.normalize(phone)
    if e164 is None:
        return False
    exists = await db.scalar(
        select(DncEntry.id).where(DncEntry.workspace_id == workspace_id, DncEntry.phone == e164)
    )
    if exists:
        return False
    db.add(DncEntry(workspace_id=workspace_id, phone=e164, reason=reason, source=source[:200]))
    await db.flush()
    return True
