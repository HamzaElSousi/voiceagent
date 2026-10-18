"""Agents: a validated AgentConfig (persona, script, booking, compliance) stored per workspace."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from ...config import AgentConfig, AgentConfigError
from .. import repo
from ..deps import WorkspaceAccess, workspace_access
from ..models import Agent
from ..templates import agent_template

router = APIRouter(tags=["agents"])


class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    config: dict[str, Any]


class AgentPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    config: Optional[dict[str, Any]] = None
    is_active: Optional[bool] = None


def _validate(raw: dict) -> AgentConfig:
    try:
        return AgentConfig.from_dict(raw)
    except AgentConfigError as exc:
        raise HTTPException(422, str(exc)) from exc


def _out(agent: Agent) -> dict:
    return {
        "id": agent.id,
        "name": agent.name,
        "mode": agent.mode,
        "is_active": agent.is_active,
        "config": agent.config,
        "updated_at": agent.updated_at.isoformat() if agent.updated_at else None,
    }


@router.get("/agent-template")
async def get_template(mode: str = "outbound") -> dict:
    if mode not in ("outbound", "inbound"):
        raise HTTPException(422, "mode must be outbound or inbound")
    return agent_template(mode)


@router.get("/workspaces/{workspace_id}/agents")
async def list_agents(access: WorkspaceAccess = Depends(workspace_access)) -> list[dict]:
    return [_out(a) for a in await access.repo.agents()]


@router.post("/workspaces/{workspace_id}/agents", status_code=201)
async def create_agent(body: AgentIn, access: WorkspaceAccess = Depends(workspace_access)) -> dict:
    access.require_write()
    agent = await access.repo.create_agent(body.name.strip(), _validate(body.config))
    await repo.audit(
        access.repo.db, "agent.create", user_id=access.auth.user.id, workspace_id=access.repo.workspace_id, target=agent.name
    )
    await access.repo.db.commit()
    return _out(agent)


@router.get("/workspaces/{workspace_id}/agents/{agent_id}")
async def get_agent(agent_id: str, access: WorkspaceAccess = Depends(workspace_access)) -> dict:
    agent = await access.repo.agent(agent_id)
    if agent is None:
        raise HTTPException(404, "agent not found")
    return _out(agent)


@router.patch("/workspaces/{workspace_id}/agents/{agent_id}")
async def update_agent(agent_id: str, body: AgentPatch, access: WorkspaceAccess = Depends(workspace_access)) -> dict:
    access.require_write()
    agent = await access.repo.agent(agent_id)
    if agent is None:
        raise HTTPException(404, "agent not found")
    if body.config is not None:
        cfg = _validate(body.config)
        agent.config = cfg.model_dump(mode="json")
        agent.mode = cfg.mode
    if body.name is not None:
        agent.name = body.name.strip()
    if body.is_active is not None:
        agent.is_active = body.is_active
    await repo.audit(
        access.repo.db, "agent.update", user_id=access.auth.user.id, workspace_id=access.repo.workspace_id, target=agent.name
    )
    await access.repo.db.commit()
    return _out(agent)


@router.delete("/workspaces/{workspace_id}/agents/{agent_id}", status_code=204)
async def delete_agent(agent_id: str, access: WorkspaceAccess = Depends(workspace_access)) -> Response:
    access.require_write()
    if not await access.repo.delete_agent(agent_id):
        raise HTTPException(404, "agent not found")
    await repo.audit(
        access.repo.db, "agent.delete", user_id=access.auth.user.id, workspace_id=access.repo.workspace_id, target=agent_id
    )
    await access.repo.db.commit()
    return Response(status_code=204)
