"""Data access. Tenant-owned queries live on WorkspaceRepo, which is bound to one workspace id and
adds it to every query, so a handler cannot forget the tenant filter."""

from __future__ import annotations

import re
from typing import Optional, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import AgentConfig, Settings
from .crypto import SecretBox, last4
from .models import (
    Agent,
    AuditLog,
    AuthSession,
    Call,
    Membership,
    PhoneNumber,
    ProviderCredential,
    User,
    Workspace,
)

# Provider secrets a workspace can store, mapped to the call engine's Settings fields.
PROVIDER_KEYS = {
    "twilio_account_sid": "Twilio Account SID",
    "twilio_auth_token": "Twilio Auth Token",
    "twilio_from_number": "Twilio caller number (E.164)",
    "deepgram_api_key": "Deepgram API key",
    "elevenlabs_api_key": "ElevenLabs API key",
    "anthropic_api_key": "Anthropic API key",
    "openrouter_api_key": "OpenRouter API key",
    "calcom_api_key": "Cal.com API key",
}


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:48] or "workspace"


# -- instance-level (not tenant-scoped) -------------------------------------------------------


async def user_by_email(db: AsyncSession, email: str) -> Optional[User]:
    return await db.scalar(select(User).where(func.lower(User.email) == email.lower()))


async def count_users(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(User)) or 0)


async def unique_slug(db: AsyncSession, name: str) -> str:
    base = slugify(name)
    slug, n = base, 1
    while await db.scalar(select(Workspace.id).where(Workspace.slug == slug)):
        n += 1
        slug = f"{base}-{n}"
    return slug


async def create_workspace(db: AsyncSession, owner: User, name: str) -> Workspace:
    ws = Workspace(name=name.strip() or "My workspace", slug=await unique_slug(db, name))
    db.add(ws)
    await db.flush()
    db.add(Membership(workspace_id=ws.id, user_id=owner.id, role="owner"))
    await db.flush()
    return ws


async def workspaces_for(db: AsyncSession, user: User) -> Sequence[tuple[Workspace, str]]:
    rows = await db.execute(
        select(Workspace, Membership.role)
        .join(Membership, Membership.workspace_id == Workspace.id)
        .where(Membership.user_id == user.id)
        .order_by(Workspace.created_at)
    )
    return [(ws, role) for ws, role in rows.all()]


async def membership(db: AsyncSession, workspace_id: str, user_id: str) -> Optional[Membership]:
    return await db.scalar(
        select(Membership).where(Membership.workspace_id == workspace_id, Membership.user_id == user_id)
    )


async def session_by_id(db: AsyncSession, sid: str) -> Optional[AuthSession]:
    return await db.get(AuthSession, sid)


async def delete_sessions_for(db: AsyncSession, user_id: str) -> None:
    await db.execute(delete(AuthSession).where(AuthSession.user_id == user_id))


async def audit(db: AsyncSession, action: str, *, user_id=None, workspace_id=None, target: str = "") -> None:
    db.add(AuditLog(action=action, user_id=user_id, workspace_id=workspace_id, target=target[:200]))


# -- tenant-scoped ----------------------------------------------------------------------------


class WorkspaceRepo:
    def __init__(self, db: AsyncSession, workspace_id: str):
        self.db = db
        self.workspace_id = workspace_id

    # provider keys

    async def credentials(self) -> list[ProviderCredential]:
        rows = await self.db.scalars(
            select(ProviderCredential).where(ProviderCredential.workspace_id == self.workspace_id)
        )
        return list(rows)

    async def set_credential(self, box: SecretBox, name: str, value: str) -> ProviderCredential:
        if name not in PROVIDER_KEYS:
            raise KeyError(name)
        cred = await self.db.scalar(
            select(ProviderCredential).where(
                ProviderCredential.workspace_id == self.workspace_id, ProviderCredential.name == name
            )
        )
        if cred is None:
            cred = ProviderCredential(workspace_id=self.workspace_id, name=name, ciphertext="")
            self.db.add(cred)
        cred.ciphertext = box.encrypt(value)
        cred.last4 = last4(value)
        await self.db.flush()
        return cred

    async def delete_credential(self, name: str) -> bool:
        result = await self.db.execute(
            delete(ProviderCredential).where(
                ProviderCredential.workspace_id == self.workspace_id, ProviderCredential.name == name
            )
        )
        return bool(result.rowcount)

    async def provider_settings(self, box: SecretBox, public_base_url: Optional[str]) -> Settings:
        """Decrypt this workspace's keys into the call engine's Settings (BYO keys per call)."""

        values = {c.name: box.decrypt(c.ciphertext) for c in await self.credentials()}
        return Settings(public_base_url=public_base_url, **values)

    # agents

    async def agents(self) -> list[Agent]:
        rows = await self.db.scalars(
            select(Agent).where(Agent.workspace_id == self.workspace_id).order_by(Agent.created_at)
        )
        return list(rows)

    async def agent(self, agent_id: str) -> Optional[Agent]:
        return await self.db.scalar(
            select(Agent).where(Agent.workspace_id == self.workspace_id, Agent.id == agent_id)
        )

    async def create_agent(self, name: str, config: AgentConfig) -> Agent:
        agent = Agent(
            workspace_id=self.workspace_id,
            name=name,
            mode=config.mode,
            config=config.model_dump(mode="json"),
        )
        self.db.add(agent)
        await self.db.flush()
        return agent

    async def delete_agent(self, agent_id: str) -> bool:
        result = await self.db.execute(
            delete(Agent).where(Agent.workspace_id == self.workspace_id, Agent.id == agent_id)
        )
        return bool(result.rowcount)

    # phone numbers

    async def phone_numbers(self) -> list[PhoneNumber]:
        rows = await self.db.scalars(
            select(PhoneNumber).where(PhoneNumber.workspace_id == self.workspace_id)
        )
        return list(rows)

    # calls

    async def calls(self, limit: int = 50, offset: int = 0) -> list[Call]:
        rows = await self.db.scalars(
            select(Call)
            .where(Call.workspace_id == self.workspace_id)
            .order_by(Call.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(rows)

    async def call(self, call_id: str) -> Optional[Call]:
        return await self.db.scalar(
            select(Call).where(Call.workspace_id == self.workspace_id, Call.id == call_id)
        )

    async def delete_call(self, call_id: str) -> bool:
        result = await self.db.execute(
            delete(Call).where(Call.workspace_id == self.workspace_id, Call.id == call_id)
        )
        return bool(result.rowcount)
