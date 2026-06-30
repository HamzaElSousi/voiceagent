"""Anthropic Claude streaming LLM client for VoiceAgent.

Conforms to the LLM Protocol in voiceagent.contracts. The AsyncAnthropic client
is constructed lazily so the module can be imported with no API key present and
creating a ClaudeLLM instance offline does not touch the network.
"""

from __future__ import annotations

from typing import AsyncIterator, Mapping, Sequence

from voiceagent.contracts import TokenUsage


class ClaudeLLM:
    """Streaming LLM client backed by Anthropic Claude.

    Satisfies the voiceagent.contracts.LLM Protocol. Text deltas are yielded
    immediately so the TTS layer can begin synthesizing the first sentence while
    the LLM is still generating the rest.

    Cancellation is cooperative: cancel the asyncio task consuming stream() or
    break out of the async for loop. Either action exits the async with block,
    which closes the underlying HTTP connection and stops generation.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "claude-haiku-4-5",
        max_tokens: int = 150,
        temperature: float = 0.6,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        # Deferred to first stream() call; stays None until then so construction
        # is network-free even when the API key is a placeholder.
        self._client = None
        self._last_usage: TokenUsage = TokenUsage()

    def _ensure_client(self):
        """Return the AsyncAnthropic client, creating it on the first call."""
        if self._client is None:
            from anthropic import AsyncAnthropic  # deferred import keeps module import clean

            self._client = AsyncAnthropic(api_key=self._api_key)
        return self._client

    async def stream(
        self,
        messages: Sequence[Mapping[str, str]],
        system: str = "",
    ) -> AsyncIterator[str]:  # type: ignore[override]  # async generator satisfies protocol
        """Yield response text deltas for the given message history.

        Usage is stored only on clean completion. On GeneratorExit or
        CancelledError the exception propagates without being suppressed;
        the async context manager closes the HTTP connection on the way out.
        """
        client = self._ensure_client()
        final_message = None
        async with client.messages.stream(
            model=self._model,
            system=system,
            messages=list(messages),
            max_tokens=self._max_tokens,
            temperature=self._temperature,
        ) as s:
            async for delta in s.text_stream:
                yield delta
            final_message = await s.get_final_message()

        if final_message is not None:
            self._last_usage = TokenUsage(
                input_tokens=final_message.usage.input_tokens,
                output_tokens=final_message.usage.output_tokens,
            )

    def last_usage(self) -> TokenUsage:
        """Return token usage for the most recently completed stream.

        Returns a zero TokenUsage before any stream has completed.
        """
        return self._last_usage

    @property
    def model(self) -> str:
        """The model id in use (for logging and cost accounting)."""
        return self._model
