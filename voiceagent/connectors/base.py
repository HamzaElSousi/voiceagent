"""Connector contracts and the normalized lead record every connector produces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable


class ConnectorError(Exception):
    """A connector could not do its job (bad URL, auth failure, unexpected response). The message
    is safe to show in the dashboard: it never contains the credential."""


@dataclass
class LeadRecord:
    """A lead as the CRM described it, after field mapping. Phone normalization and consent
    checks happen later, in the platform, so every connector gets the same rules."""

    external_id: str
    phone: str
    name: str = ""
    email: str = ""
    company: str = ""
    timezone: str = ""
    consent_at: Optional[str] = None       # when the contact agreed to be called (ISO-8601)
    consent_evidence: str = ""             # how: "web form 2026-10-01, ip 1.2.3.4", "inbound call"
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class CrmConnector(Protocol):
    async def fetch_leads(self) -> list[LeadRecord]:
        """Leads ready to be called (the CRM decides which; we still gate on consent)."""

    async def push_outcome(self, payload: dict[str, Any]) -> None:
        """Deliver one call outcome. Raises ConnectorError on failure so the outbox retries."""

    async def describe(self) -> dict[str, Any]:
        """Connection test for the dashboard: reachable, plus anything useful (e.g. MCP tools)."""
