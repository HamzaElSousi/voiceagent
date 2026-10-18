"""No-network unit tests for ClaudeLLM.

The AsyncAnthropic client is replaced with a scripted fake before any stream()
call is made, so no API key or network is required. The fake replicates the
three surface points the implementation depends on:
  - client.messages.stream(**kwargs) returns an async context manager
  - stream.text_stream is an async iterable of str deltas
  - stream.get_final_message() returns an object with .usage.input_tokens /
    .usage.output_tokens
"""

from __future__ import annotations

from voiceagent.contracts import TokenUsage
from voiceagent.llm.claude import ClaudeLLM

# ---------------------------------------------------------------------------
# Scripted fakes
# ---------------------------------------------------------------------------


class _FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeFinalMessage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.usage = _FakeUsage(input_tokens, output_tokens)


class _FakeStream:
    """Async context manager that replays canned deltas and a fake final message."""

    def __init__(
        self, deltas: list[str], input_tokens: int, output_tokens: int
    ) -> None:
        self._deltas = deltas
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        # Track kwargs forwarded by the implementation.
        self.received_kwargs: dict = {}

    async def __aenter__(self) -> "_FakeStream":
        return self

    async def __aexit__(self, *args) -> bool:
        return False

    @property
    def text_stream(self):
        return self._iter_deltas()

    async def _iter_deltas(self):
        for delta in self._deltas:
            yield delta

    async def get_final_message(self) -> _FakeFinalMessage:
        return _FakeFinalMessage(self._input_tokens, self._output_tokens)


class _FakeMessages:
    """Replaces client.messages; records the kwargs stream() is called with."""

    def __init__(
        self, deltas: list[str], input_tokens: int, output_tokens: int
    ) -> None:
        self._deltas = deltas
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self.last_kwargs: dict = {}

    def stream(self, **kwargs) -> _FakeStream:
        self.last_kwargs = kwargs
        return _FakeStream(self._deltas, self._input_tokens, self._output_tokens)


class _FakeClient:
    def __init__(
        self, deltas: list[str], input_tokens: int, output_tokens: int
    ) -> None:
        self.messages = _FakeMessages(deltas, input_tokens, output_tokens)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_llm(
    deltas: list[str],
    input_tokens: int,
    output_tokens: int,
    model: str = "claude-haiku-4-5",
) -> ClaudeLLM:
    """Build a ClaudeLLM with a pre-installed fake client; no network contact."""
    llm = ClaudeLLM(api_key="test-key-no-network", model=model)
    llm._client = _FakeClient(deltas, input_tokens, output_tokens)
    return llm


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_stream_yields_all_deltas():
    """Collected deltas concatenate to the full expected reply."""
    deltas = ["Hello", ",", " world", "!"]
    llm = _make_llm(deltas, input_tokens=10, output_tokens=4)

    collected: list[str] = []
    async for delta in llm.stream([{"role": "user", "content": "Hi"}]):
        collected.append(delta)

    assert "".join(collected) == "Hello, world!"


async def test_last_usage_reflects_fake_values():
    """last_usage() returns the token counts from the fake final message."""
    llm = _make_llm(["ok"], input_tokens=42, output_tokens=7)

    async for _ in llm.stream([{"role": "user", "content": "test"}]):
        pass

    usage = llm.last_usage()
    assert isinstance(usage, TokenUsage)
    assert usage.input_tokens == 42
    assert usage.output_tokens == 7


async def test_usage_accumulates_most_recent_call():
    """A second stream() call replaces usage from the first."""
    llm = _make_llm(["first"], input_tokens=5, output_tokens=1)
    async for _ in llm.stream([{"role": "user", "content": "a"}]):
        pass

    # Replace the fake client with different token counts.
    llm._client = _FakeClient(["second"], input_tokens=99, output_tokens=3)
    async for _ in llm.stream([{"role": "user", "content": "b"}]):
        pass

    usage = llm.last_usage()
    assert usage.input_tokens == 99
    assert usage.output_tokens == 3


def test_last_usage_zeros_before_any_call():
    """last_usage() returns zeros on a fresh instance before stream() is called."""
    llm = ClaudeLLM(api_key="test-key-no-network")
    usage = llm.last_usage()
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0


def test_model_property_default():
    """Default model id matches the contract-specified default."""
    llm = ClaudeLLM(api_key="test-key-no-network")
    assert llm.model == "claude-haiku-4-5"


def test_model_property_custom():
    """Passing a different model id is reflected in the property."""
    llm = ClaudeLLM(api_key="test-key-no-network", model="claude-sonnet-4-6")
    assert llm.model == "claude-sonnet-4-6"


async def test_system_string_forwarded():
    """The system parameter reaches the underlying stream() call."""
    llm = _make_llm(["reply"], input_tokens=1, output_tokens=1)
    fake_messages = llm._client.messages  # type: ignore[union-attr]

    async for _ in llm.stream(
        [{"role": "user", "content": "x"}], system="Be concise."
    ):
        pass

    assert fake_messages.last_kwargs.get("system") == "Be concise."


async def test_model_forwarded_to_api():
    """The model id is passed to the underlying Anthropic API call."""
    llm = _make_llm(["hi"], input_tokens=1, output_tokens=1, model="claude-sonnet-4-6")
    fake_messages = llm._client.messages  # type: ignore[union-attr]

    async for _ in llm.stream([{"role": "user", "content": "ping"}]):
        pass

    assert fake_messages.last_kwargs.get("model") == "claude-sonnet-4-6"


async def test_early_break_does_not_update_usage():
    """Breaking out of the stream mid-way leaves last_usage at the previous value."""
    llm = _make_llm(["a", "b", "c", "d"], input_tokens=50, output_tokens=10)

    # Consume only the first delta then break (simulates barge-in).
    async for delta in llm.stream([{"role": "user", "content": "go"}]):
        break  # exits the context manager, stops generation

    # Usage should still be zeros because the stream did not complete normally.
    usage = llm.last_usage()
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0


def test_no_network_on_construction():
    """ClaudeLLM can be constructed without triggering any network I/O.

    The _client attribute is None after construction, proving AsyncAnthropic
    has not been instantiated yet.
    """
    llm = ClaudeLLM(api_key="irrelevant")
    assert llm._client is None
