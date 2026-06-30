# ElevenLabs Streaming TTS WebSocket Cheat-Sheet (for Twilio voice agent)

Implementation target: `tts/elevenlabs.py`, real-time, Twilio Media Streams (8 kHz mono mu-law).

## 1. Endpoint and auth

```
wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input?model_id=eleven_flash_v2_5&output_format=ulaw_8000
```

- `{voice_id}` is a path parameter.
- Query params: `model_id`, `output_format`, plus optional `language_code`, `auto_mode`, `inactivity_timeout` (default 20s), `enable_ssml_parsing`, `sync_alignment`.
- Auth (pick one): HTTP header `xi-api-key: <key>` on the WS handshake, OR put `"xi-api-key": "<key>"` inside the first (BOS) message. Header is cleaner for Python `websockets`.

## 2. Lowest-latency model

- Use `eleven_flash_v2_5` (~75 ms model latency, 32 languages). ElevenLabs recommends Flash over Turbo for all real-time use.
- `eleven_turbo_v2_5` is functionally equivalent but higher average latency (~250 to 300 ms); slightly better prosody/expressiveness.
- Tradeoff: Flash = fastest, lighter prosody; Turbo = a bit richer, slower. For phone barge-in, prefer Flash.

## 3. Output format for Twilio (no transcoding)

Request mu-law 8 kHz directly so the base64 audio drops straight into a Twilio Media Stream `media` payload:

- `ulaw_8000` <- use this (G.711 u-law, 8 kHz mono). Matches Twilio exactly.
- `alaw_8000` (G.711 A-law, telephony alt, EU/PSTN).
- `pcm_8000` (raw 16-bit PCM 8 kHz; would need u-law encoding for Twilio).

Other (non-telephony, would need transcoding): `mp3_44100_128` (default), `pcm_16000/22050/24000/44100`, `opus_48000_*`.

Twilio frames are 20 ms = 160 bytes of u-law at 8 kHz. Slice the decoded bytes into 160-byte chunks.

## 4. WebSocket message protocol

Send order: BOS once, then one or more text chunks, then EOS. All messages are JSON text frames.

BOS (beginning of stream, sent first):

```json
{
  "text": " ",
  "voice_settings": { "stability": 0.5, "similarity_boost": 0.75, "speed": 1 },
  "generation_config": { "chunk_length_schedule": [120, 160, 250, 290] },
  "xi-api-key": "YOUR_KEY"
}
```

(`xi-api-key` here is optional if you sent it as a handshake header. The leading `" "` text primes the stream.)

Text chunk (send as the LLM emits tokens; keep a trailing space):

```json
{ "text": "Hello there, how can I help? ", "try_trigger_generation": true }
```

- `try_trigger_generation`: ask the server to start generating now (best effort).
- `flush: true`: force-generate everything buffered now (use at end of a sentence to cut latency). Sending `{"text": "", "flush": true}` flushes without closing.

EOS (end of stream, empty text closes input and drains audio):

```json
{ "text": "" }
```

## 5. Receiving audio and streaming it out

Server pushes response frames:

```json
{
  "audio": "<base64 ulaw bytes>",
  "isFinal": null,
  "alignment": { "chars": ["H","i"], "charStartTimesMs": [0,80], "charDurationsMs": [80,75] },
  "normalizedAlignment": { "chars": ["H","i"], "charStartTimesMs": [0,80], "charDurationsMs": [80,75] }
}
```

- `audio` may be `null` on some frames; skip those.
- `isFinal: true` marks the last frame for this generation.
- Loop: `base64.b64decode(msg["audio"])` -> already mu-law 8 kHz -> chunk into 160-byte (20 ms) frames -> base64 re-encode each -> send as Twilio `{"event":"media","streamSid":..., "media":{"payload": <b64>}}`. Pace at ~20 ms per frame (or let Twilio buffer).
- `alignment` gives per-character timing (use for captions or interruption bookkeeping).

## 6. Barge-in / mid-stream cancellation

Single-context `stream-input` has no per-utterance cancel message. To stop an in-flight utterance when the caller speaks:

1. Stop forwarding ElevenLabs audio to Twilio immediately, and send Twilio a `clear` event to flush its outbound buffer.
2. Close the ElevenLabs WebSocket (client-side close) to abort generation.
3. Open a fresh `stream-input` socket for the next response.

For lower-overhead barge-in without reconnecting, use the multi-stream (`/multi-stream-input`) variant: stream text into one `context_id`, and on interruption send a `close_context` message for that context and start a new context on the same connection. Single-context is simpler; multi-context avoids reconnect cost.

## 7. Sentence-level streaming from the LLM

- Open the WS as soon as the turn starts (before the LLM finishes).
- Forward LLM output to the WS as it arrives, breaking on sentence boundaries; append a trailing space to each chunk.
- Send `flush: true` at the end of each complete sentence so audio for sentence 1 plays while sentence 2 is still generating.
- `chunk_length_schedule` (default `[120,160,250,290]`) sets how many characters buffer before the server auto-generates. Lower the first value (e.g. `[50, ...]`) to start audio sooner at a small quality cost; raise it for smoother prosody.
- Set `auto_mode=true` (query param) to let the server decide generation timing and ignore `chunk_length_schedule`/`try_trigger_generation`; good for token-by-token LLM streams.
- Send `{"text": ""}` only to end; to keep the socket alive during an LLM stall, send an empty-text keep-alive to reset the inactivity timeout.

## 8. Pricing (cost meter)

- Flash and Turbo API usage: about $0.05 per 1,000 characters (~$0.00005/char).
- Credit model: 1 credit per 2 characters on Flash/Turbo.
- Calibrate the meter on characters sent in `text` fields (count outgoing text, not audio bytes).

## Sources

- WebSocket reference: https://elevenlabs.io/docs/api-reference/text-to-speech/v-1-text-to-speech-voice-id-stream-input
- Models: https://elevenlabs.io/docs/overview/models
- Multi-context WebSocket: https://elevenlabs.io/docs/api-reference/multi-context-text-to-speech/v-1-text-to-speech-voice-id-multi-stream-input
- Pricing: https://elevenlabs.io/pricing/api
