"""Run Alembic programmatically (no alembic.ini needed): `voiceagent db-upgrade`."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from alembic import command
from alembic.config import Config

_DIR = Path(__file__).resolve().parent / "migrations"


def alembic_config(url: Optional[str] = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_DIR))
    if url:
        cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def upgrade(url: Optional[str] = None, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def revision(message: str, url: Optional[str] = None) -> None:
    command.revision(alembic_config(url), message=message, autogenerate=True, rev_id=None)
