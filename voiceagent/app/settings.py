"""Instance settings for a self-hosted deployment, read from the environment (.env).

These belong to the instance operator, not to a workspace. Workspace provider keys (Twilio,
Deepgram, ...) are stored encrypted in the database and entered through the dashboard.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional


def _bool(value: Optional[str], default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class InstanceSettings:
    database_url: str
    secret_key: str
    encryption_key: Optional[str]
    allow_signups: bool
    public_base_url: Optional[str]
    cookie_secure: bool
    session_days: int = 14

    @property
    def dev_mode(self) -> bool:
        return not self.cookie_secure


def load_instance_settings() -> InstanceSettings:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    public = os.getenv("PUBLIC_BASE_URL") or None
    return InstanceSettings(
        database_url=os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./voiceagent.db"),
        # Only used to sign non-secret tokens; sessions are random and stored hashed.
        secret_key=os.getenv("SECRET_KEY", "dev-insecure-secret-key"),
        encryption_key=os.getenv("ENCRYPTION_KEY") or None,
        allow_signups=_bool(os.getenv("ALLOW_SIGNUPS"), default=False),
        public_base_url=public,
        # Secure cookies whenever the instance is served over HTTPS (always, behind Caddy).
        cookie_secure=_bool(os.getenv("COOKIE_SECURE"), default=bool(public and public.startswith("https"))),
        session_days=int(os.getenv("SESSION_DAYS", "14")),
    )


@lru_cache(maxsize=1)
def instance_settings() -> InstanceSettings:
    return load_instance_settings()
