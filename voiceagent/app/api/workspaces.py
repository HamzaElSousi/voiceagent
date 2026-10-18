"""Workspaces: the tenant boundary. Each has its own keys, agents, numbers and call history."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from .. import repo
from ..deps import Auth, WorkspaceAccess, current_auth, get_db, workspace_access
from ..models import Workspace
from ..settings import instance_settings

router = APIRouter(tags=["workspaces"])


class WorkspaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class WorkspacePatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    retention_days: Optional[int] = Field(None, ge=1, le=3650)


def _out(ws: Workspace, role: str) -> dict:
    base = (instance_settings().public_base_url or "").rstrip("/")
    return {
        "id": ws.id,
        "name": ws.name,
        "slug": ws.slug,
        "role": role,
        "retention_days": ws.retention_days,
        # Paste into the Twilio number's "A call comes in" webhook (inbound agents).
        "inbound_webhook_url": f"{base}/voice/inbound/{ws.slug}" if base else None,
    }


@router.get("/workspaces")
async def list_workspaces(auth: Auth = Depends(current_auth), db: AsyncSession = Depends(get_db)) -> list[dict]:
    return [_out(ws, role) for ws, role in await repo.workspaces_for(db, auth.user)]


@router.post("/workspaces", status_code=201)
async def create_workspace(
    body: WorkspaceIn, auth: Auth = Depends(current_auth), db: AsyncSession = Depends(get_db)
) -> dict:
    ws = await repo.create_workspace(db, auth.user, body.name)
    await repo.audit(db, "workspace.create", user_id=auth.user.id, workspace_id=ws.id, target=ws.name)
    await db.commit()
    return _out(ws, "owner")


@router.get("/workspaces/{workspace_id}")
async def get_workspace(access: WorkspaceAccess = Depends(workspace_access)) -> dict:
    ws = await access.repo.db.get(Workspace, access.repo.workspace_id)
    return _out(ws, access.role)


@router.patch("/workspaces/{workspace_id}")
async def update_workspace(body: WorkspacePatch, access: WorkspaceAccess = Depends(workspace_access)) -> dict:
    access.require_write()
    db = access.repo.db
    ws = await db.get(Workspace, access.repo.workspace_id)
    if body.name is not None:
        ws.name = body.name.strip()
    if body.retention_days is not None:
        ws.retention_days = body.retention_days
    await repo.audit(db, "workspace.update", user_id=access.auth.user.id, workspace_id=ws.id)
    await db.commit()
    return _out(ws, access.role)
