"""Signed per-call tokens. A platform call's media stream carries `call_id` + `token` as TwiML
<Parameter>s; the server only runs a session (and spends that workspace's provider credits) when the
token is a valid HMAC of the call id under the instance SECRET_KEY. This stops anyone who can reach
/stream from opening sessions on someone else's account."""

from __future__ import annotations

import hashlib
import hmac

from .settings import instance_settings


def sign_call(call_id: str, secret: str | None = None) -> str:
    key = (secret or instance_settings().secret_key).encode()
    return hmac.new(key, f"call:{call_id}".encode(), hashlib.sha256).hexdigest()


def verify_call(call_id: str, token: str, secret: str | None = None) -> bool:
    return bool(call_id and token) and hmac.compare_digest(sign_call(call_id, secret), token)
