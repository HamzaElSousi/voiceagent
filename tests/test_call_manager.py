"""Campaign orchestration tests: contact loading, budget stop, compliance skip. Fake runner."""

from __future__ import annotations

from voiceagent.call_manager import CampaignRunner, load_contacts
from voiceagent.compliance import ComplianceGate
from voiceagent.contracts import CallResult, CostBreakdown, Outcome


async def test_load_contacts_with_header_aliases(tmp_path):
    csv = tmp_path / "contacts.csv"
    csv.write_text(
        "Full Name,Number,E-Mail,Slot\n"
        "John Smith,+14155551234,john@example.com,Tuesday\n"
        "No Phone,,nobody@example.com,Friday\n"  # dropped: no phone
    )
    contacts = await load_contacts(csv)
    assert len(contacts) == 1
    assert contacts[0]["name"] == "John Smith"
    assert contacts[0]["phone"] == "+14155551234"
    assert contacts[0]["email"] == "john@example.com"
    assert contacts[0]["suggested_time"] == "Tuesday"


def _result(phone: str, usd: float, outcome=Outcome.NOT_INTERESTED) -> CallResult:
    return CallResult(
        call_sid="CA",
        contact_name="x",
        contact_phone=phone,
        outcome=outcome,
        duration_seconds=10.0,
        cost=CostBreakdown(total_usd=usd),
        end_reason="done",
    )


async def test_campaign_stops_when_budget_exhausted(cfg, dnc_path):
    cfg.limits.per_campaign_usd = 0.10
    contacts = [{"name": f"c{i}", "phone": f"+1415555000{i}", "email": "", "suggested_time": ""} for i in range(5)]

    async def runner(contact):
        return _result(contact["phone"], 0.06)  # two calls exhaust a $0.10 cap

    compliance = ComplianceGate(cfg.compliance, dnc_path=dnc_path)
    # Force inside calling hours regardless of when the test runs.
    compliance.within_calling_hours = lambda now=None: True  # type: ignore
    campaign = CampaignRunner(cfg, runner, compliance=compliance)
    summary = await campaign.run_list(contacts)
    assert summary.dialed == 2, summary.dialed  # stopped after the cap was hit


async def test_campaign_skips_suppressed_contact(cfg, dnc_path):
    contacts = [
        {"name": "ok", "phone": "+14155550001", "email": "", "suggested_time": ""},
        {"name": "blocked", "phone": "+14155550002", "email": "", "suggested_time": ""},
    ]
    compliance = ComplianceGate(cfg.compliance, dnc_path=dnc_path)
    compliance.within_calling_hours = lambda now=None: True  # type: ignore
    compliance.suppress("+14155550002")

    async def runner(contact):
        return _result(contact["phone"], 0.02)

    campaign = CampaignRunner(cfg, runner, compliance=compliance)
    summary = await campaign.run_list(contacts)
    assert summary.dialed == 1
    assert summary.skipped == 1
