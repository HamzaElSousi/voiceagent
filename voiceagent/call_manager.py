"""Campaign orchestration: load contacts, gate them through compliance and the budget, dial.

The campaign runs headlessly from the CLI (run-list contacts.csv). The dialing mechanism itself
(placing a Twilio call and receiving its Media Stream) is injected as a `call_runner` so the
orchestration logic is testable with a fake runner and zero telephony, while production injects
the real Twilio-backed runner.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

import aiofiles

from .compliance import ComplianceGate
from .config import AgentConfig
from .contracts import CallResult, Outcome
from .cost import CampaignBudget

# A call runner places one call and returns its result. Injected for testability.
CallRunner = Callable[[dict], Awaitable[CallResult]]


# Accepted header aliases so a user's CSV does not have to match exactly.
_ALIASES = {
    "name": {"name", "contact_name", "full_name"},
    "phone": {"phone", "number", "phone_number", "contact_phone"},
    "email": {"email", "contact_email", "e-mail"},
    "suggested_time": {"suggested_time", "time", "slot", "meeting_time"},
}


def _map_row(row: dict) -> dict:
    out = {"name": "", "phone": "", "email": "", "suggested_time": ""}
    # Build two lookups: one with the raw lowercased key and one with spaces
    # replaced by underscores, so "Full Name" matches the alias "full_name".
    lowered = {str(k).strip().lower(): (v or "").strip() for k, v in row.items()}
    lowered_underscored = {k.replace(" ", "_"): v for k, v in lowered.items()}
    lookup = {**lowered, **lowered_underscored}
    for field_name, aliases in _ALIASES.items():
        for key in lookup:
            if key in aliases:
                out[field_name] = lookup[key]
                break
    return out


async def load_contacts(path: str | Path) -> list[dict]:
    """Load a contacts CSV into a list of {name, phone, email, suggested_time} dicts."""

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"contacts file not found: {p}")
    async with aiofiles.open(p, "r", encoding="utf-8") as fh:
        content = await fh.read()
    reader = csv.DictReader(io.StringIO(content))
    contacts = [_map_row(row) for row in reader]
    return [c for c in contacts if c["phone"]]


@dataclass
class CampaignSummary:
    dialed: int = 0
    skipped: int = 0
    total_usd: float = 0.0
    outcomes: dict[str, int] = field(default_factory=dict)
    results: list[CallResult] = field(default_factory=list)

    def _count(self, outcome: str) -> None:
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1


class CampaignRunner:
    """Runs a contact list sequentially, enforcing compliance and the campaign budget."""

    def __init__(
        self,
        config: AgentConfig,
        call_runner: CallRunner,
        *,
        compliance: Optional[ComplianceGate] = None,
        outcome_sink=None,
    ):
        self.config = config
        self.call_runner = call_runner
        self.compliance = compliance or ComplianceGate(config.compliance)
        self.budget = CampaignBudget(per_campaign_cap_usd=config.limits.per_campaign_usd)
        self.outcome_sink = outcome_sink

    async def run_list(self, contacts: list[dict]) -> CampaignSummary:
        summary = CampaignSummary()
        for contact in contacts:
            if self.budget.exhausted():
                break

            allowed, reason = self.compliance.can_dial(contact.get("phone", ""))
            if not allowed:
                summary.skipped += 1
                summary._count(f"skipped:{reason}")
                continue

            result = await self.call_runner(contact)
            self.budget.record_call(result.cost.total_usd)
            summary.dialed += 1
            summary.total_usd = round(summary.total_usd + result.cost.total_usd, 6)
            summary._count(result.outcome.value)
            summary.results.append(result)
            if self.outcome_sink is not None:
                await self.outcome_sink.record(result)

        return summary

    async def run_one(self, contact: dict) -> Optional[CallResult]:
        allowed, reason = self.compliance.can_dial(contact.get("phone", ""))
        if not allowed:
            return None
        result = await self.call_runner(contact)
        self.budget.record_call(result.cost.total_usd)
        if self.outcome_sink is not None:
            await self.outcome_sink.record(result)
        return result
