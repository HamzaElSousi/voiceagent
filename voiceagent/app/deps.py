"""FastAPI dependencies: DB session, the logged-in user, CSRF, and workspace access."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from typing import AsyncIterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from . import repo
from .crypto import SecretBox
from .db import get_database
from .models import AuthSession, User, utcnow
from .security import CSRF_HEADER, SESSION_COOKIE, token_id, tokens_match
from .settings import instance_settings

_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_WRITE_ROLES = {"owner", "admin"}


async def get_db() -> AsyncIterator[AsyncSession]:
    async with get_database().sessionmaker() as session:
        yield session


def secret_box() -> SecretBox:
    return SecretBox(instance_settings().encryption_key)


@dataclass
class Auth:
    user: User
    session: AuthSession


async def current_auth(request: Request, db: AsyncSession = Depends(get_db)) -> Auth:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        raise HTTPException(401, "not signed in")
    sess = await repo.session_by_id(db, token_id(token))
    expires = sess.expires_at if sess else None
    if expires is not None and expires.tzinfo is None:  # SQLite drops tzinfo
        expires = expires.replace(tzinfo=timezone.utc)
    if sess is None or expires is None or expires < utcnow():
        raise HTTPException(401, "session expired")
    user = await db.get(User, sess.user_id)
    if user is None or not user.is_active:
        raise HTTPException(401, "not signed in")
    if request.method in _UNSAFE_METHODS and not tokens_match(
        request.headers.get(CSRF_HEADER, ""), sess.csrf_token
    ):
        raise HTTPException(403, "missing or invalid CSRF token")
    return Auth(user=user, session=sess)


@dataclass
class WorkspaceAccess:
    auth: Auth
    repo: repo.WorkspaceRepo
    role: str

    @property
    def can_write(self) -> bool:
        return self.role in _WRITE_ROLES

    def require_write(self) -> None:
        if not self.can_write:
            raise HTTPException(403, "your role cannot change this workspace")


async def workspace_access(
    workspace_id: str, auth: Auth = Depends(current_auth), db: AsyncSession = Depends(get_db)
) -> WorkspaceAccess:
    m = await repo.membership(db, workspace_id, auth.user.id)
    if m is None:
        # 404, not 403: do not reveal whether another tenant's workspace exists.
        raise HTTPException(404, "workspace not found")
    return WorkspaceAccess(auth=auth, repo=repo.WorkspaceRepo(db, workspace_id), role=m.role)
