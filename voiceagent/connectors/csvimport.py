"""CSV lead import. Consent columns are first-class: rows without consent are imported but will
never be dialed by an outbound campaign until consent is recorded."""

from __future__ import annotations

import csv
import io

from .base import ConnectorError, LeadRecord

_ALIASES = {
    "external_id": {"external_id", "id", "lead_id", "crm_id"},
    "name": {"name", "contact_name", "full_name"},
    "phone": {"phone", "number", "phone_number", "contact_phone", "mobile"},
    "email": {"email", "contact_email", "e-mail"},
    "company": {"company", "business", "organization", "organisation"},
    "timezone": {"timezone", "time_zone", "tz"},
    "consent_at": {"consent_at", "call_consent_at", "consented_at", "consent_date"},
    "consent_evidence": {"consent_evidence", "consent_source", "consent"},
}
MAX_ROWS = 5000


def parse_csv(text: str) -> tuple[list[LeadRecord], int]:
    """Returns (leads, skipped rows without a phone)."""

    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames:
        raise ConnectorError("CSV has no header row")
    lookup: dict[str, str] = {}
    for header in reader.fieldnames:
        key = header.strip().lower().replace(" ", "_")
        for field, aliases in _ALIASES.items():
            if key in aliases and field not in lookup:
                lookup[field] = header
    if "phone" not in lookup:
        raise ConnectorError("CSV needs a phone column")
    leads: list[LeadRecord] = []
    skipped = 0
    for i, row in enumerate(reader):
        if i >= MAX_ROWS:
            raise ConnectorError(f"CSV is limited to {MAX_ROWS} rows per import")
        values = {f: (row.get(h) or "").strip() for f, h in lookup.items()}
        if not values.get("phone"):
            skipped += 1
            continue
        leads.append(
            LeadRecord(
                external_id=values.get("external_id") or values["phone"],
                phone=values["phone"],
                name=values.get("name", ""),
                email=values.get("email", ""),
                company=values.get("company", ""),
                timezone=values.get("timezone", ""),
                consent_at=values.get("consent_at") or None,
                consent_evidence=values.get("consent_evidence", ""),
                raw={k: v for k, v in row.items() if k},
            )
        )
    return leads, skipped
