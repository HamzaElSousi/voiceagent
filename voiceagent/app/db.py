"""Async SQLAlchemy engine and sessions. Postgres (asyncpg) in production, SQLite (aiosqlite) for
local dev and tests. Every call-path rule still applies: the live call never waits on the DB; the
API and the worker do."""

from __future__ import annotations

from typing import AsyncIterator, Optional

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, url: str):
        kwargs = {}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        else:
            kwargs.update(pool_pre_ping=True, pool_size=5, max_overflow=5)
        self.engine: AsyncEngine = create_async_engine(url, **kwargs)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_all(self) -> None:
        """Create tables directly (tests, SQLite dev). Production uses Alembic migrations."""

        from . import models  # noqa: F401  (registers the tables)

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as s:
            yield s

    async def dispose(self) -> None:
        await self.engine.dispose()


_db: Optional[Database] = None


def get_database(url: Optional[str] = None) -> Database:
    global _db
    if _db is None:
        from .settings import instance_settings

        _db = Database(url or instance_settings().database_url)
    return _db


def set_database(db: Optional[Database]) -> None:
    """Swap the process-wide database (tests)."""

    global _db
    _db = db
