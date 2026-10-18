"""CRM connectors: create (MCP / webhook / CSV), test the connection, sync leads now."""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, ValidationError

from ...connectors.base import ConnectorError
from ...connectors.mapping import LEAD_FIELDS, extract_items, map_lead
from .. import repo
from ..crypto import SecretBox, last4, mask
from ..deps import WorkspaceAccess, secret_box, workspace_access
from ..leads import build_connector, sync_connector
from ..models import Connector

router = APIRouter(tags=["connectors"])


class McpConfig(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    auth_header: str = Field("Authorization", max_length=100)  # or e.g. "X-API-Key"
    lead_tool: str = Field("", max_length=200)
    lead_args: dict[str, Any] = Field(default_factory=dict)
    items_path: str = Field("$", max_length=200)
    field_map: dict[str, str] = Field(default_factory=dict)
    outcome_tool: str = Field("", max_length=200)
    outcome_args: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class WebhookConfig(BaseModel):
    leads_url: str = Field("", max_length=2000)
    outcome_url: str = Field("", max_length=2000)
    items_path: str = Field("$", max_length=200)
    field_map: dict[str, str] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


_CONFIG_MODELS = {"mcp": McpConfig, "webhook": WebhookConfig}


class ConnectorIn(BaseModel):
    type: Literal["mcp", "webhook", "csv"]
    name: str = Field(min_length=1, max_length=200)
    config: dict[str, Any] = Field(default_factory=dict)
    secret: Optional[str] = Field(None, max_length=4096)  # write-only


class ConnectorPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    config: Optional[dict[str, Any]] = None
    secret: Optional[str] = Field(None, max_length=4096)
    enabled: Optional[bool] = None


def _validate_config(kind: str, config: dict) -> dict:
    model = _CONFIG_MODELS.get(kind)
    if model is None:
        return {}
    try:
        cfg = model.model_validate(config).model_dump()
    except ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    unknown = set(cfg.get("field_map", {})) - set(LEAD_FIELDS)
    if unknown:
        raise HTTPException(422, f"unknown lead fields in field_map: {sorted(unknown)}")
    return cfg


def _out(c: Connector) -> dict:
    return {
        "id": c.id,
        "type": c.type,
        "name": c.name,
        "config": c.config,
        "enabled": c.enabled,
        "has_secret": bool(c.secret_ciphertext),
        "secret_masked": mask(c.secret_last4) if c.secret_ciphertext else None,
        "last_sync_at": c.last_sync_at.isoformat() if c.last_sync_at else None,
        "last_error": c.last_error,
    }


async def _get(access: WorkspaceAccess, connector_id: str) -> Connector:
    from sqlalchemy import select

    conn = await access.repo.db.scalar(
        select(Connector).where(Connector.workspace_id == access.repo.workspace_id, Connector.id == connector_id)
    )
    if conn is None:
        raise HTTPException(404, "connector not found")
    return conn


@router.get("/workspaces/{workspace_id}/connectors")
async def list_connectors(access: WorkspaceAccess = Depends(workspace_access)) -> list[dict]:
    from sqlalchemy import select

    rows = await access.repo.db.scalars(
        select(Connector).where(Connector.workspace_id == access.repo.workspace_id).order_by(Connector.created_at)
    )
    return [_out(c) for c in rows]


@router.post("/workspaces/{workspace_id}/connectors", status_code=201)
async def create_connector(
    body: ConnectorIn, access: WorkspaceAccess = Depends(workspace_access), box: SecretBox = Depends(secret_box)
) -> dict:
    access.require_write()
    conn = Connector(
        workspace_id=access.repo.workspace_id,
        type=body.type,
        name=body.name.strip(),
        config=_validate_config(body.type, body.config),
    )
    if body.secret:
        conn.secret_ciphertext = box.encrypt(body.secret.strip())
        conn.secret_last4 = last4(body.secret.strip())
    access.repo.db.add(conn)
    await access.repo.db.flush()
    await repo.audit(
        access.repo.db, "connector.create", user_id=access.auth.user.id, workspace_id=conn.workspace_id, target=conn.name
    )
    await access.repo.db.commit()
    return _out(conn)


@router.patch("/workspaces/{workspace_id}/connectors/{connector_id}")
async def update_connector(
    connector_id: str,
    body: ConnectorPatch,
    access: WorkspaceAccess = Depends(workspace_access),
    box: SecretBox = Depends(secret_box),
) -> dict:
    access.require_write()
    conn = await _get(access, connector_id)
    if body.name is not None:
        conn.name = body.name.strip()
    if body.config is not None:
        conn.config = _validate_config(conn.type, body.config)
    if body.secret is not None:
        value = body.secret.strip()
        conn.secret_ciphertext = box.encrypt(value) if value else ""
        conn.secret_last4 = last4(value) if value else ""
    if body.enabled is not None:
        conn.enabled = body.enabled
    await repo.audit(
        access.repo.db, "connector.update", user_id=access.auth.user.id, workspace_id=conn.workspace_id, target=conn.name
    )
    await access.repo.db.commit()
    return _out(conn)


@router.delete("/workspaces/{workspace_id}/connectors/{connector_id}", status_code=204)
async def delete_connector(connector_id: str, access: WorkspaceAccess = Depends(workspace_access)) -> Response:
    access.require_write()
    conn = await _get(access, connector_id)
    await access.repo.db.delete(conn)
    await repo.audit(
        access.repo.db, "connector.delete", user_id=access.auth.user.id, workspace_id=access.repo.workspace_id, target=connector_id
    )
    await access.repo.db.commit()
    return Response(status_code=204)


@router.post("/workspaces/{workspace_id}/connectors/{connector_id}/test")
async def test_connector(
    connector_id: str, access: WorkspaceAccess = Depends(workspace_access), box: SecretBox = Depends(secret_box)
) -> dict:
    """Reach the CRM and preview up to 3 mapped leads, without saving anything."""

    conn = await _get(access, connector_id)
    if conn.type == "csv":
        return {"ok": True, "info": {"note": "CSV connectors are imported, not tested"}, "preview": []}
    connector = build_connector(conn, box)
    try:
        info = await connector.describe()
        preview = []
        if conn.type == "mcp" and conn.config.get("lead_tool"):
            data = await connector.mcp.call_tool(conn.config["lead_tool"], conn.config.get("lead_args") or {})
            items = extract_items(data, conn.config.get("items_path") or "$")[:3]
            preview = [_lead_preview(map_lead(i, conn.config.get("field_map") or {}), i) for i in items]
        elif conn.type == "webhook" and conn.config.get("leads_url"):
            preview = [_lead_preview(r, r.raw) for r in (await connector.fetch_leads())[:3]]
        return {"ok": True, "info": info, "preview": preview}
    except ConnectorError as exc:
        return {"ok": False, "error": str(exc), "preview": []}
    finally:
        await connector.close()


def _lead_preview(rec, raw: dict) -> dict:
    if rec is None:
        return {"mapped": None, "raw_keys": sorted(raw)[:30], "problem": "no phone after mapping"}
    return {
        "mapped": {
            "external_id": rec.external_id,
            "name": rec.name,
            "phone": rec.phone,
            "email": rec.email,
            "consent_at": rec.consent_at,
        },
        "raw_keys": sorted(raw)[:30],
    }


@router.post("/workspaces/{workspace_id}/connectors/{connector_id}/sync")
async def sync_now(
    connector_id: str, access: WorkspaceAccess = Depends(workspace_access), box: SecretBox = Depends(secret_box)
) -> dict:
    access.require_write()
    conn = await _get(access, connector_id)
    if conn.type == "csv":
        raise HTTPException(422, "CSV connectors are imported with POST .../leads/import")
    report = await sync_connector(access.repo.db, conn, box)
    await repo.audit(
        access.repo.db, "connector.sync", user_id=access.auth.user.id, workspace_id=conn.workspace_id, target=conn.name
    )
    await access.repo.db.commit()
    return report.as_dict()
