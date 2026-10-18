"""Field mapping: turn whatever JSON a CRM returns into LeadRecords, and render outcome arguments
from templates. Paths use a tiny JSONPath subset: `$.a.b`, `$.items[0].phone`, `$` (the root).

    field_map = {"external_id": "$.id", "phone": "$.phone", "consent_at": "$.call_consent_at"}
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from .base import ConnectorError, LeadRecord

_TOKEN_RE = re.compile(r"\.([A-Za-z0-9_\-]+)|\[(\d+)\]")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.\[\]]+)\s*\}\}")

LEAD_FIELDS = (
    "external_id", "phone", "name", "email", "company", "timezone", "consent_at", "consent_evidence",
)
DEFAULT_FIELD_MAP = {f: f"$.{f}" for f in LEAD_FIELDS}


def get_path(data: Any, path: str) -> Any:
    """Resolve `$.a.b[0]` against data. Returns None when any step is missing."""

    if not path or path == "$":
        return data
    if not path.startswith("$"):
        raise ConnectorError(f"mapping path must start with '$': {path!r}")
    rest = path[1:]
    pos = 0
    cur = data
    for m in _TOKEN_RE.finditer(rest):
        if m.start() != pos:
            raise ConnectorError(f"invalid mapping path {path!r}")
        pos = m.end()
        key, index = m.group(1), m.group(2)
        if key is not None:
            if not isinstance(cur, dict) or key not in cur:
                return None
            cur = cur[key]
        else:
            i = int(index)
            if not isinstance(cur, list) or i >= len(cur):
                return None
            cur = cur[i]
    if pos != len(rest):
        raise ConnectorError(f"invalid mapping path {path!r}")
    return cur


def extract_items(data: Any, items_path: str = "$") -> list[dict]:
    """The list of lead objects inside a response. Accepts a list, or common wrappers like
    {"leads": [...]}, {"data": [...]}, {"items": [...]} when items_path is the root."""

    found = get_path(data, items_path)
    if isinstance(found, dict) and items_path in ("", "$"):
        for key in ("leads", "data", "items", "results", "records"):
            if isinstance(found.get(key), list):
                found = found[key]
                break
    if found is None:
        return []
    if not isinstance(found, list):
        raise ConnectorError(f"expected a list of leads at {items_path!r}, got {type(found).__name__}")
    return [item for item in found if isinstance(item, dict)]


def map_lead(item: dict, field_map: dict[str, str]) -> Optional[LeadRecord]:
    """One CRM object -> LeadRecord. Returns None when there is no phone to call."""

    fm = {**DEFAULT_FIELD_MAP, **(field_map or {})}
    values: dict[str, Any] = {}
    for name in LEAD_FIELDS:
        value = get_path(item, fm[name]) if fm.get(name) else None
        values[name] = "" if value is None else str(value).strip()
    if not values["phone"]:
        return None
    external_id = values["external_id"] or values["phone"]
    return LeadRecord(
        external_id=external_id,
        phone=values["phone"],
        name=values["name"],
        email=values["email"],
        company=values["company"],
        timezone=values["timezone"],
        consent_at=values["consent_at"] or None,
        consent_evidence=values["consent_evidence"],
        raw=item,
    )


def render_args(template: Any, context: dict) -> Any:
    """Fill `{{path}}` placeholders from context. A value that is exactly one placeholder keeps
    the original type (numbers stay numbers); otherwise placeholders are interpolated as text."""

    if isinstance(template, dict):
        return {k: render_args(v, context) for k, v in template.items()}
    if isinstance(template, list):
        return [render_args(v, context) for v in template]
    if not isinstance(template, str):
        return template
    whole = _PLACEHOLDER_RE.fullmatch(template.strip())
    if whole:
        return get_path(context, "$." + whole.group(1))

    def _sub(m: re.Match) -> str:
        value = get_path(context, "$." + m.group(1))
        if value is None:
            return ""
        return value if isinstance(value, str) else json.dumps(value)

    return _PLACEHOLDER_RE.sub(_sub, template)
