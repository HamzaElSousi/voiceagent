"""Server-side request forgery guard for user-supplied connector URLs.

Workspaces configure URLs the server will call. On a shared instance that must not become a way to
reach the host's private network (cloud metadata at 169.254.169.254, the Postgres container, ...).
Private targets are refused unless the instance operator opts in (ALLOW_PRIVATE_CONNECTOR_URLS),
e.g. for a CRM running on the same box. DNS is resolved asynchronously (never on the loop).
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from urllib.parse import urlparse

from .base import ConnectorError


def private_urls_allowed() -> bool:
    return os.getenv("ALLOW_PRIVATE_CONNECTOR_URLS", "").strip().lower() in ("1", "true", "yes")


def _is_private(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return (
        addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
        or addr.is_multicast or addr.is_unspecified
    )


async def check_url(url: str, *, allow_private: bool | None = None) -> None:
    allow = private_urls_allowed() if allow_private is None else allow_private
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise ConnectorError("URL must be an http(s) URL with a host")
    if parsed.scheme == "http" and not allow:
        raise ConnectorError("URL must use https")
    if allow:
        return
    host = parsed.hostname
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ConnectorError(f"cannot resolve host {host!r}") from exc
    for info in infos:
        if _is_private(info[4][0]):
            raise ConnectorError(
                f"{host!r} resolves to a private address; the instance admin can allow this with "
                "ALLOW_PRIVATE_CONNECTOR_URLS=true"
            )
