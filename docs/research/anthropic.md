# Anthropic Streaming Messages API: Voice Agent Cheat-Sheet

Implementation reference for a real-time voice agent optimized for lowest time-to-first-token (TTFT). Source: the `claude-api` skill (canonical for model ids, pricing, streaming, usage).

## 1. Model ids (configurable)

- **Default (Haiku):** `claude-haiku-4-5` (full dated id: `claude-haiku-4-5-20251001`). Fastest, cheapest tier; 200K context, 64K max output. Use the alias unless you need to pin the snapshot.
- **Swap to Sonnet via config:** `claude-sonnet-4-6` (more capable, higher latency/cost).
- Keep the model in config (env var or settings), defaulting to `claude-haiku-4-5`. Both ids are valid as written, no extra date suffix needed on the alias.

```python
MODEL = os.getenv("LLM_MODEL", "claude-haiku-4-5")  # set to claude-sonnet-4-6 to upgrade
```

## 2. Async streaming pattern (official `anthropic` SDK)

Use `AsyncAnthropic`, open `client.messages.stream(...)` as an async context manager, and iterate `stream.text_stream` for text deltas. Yield each delta promptly so TTS can start as soon as the first words arrive.

```python
from anthropic import AsyncAnthropic

client = AsyncAnthropic()  # reads ANTHROPIC_API_KEY from env

async def stream_reply(system, messages, model="claude-haiku-4-5"):
    async with client.messages.stream(
        model=model,
        system=system,            # persona / instructions (see #3)
        messages=messages,        # role/content history (see #3)
        max_tokens=150,           # short answers for voice (see #7)
        temperature=0.7,
    ) as stream:
        async for delta in stream.text_stream:   # text chunks, not events
            yield delta
        final = await stream.get_final_message()  # usage + full message (see #4)
        record_usage(final.usage)
```

Notes: `text_stream` yields only the text deltas (no event parsing). If you need raw events, iterate `stream` directly and branch on `event.type == "content_block_delta"`. `get_final_message()` works even while streaming and gives timeout protection plus the usage block.

## 3. Passing persona, history, and params

- **`system`** (top-level string): the persona / instructions. Not a message; keep it stable so prompt caching can kick in.
- **`messages`** (list): conversation history, each `{"role": "user"|"assistant", "content": "..."}`. First message must be `user`; roles alternate (consecutive same-role entries are merged by the API).
- **`max_tokens`** (required): hard output cap. **`temperature`** (0.0 to 1.0): sampling. **`model`**: from config.

```python
system = "You are a friendly phone assistant. Keep replies to one or two short sentences."
messages = [
    {"role": "user", "content": "Hi, are you open on Sundays?"},
    {"role": "assistant", "content": "We are, from 9 to 5."},
    {"role": "user", "content": "Great, and on holidays?"},
]
```

## 4. Token usage (cost meter)

After streaming completes, read usage from the final message:

```python
final = await stream.get_final_message()
in_tok  = final.usage.input_tokens          # uncached input tokens (full price)
out_tok = final.usage.output_tokens
# prompt caching, if used:
cache_read   = final.usage.cache_read_input_tokens      # ~0.1x input price
cache_write  = final.usage.cache_creation_input_tokens  # ~1.25x input price
```

Accumulate `input_tokens` and `output_tokens` per turn and multiply by the per-Mtok rates below. (Total prompt size = `input_tokens + cache_read + cache_write` if caching is on.)

## 5. Haiku pricing (calibrate the cost meter)

`claude-haiku-4-5`: **$1.00 per 1M input tokens**, **$5.00 per 1M output tokens**.

```python
cost = in_tok / 1_000_000 * 1.00 + out_tok / 1_000_000 * 5.00
```

(For reference, Sonnet `claude-sonnet-4-6` is $3.00 input / $15.00 output per Mtok.)

## 6. Latency / TTFT guidance

- Haiku is the fastest tier: expect noticeably lower TTFT than Sonnet (Haiku is the model to reach for on speed-critical paths; Sonnet trades latency for capability). Default to Haiku for voice.
- Levers that reduce TTFT:
  - **Stream** (already doing so): you get the first token instead of waiting for the whole response.
  - **Smaller `system` + shorter history**: less input to process before generation starts.
  - **Prompt caching** on the stable `system` prefix: repeat calls skip re-processing the persona, cutting TTFT. Pre-warm the cache at startup with a `max_tokens=0` request so the first real turn is warm.
  - **Do NOT enable `thinking`** for voice. Haiku 4.5 does not use the adaptive-thinking path of larger models; keep requests plain so output starts immediately.
  - Keep `max_tokens` modest (caps worst-case generation, not TTFT directly, but bounds total turn time).

## 7. Voice-specific: keep it short, stream for sentence-level TTS

- **Cap length two ways:** set a low `max_tokens` (e.g. 100 to 150) AND instruct brevity in `system` ("Reply in one or two short sentences, no lists.").
- **Sentence handoff to TTS:** the LLM streams token deltas; the **TTS module owns sentence splitting**. The LLM side just needs to forward `text_stream` deltas promptly (flush per delta, do not buffer the whole reply). The TTS layer accumulates deltas and cuts on sentence boundaries (`.`, `!`, `?`, newline) so it can begin synthesizing the first sentence while later ones are still streaming.

## 8. Cancellation (barge-in)

The async stream is cancelled cooperatively: cancel the asyncio task that is consuming `text_stream`, or `break` out of the `async with` block. Exiting the context manager closes the underlying HTTP connection and stops generation.

```python
task = asyncio.create_task(consume(stream_reply(system, messages)))
# barge-in detected (user started speaking):
task.cancel()
try:
    await task
except asyncio.CancelledError:
    pass
```

Alternatively, inside the consumer, `break` on a barge-in flag:

```python
async with client.messages.stream(...) as stream:
    async for delta in stream.text_stream:
        if barge_in.is_set():
            break          # leaving the context manager aborts the request
        await tts.feed(delta)
```

Either approach stops both the LLM stream and downstream TTS at the next delta boundary, giving responsive barge-in.
