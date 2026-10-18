"""OpenRouterLLM streaming, with no network. The SSE parse is unit-tested directly, and a fake
httpx client verifies the full stream() path yields deltas and records usage."""

from __future__ import annotations

import json

from voiceagent.contracts import LLM, TokenUsage
from voiceagent.llm.openrouter import OpenRouterLLM


def test_conforms_to_protocol_and_is_offline():
    llm = OpenRouterLLM("key", model="openai/gpt-4o-mini")
    assert isinstance(llm, LLM)
    assert llm.model == "openai/gpt-4o-mini"
    assert llm.last_usage() == TokenUsage()


def test_sse_line_parse():
    llm = OpenRouterLLM("key")
    # Content delta.
    line = "data: " + json.dumps({"choices": [{"delta": {"content": "Hello"}}]})
    assert llm._parse_sse_line(line) == "Hello"
    # Keepalive / non-data / DONE / empty choices.
    assert llm._parse_sse_line(": ping") == ""
    assert llm._parse_sse_line("data: [DONE]") == ""
    assert llm._parse_sse_line("") == ""
    # Usage chunk records usage and yields no text.
    usage_line = "data: " + json.dumps(
        {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 5}}
    )
    assert llm._parse_sse_line(usage_line) == ""
    assert llm.last_usage() == TokenUsage(input_tokens=12, output_tokens=5)


async def test_stream_yields_deltas_and_usage(monkeypatch):
    sse_lines = [
        "data: " + json.dumps({"choices": [{"delta": {"content": "Hi"}}]}),
        "data: " + json.dumps({"choices": [{"delta": {"content": " there"}}]}),
        "data: " + json.dumps(
            {"choices": [], "usage": {"prompt_tokens": 8, "completion_tokens": 2}}
        ),
        "data: [DONE]",
    ]

    class FakeResponse:
        def raise_for_status(self):
            pass

        async def aiter_lines(self):
            for line in sse_lines:
                yield line

    class FakeStreamCtx:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, *a):
            return False

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, *a, **k):
            return FakeStreamCtx()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    llm = OpenRouterLLM("key", model="anthropic/claude-3.5-haiku")
    out = []
    async for delta in llm.stream([{"role": "user", "content": "hi"}], system="be brief"):
        out.append(delta)
    assert "".join(out) == "Hi there"
    assert llm.last_usage() == TokenUsage(input_tokens=8, output_tokens=2)
