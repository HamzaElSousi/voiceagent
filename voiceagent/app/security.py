"""Passwords, login sessions, CSRF and login rate limiting.

The API shares the event loop with live calls, so CPU-heavy work (argon2) always runs in a worker
thread: a 50ms hash on the loop is a 50ms stall in every live call's audio.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()

SESSION_COOKIE = "va_session"
CSRF_COOKIE = "va_csrf"
CSRF_HEADER = "X-CSRF-Token"
MIN_PASSWORD_LENGTH = 10


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hasher.hash, password)


async def verify_password(password_hash: str, password: str) -> bool:
    def _verify() -> bool:
        try:
            return _hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    return await asyncio.to_thread(_verify)


# A real hash to verify against when the email is unknown, so "no such user" and "wrong password"
# take the same time (no account enumeration by timing).
_DUMMY_HASH = _hasher.hash("not-a-real-password-placeholder")


async def verify_dummy(password: str) -> None:
    await verify_password(_DUMMY_HASH, password)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_id(token: str) -> str:
    """What is stored for a session token: its SHA-256, never the token itself."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_match(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a, b)


class RateLimiter:
    """In-memory sliding-window limiter (per process). Enough for a single-node self-hosted
    instance; a multi-node deployment would move this to Postgres or Redis."""

    def __init__(self, max_events: int, window_seconds: float):
        self.max_events = max_events
        self.window = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str) -> bool:
        """Record an attempt. Returns False when the key is over its limit."""

        now = time.monotonic()
        q = self._events[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.max_events:
            return False
        q.append(now)
        return True

    def reset(self, key: str) -> None:
        self._events.pop(key, None)


login_limiter = RateLimiter(max_events=10, window_seconds=15 * 60)
