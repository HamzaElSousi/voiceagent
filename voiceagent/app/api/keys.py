"""Bring-your-own provider keys. Write-only: values are encrypted on the way in and the API only
ever returns whether a key is set plus its last 4 characters."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from .. import repo
from ..crypto import SecretBox, mask
from ..deps import WorkspaceAccess, secret_box, workspace_access

router = APIRouter(tags=["keys"])


class KeyIn(BaseModel):
    value: str = Field(min_length=1, max_length=4096)


@router.get("/workspaces/{workspace_id}/keys")
async def list_keys(access: WorkspaceAccess = Depends(workspace_access)) -> list[dict]:
    stored = {c.name: c for c in await access.repo.credentials()}
    return [
        {
            "name": name,
            "label": label,
            "is_set": name in stored,
            "masked": mask(stored[name].last4) if name in stored else None,
            "updated_at": stored[name].updated_at.isoformat() if name in stored else None,
        }
        for name, label in repo.PROVIDER_KEYS.items()
    ]


@router.put("/workspaces/{workspace_id}/keys/{name}")
async def set_key(
    name: str,
    body: KeyIn,
    access: WorkspaceAccess = Depends(workspace_access),
    box: SecretBox = Depends(secret_box),
) -> dict:
    access.require_write()
    if name not in repo.PROVIDER_KEYS:
        raise HTTPException(404, f"unknown key {name!r}")
    cred = await access.repo.set_credential(box, name, body.value.strip())
    await repo.audit(
        access.repo.db, "key.set", user_id=access.auth.user.id, workspace_id=access.repo.workspace_id, target=name
    )
    await access.repo.db.commit()
    return {"name": name, "is_set": True, "masked": mask(cred.last4)}


@router.delete("/workspaces/{workspace_id}/keys/{name}", status_code=204)
async def delete_key(name: str, access: WorkspaceAccess = Depends(workspace_access)) -> Response:
    access.require_write()
    if not await access.repo.delete_credential(name):
        raise HTTPException(404, "key not set")
    await repo.audit(
        access.repo.db, "key.delete", user_id=access.auth.user.id, workspace_id=access.repo.workspace_id, target=name
    )
    await access.repo.db.commit()
    return Response(status_code=204)
