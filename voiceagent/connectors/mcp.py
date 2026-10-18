"""Generic MCP connector: use any MCP server (Streamable HTTP transport) as a CRM.

The workspace picks two tools from the server and maps fields:
  - lead tool:    called to fetch leads ready to be called (e.g. "leads_call_queue")
  - outcome tool: called once per finished call (e.g. "leads_log_call"), with arguments rendered
                  from a template such as {"lead_id": "{{lead.external_id}}", "outcome": "{{outcome}}"}

Works with stateless servers (no Mcp-Session-Id) and stateful ones (the id is echoed back), and
with both response styles the spec allows: a plain JSON body or a text/event-stream.
"""

from __future__ import annotations

import itertools
import json
from typing import Any, Optional

import httpx

from .base import ConnectorError, LeadRecord
from .mapping import extract_items, map_lead, render_args
from .urlsafety import check_url

PROTOCOL_VERSION = "2025-06-18"
_CLIENT_INFO = {"name": "voiceagent", "version": "0.1.0"}


class McpClient:
    def __init__(
        self,
        url: str,
        *,
        credential: str = "",
        auth_header: str = "Authorization",
        timeout: float = 20.0,
        client: Optional[httpx.AsyncClient] = None,
        allow_private: Optional[bool] = None,
    ):
        self.url = url
        self._credential = credential
        self._auth_header = auth_header
        self._timeout = timeout
        self._client = client
        self._allow_private = allow_private
        self._ids = itertools.count(1)
        self._session_id: Optional[str] = None
        self._initialized = False

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=False)
        return self._client

    def _headers(self) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._credential:
            if self._auth_header.lower() == "authorization":
                h["Authorization"] = f"Bearer {self._credential}"
            else:
                h[self._auth_header] = self._credential
        if self._initialized:
            h["MCP-Protocol-Version"] = PROTOCOL_VERSION
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    async def _post(self, message: dict) -> Optional[dict]:
        try:
            resp = await self._http().post(self.url, json=message, headers=self._headers())
        except httpx.HTTPError as exc:
            raise ConnectorError(f"MCP server unreachable: {exc.__class__.__name__}") from exc
        if resp.status_code in (401, 403):
            raise ConnectorError(f"MCP server rejected the credential ({resp.status_code})")
        if resp.status_code == 202 or "id" not in message:
            return None  # notification accepted
        if resp.status_code >= 400:
            raise ConnectorError(f"MCP server returned HTTP {resp.status_code}")
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self._session_id = sid
        payload = _parse_response(resp, message["id"])
        if "error" in payload:
            err = payload["error"] or {}
            raise ConnectorError(f"MCP error {err.get('code')}: {err.get('message', '')}"[:300])
        return payload.get("result") or {}

    async def _request(self, method: str, params: Optional[dict] = None) -> dict:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        if params is not None:
            msg["params"] = params
        return await self._post(msg) or {}

    async def initialize(self) -> dict:
        if self._initialized:
            return {}
        await check_url(self.url, allow_private=self._allow_private)
        result = await self._request(
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": _CLIENT_INFO},
        )
        self._initialized = True
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    async def list_tools(self) -> list[dict]:
        await self.initialize()
        tools: list[dict] = []
        cursor = None
        for _ in range(20):  # paging guard
            result = await self._request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: Optional[dict] = None) -> Any:
        """Call a tool and return its data: structuredContent when present, else the text content
        parsed as JSON when possible, else the raw text."""

        await self.initialize()
        result = await self._request("tools/call", {"name": name, "arguments": arguments or {}})
        texts = [c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text"]
        if result.get("isError"):
            raise ConnectorError(f"tool {name!r} failed: {' '.join(texts)[:300]}")
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        joined = "\n".join(texts).strip()
        try:
            return json.loads(joined)
        except ValueError:
            return joined

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()


def _parse_response(resp: httpx.Response, request_id: int) -> dict:
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        for block in resp.text.split("\n\n"):
            data = "\n".join(
                line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")
            )
            if not data:
                continue
            try:
                msg = json.loads(data)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id") == request_id:
                return msg
        raise ConnectorError("MCP stream ended without a response")
    try:
        msg = resp.json()
    except ValueError as exc:
        raise ConnectorError("MCP server returned a non-JSON response") from exc
    if isinstance(msg, list):  # batched
        msg = next((m for m in msg if isinstance(m, dict) and m.get("id") == request_id), {})
    if not isinstance(msg, dict):
        raise ConnectorError("MCP server returned an unexpected response")
    return msg


class McpConnector:
    """CrmConnector over an MCP server. `config` holds the non-secret settings:

        url, auth_header, lead_tool, lead_args, items_path, field_map, outcome_tool, outcome_args
    """

    def __init__(self, config: dict, credential: str = "", *, client: Optional[httpx.AsyncClient] = None,
                 allow_private: Optional[bool] = None):
        if not config.get("url"):
            raise ConnectorError("MCP connector needs a server URL")
        self.config = config
        self.mcp = McpClient(
            config["url"],
            credential=credential,
            auth_header=config.get("auth_header") or "Authorization",
            client=client,
            allow_private=allow_private,
        )

    async def fetch_leads(self) -> list[LeadRecord]:
        tool = self.config.get("lead_tool")
        if not tool:
            raise ConnectorError("pick the tool that returns leads (lead_tool)")
        data = await self.mcp.call_tool(tool, self.config.get("lead_args") or {})
        items = extract_items(data, self.config.get("items_path") or "$")
        return [r for r in (map_lead(i, self.config.get("field_map") or {}) for i in items) if r]

    async def push_outcome(self, payload: dict) -> None:
        tool = self.config.get("outcome_tool")
        if not tool:
            return  # outcomes not wanted for this connector
        args = render_args(self.config.get("outcome_args") or {"outcome": "{{outcome}}"}, payload)
        await self.mcp.call_tool(tool, args)

    async def describe(self) -> dict:
        info = await self.mcp.initialize()
        tools = await self.mcp.list_tools()
        return {
            "server": (info.get("serverInfo") or {}),
            "tools": [{"name": t.get("name"), "description": (t.get("description") or "")[:300]} for t in tools],
        }

    async def close(self) -> None:
        await self.mcp.close()
