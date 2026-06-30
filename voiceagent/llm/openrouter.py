"""OpenRouter streaming LLM client for VoiceAgent.

Conforms to the LLM Protocol in voiceagent.contracts. OpenRouter exposes an OpenAI-compatible
`/chat/completions` endpoint, so this streams Server-Sent Events with httpx (no extra SDK). The
HTTP client is created lazily inside stream(), so importing this module and constructing an
OpenRouterLLM offline never touch the network. Selected via `llm.provider: openrouter` in
agent.yaml; the model id is any OpenRouter model (e.g. "openai/gpt-4o-mini",
"anthropic/claude-3.5-haiku", "google/gemini-flash-1.5").

Cancellation is cooperative: cancel the asyncio task consuming stream() or break out of the async
for loop; the `async with client.stream(...)` context closes the connection on the way out.
"""

from __future__ import annotations

import json
from typing import AsyncIterator, Mapping, Sequence

from voiceagent.contracts import TokenUsage

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterLLM:
    def __init__(
        self,
        api_key: str,
        model: str = "openai/gpt-4o-mini",
        max_tokens: int = 150,
        temperature: float = 0.6,
        base_url: str = _OPENROUTER_BASE_URL,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._last_usage: TokenUsage = TokenUsage()

    def _build_messages(
        self, messages: Sequence[Mapping[str, str]], system: str
    ) -> list[dict[str, str]]:
        """Map the engine's message history to OpenAI-style messages, with the persona as a
        leading system message (Claude takes `system` separately; OpenAI-style inlines it)."""

        out: list[dict[str, str]] = []
        if system:
            out.append({"role": "system", "content": system})
        for m in messages:
            out.append({"role": str(m.get("role", "user")), "content": str(m.get("content", ""))})
        return out

    async def stream(
        self,
        messages: Sequence[Mapping[str, str]],
        system: str = "",
    ) -> AsyncIterator[str]:  # type: ignore[override]  # async generator satisfies protocol
        """Yield response text deltas. Usage is captured from the final SSE chunk when present."""

        import httpx  # deferred import: module import stays network-free and dependency-light

        payload = {
            "model": self._model,
            "messages": self._build_messages(messages, system),
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self._base_url}/chat/completions"

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    delta = self._parse_sse_line(line)
                    if delta:
                        yield delta

    def _parse_sse_line(self, line: str) -> str:
        """Parse one SSE line. Returns a text delta (possibly empty). Records usage as a side
        effect when a chunk carries it. Static so the parse is unit-testable without network."""

        if not line or not line.startswith("data:"):
            return ""
        data = line[len("data:"):].strip()
        if not data or data == "[DONE]":
            return ""
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            return ""
        usage = chunk.get("usage")
        if usage:
            self._last_usage = TokenUsage(
                input_tokens=int(usage.get("prompt_tokens", 0) or 0),
                output_tokens=int(usage.get("completion_tokens", 0) or 0),
            )
        choices = chunk.get("choices") or []
        if choices:
            return choices[0].get("delta", {}).get("content") or ""
        return ""

    def last_usage(self) -> TokenUsage:
        return self._last_usage

    @property
    def model(self) -> str:
        return self._model
