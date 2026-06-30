# Deepgram Streaming STT Cheat-Sheet (Twilio mu-law -> Python)

Implementation-ready notes for `stt/deepgram.py`. Source: developers.deepgram.com (live streaming reference).

## 1. Endpoint and Auth

- **Endpoint:** `wss://api.deepgram.com/v1/listen`
- **API key:** passed via the `Authorization` header (NOT a query param):
  `Authorization: Token YOUR_DEEPGRAM_API_KEY`
  (note the literal word `Token`, not `Bearer`).
- In browser-only environments where custom headers are blocked, the key can instead ride in the `Sec-WebSocket-Protocol` header (`["token", "YOUR_KEY"]`). For a Python server, always use the `Authorization` header.

```python
import websockets
ws = await websockets.connect(url, additional_headers={"Authorization": f"Token {key}"})
```

## 2. Query Parameters for Twilio Audio

Twilio Media Streams send 8 kHz mono mu-law (G.711 u-law). Tell Deepgram to accept the raw bytes directly (no decoding needed):

| Param | Value | Notes |
|-------|-------|-------|
| `encoding` | `mulaw` | matches G.711 u-law from Twilio |
| `sample_rate` | `8000` | required when encoding is set |
| `channels` | `1` | mono |
| `model` | `nova-3` (or `nova-2`) | both low-latency; nova-3 best multilingual/noisy, nova-2 cheaper English. `nova-2-phonecall` is tuned for telephony. |
| `language` | `en` (or `multi` for nova-3) | |
| `punctuate` | `true` | |
| `interim_results` | `true` | required if you use `utterance_end_ms` |
| `smart_format` | `true` | formats numbers, dates, etc. |
| `endpointing` | `300` | ms of silence to mark end of speech (default 10) |
| `utterance_end_ms` | `1000` | gap after last word to emit UtteranceEnd |
| `vad_events` | `true` | enables SpeechStarted events |

Example query string:

```
wss://api.deepgram.com/v1/listen?encoding=mulaw&sample_rate=8000&channels=1&model=nova-3&language=en&punctuate=true&interim_results=true&smart_format=true&endpointing=300&utterance_end_ms=1000&vad_events=true
```

## 3. Endpointing / VAD and Turn Detection

Three distinct signals, do not confuse them:

- **`is_final: true`** on a Results message: that transcript segment is finalized (max accuracy reached, text will not change). Does NOT mean the speaker stopped, it just flushes a stable chunk.
- **`speech_final: true`** on a Results message: the `endpointing` VAD detected a real pause, so this finalized segment is the end of an utterance. This is your main "caller finished talking" trigger in quiet audio.
- **`UtteranceEnd` message:** emitted by the `utterance_end_ms` feature, which watches word timing gaps (not raw silence). Robust in noisy telephony where the VAD may miss the pause.

**Recommended turn-detection logic for the agent:**
1. Accumulate finalized text from `is_final: true` segments.
2. Fire the turn on the first of: a segment with `speech_final: true`, OR an `UtteranceEnd` message arriving without a preceding `speech_final` (noisy-audio fallback).
3. Use `SpeechStarted` (needs `vad_events=true`) to detect barge-in and interrupt TTS playback.

`endpointing` and `utterance_end_ms` operate independently and can be used together.

## 4. Inbound Message Shapes

Transcript result (`type: "Results"`):

```json
{
  "type": "Results",
  "channel_index": [0],
  "is_final": true,
  "speech_final": false,
  "channel": {
    "alternatives": [
      { "transcript": "hello world", "confidence": 0.95, "words": [ ... ] }
    ]
  }
}
```

Read text at `channel.alternatives[0].transcript`. Flags `is_final` and `speech_final` are top-level.

UtteranceEnd event (`last_word_end` is the time speech ended):

```json
{ "type": "UtteranceEnd", "channel": [0], "last_word_end": 3.1 }
```

SpeechStarted event:

```json
{ "type": "SpeechStarted", "channel": [0], "timestamp": 0.1 }
```

Ignore empty interim transcripts (`transcript == ""`).

## 5. Sending Audio and Closing

- **Send audio:** write the raw mu-law payload as **binary** WebSocket frames. With Twilio, base64-decode each media `payload` and send the bytes straight through (no transcoding, since `encoding=mulaw&sample_rate=8000` matches Twilio exactly).
- **Flush:** send `{"type": "Finalize"}` (text frame) to force Deepgram to return any buffered transcript immediately, without closing.
- **Close gracefully:** send `{"type": "CloseStream"}` (text frame). Deepgram processes remaining audio, returns final results, then closes. Preferred over abruptly dropping the socket.

## 6. KeepAlive

- Message: `{"type": "KeepAlive"}` sent as a **text** frame (binary will be misread).
- Idle connection closes after **10 seconds** of no audio/messages with error **NET-0001**.
- Send a KeepAlive every **3 to 5 seconds** while no audio is flowing (e.g. caller on hold / silence). Server sends no response to it.
- KeepAlive alone is not enough at startup: you must stream at least one audio message within 10 s of connecting.

## 7. Latency and mu-law Gotchas

- Streaming transcript latency is typically sub-300 ms for interim results; end-of-utterance latency is dominated by your `endpointing` value (e.g. 300 ms) plus `utterance_end_ms` (1000 ms).
- Mu-law 8 kHz is narrowband telephony, so expect lower accuracy than wideband; prefer telephony-tuned models (`nova-2-phonecall`) or `nova-3`.
- `encoding` and `sample_rate` MUST be set together and match the stream, otherwise you get garbage or DATA errors.
- Twilio frames are ~20 ms (160 bytes) each; you can forward them as-is, no need to batch.
- Lower `endpointing` (e.g. 150 to 300 ms) cuts response latency but risks cutting off slow speakers; tune for your callers.

## 8. Pricing (calibrate the cost meter)

- **Nova-3 streaming:** approx **$0.0077 / min** (~$0.46 / hour) pay-as-you-go.
- **Nova-2 streaming:** lower (~$0.0059 / min range); nova-2 is the cost-driven English option.
- Billed per second of audio processed (61 s billed as 61 s, not rounded to 2 min).

## Sources

- https://developers.deepgram.com/reference/speech-to-text/listen-streaming
- https://developers.deepgram.com/docs/understanding-end-of-speech-detection
- https://developers.deepgram.com/docs/utterance-end
- https://developers.deepgram.com/docs/endpointing
- https://developers.deepgram.com/docs/audio-keep-alive
- https://deepgram.com/pricing
