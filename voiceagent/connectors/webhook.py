"""Webhook connector: the simplest way to plug in any system.

  GET  leads_url    -> JSON list of leads (or {"leads": [...], "next_cursor": "..."}), paged by
                       ?cursor=. Sent with "Authorization: Bearer <secret>".
  POST outcome_url  <- one JSON outcome per call, signed so the receiver can verify it:
       X-VoiceAgent-Timestamp: <unix seconds>
       X-VoiceAgent-Signature: sha256=<hex HMAC-SHA256(secret, "<timestamp>.<raw body>")>
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Optional

import httpx

from .base import ConnectorError, LeadRecord
from .mapping import extract_items, map_lead
from .urlsafety import check_url

SIGNATURE_HEADER = "X-VoiceAgent-Signature"
TIMESTAMP_HEADER = "X-VoiceAgent-Timestamp"
_MAX_PAGES = 50


def sign(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify(secret: str, timestamp: str, body: bytes, signature: str, max_age: int = 300) -> bool:
    """Receiver-side helper (documented for integrators, used in tests)."""

    try:
        if abs(time.time() - int(timestamp)) > max_age:
            return False
    except ValueError:
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), signature)


class WebhookConnector:
    def __init__(self, config: dict, secret: str = "", *, client: Optional[httpx.AsyncClient] = None,
                 allow_private: Optional[bool] = None):
        self.config = config
        self.secret = secret
        self._client = client
        self._allow_private = allow_private

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20.0, follow_redirects=False)
        return self._client

    async def fetch_leads(self) -> list[LeadRecord]:
        url = self.config.get("leads_url")
        if not url:
            return []
        await check_url(url, allow_private=self._allow_private)
        headers = {"Authorization": f"Bearer {self.secret}"} if self.secret else {}
        leads: list[LeadRecord] = []
        cursor = None
        for _ in range(_MAX_PAGES):
            try:
                resp = await self._http().get(url, params={"cursor": cursor} if cursor else None, headers=headers)
            except httpx.HTTPError as exc:
                raise ConnectorError(f"leads URL unreachable: {exc.__class__.__name__}") from exc
            if resp.status_code >= 400:
                raise ConnectorError(f"leads URL returned HTTP {resp.status_code}")
            try:
                data = resp.json()
            except ValueError as exc:
                raise ConnectorError("leads URL did not return JSON") from exc
            items = extract_items(data, self.config.get("items_path") or "$")
            leads += [r for r in (map_lead(i, self.config.get("field_map") or {}) for i in items) if r]
            cursor = data.get("next_cursor") if isinstance(data, dict) else None
            if not cursor:
                break
        return leads

    async def push_outcome(self, payload: dict) -> None:
        url = self.config.get("outcome_url")
        if not url:
            return
        await check_url(url, allow_private=self._allow_private)
        body = json.dumps(payload, separators=(",", ":"), default=str).encode()
        ts = str(int(time.time()))
        headers = {"Content-Type": "application/json", TIMESTAMP_HEADER: ts}
        if self.secret:
            headers[SIGNATURE_HEADER] = sign(self.secret, ts, body)
        try:
            resp = await self._http().post(url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"outcome URL unreachable: {exc.__class__.__name__}") from exc
        if resp.status_code >= 300:
            raise ConnectorError(f"outcome URL returned HTTP {resp.status_code}")

    async def describe(self) -> dict:
        sample = await self.fetch_leads() if self.config.get("leads_url") else []
        return {"leads_found": len(sample)}

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
