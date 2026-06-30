# Twilio Media Streams + Outbound Calls: Implementation Cheat-Sheet

Target: `telephony/twilio.py` (FastAPI WebSocket handler + REST call origination).
Audio is 8 kHz mono mu-law (G.711 u-law), base64-encoded, over a WSS WebSocket.

## 1. Outbound call origination (REST)

```python
from twilio.rest import Client
client = Client(account_sid, auth_token)
call = client.calls.create(
    to="+15558675310",          # callee (E.164)
    from_="+15551112222",       # your Twilio number (E.164)
    twiml=twiml_string,         # inline TwiML, OR url="https://.../answer"
)
```

Use either `twiml` (inline XML string) or `url` (Twilio GETs/POSTs TwiML from it). For a
voice agent, inline `twiml` with a bidirectional `<Connect><Stream>` is simplest:

```xml
<Response>
  <Connect>
    <Stream url="wss://your-host/stream" />
  </Connect>
</Response>
```

- `<Connect><Stream>` is BIDIRECTIONAL (Twilio sends inbound audio AND plays audio you send back). This is what we need.
- `<Start><Stream>` is ONE-WAY (fork only, no playback). Do not use it.
- With `<Connect>`, Twilio pauses subsequent TwiML until your server closes the socket.
- `url` must be `wss://` (TLS). Query strings are NOT supported: use `<Parameter>` instead.

## 2. Inbound WebSocket events (Twilio -> server)

`connected` (first frame, handshake):
```json
{ "event": "connected", "protocol": "Call", "version": "1.0.0" }
```

`start` (once, carries IDs + custom params). Save `streamSid` here for all outbound messages:
```json
{
  "event": "start",
  "sequenceNumber": "1",
  "start": {
    "accountSid": "AC...",
    "streamSid": "MZ...",
    "callSid": "CA...",
    "tracks": ["inbound"],
    "mediaFormat": { "encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1 },
    "customParameters": { "ContactName": "Jane Doe" }
  },
  "streamSid": "MZ..."
}
```

`media` (streamed, ~every 20 ms). `payload` = base64 mu-law; `track` is "inbound" for caller audio:
```json
{
  "event": "media",
  "sequenceNumber": "4",
  "media": { "track": "inbound", "chunk": "2", "timestamp": "5", "payload": "<base64 mu-law>" },
  "streamSid": "MZ..."
}
```

`stop` (call/stream ended):
```json
{ "event": "stop", "sequenceNumber": "5",
  "stop": { "accountSid": "AC...", "callSid": "CA..." }, "streamSid": "MZ..." }
```

`mark` (echoed back to you when queued playback reaches your mark, see section 4).
`dtmf` events also arrive: `{ "event": "dtmf", "dtmf": { "track": "inbound_track", "digit": "1" }, ... }`.

## 3. Sending audio back (server -> Twilio)

```json
{ "event": "media", "streamSid": "MZ...", "media": { "payload": "<base64 mu-law>" } }
```

- `streamSid` (from the `start` event) is required; outbound `media` has only `media.payload`.
- Payload must be 8 kHz mono mu-law, base64-encoded (same format as inbound).
- Frame sizing: Twilio uses 20 ms frames = 160 bytes of raw mu-law per frame (8000 samples/s x 0.020 s = 160 samples = 160 bytes).
- Pacing: you may send larger chunks; Twilio buffers and plays them at real time. Send roughly in real time (do not flood far ahead) so barge-in `clear` stays responsive. A safe pattern is to emit 20 ms (160-byte) frames on a ~20 ms cadence, or send a few hundred ms at a time.

## 4. Barge-in: clear + mark

Send `clear` to immediately flush/discard all buffered outbound audio (use when the caller
starts talking over the agent):
```json
{ "event": "clear", "streamSid": "MZ..." }
```

Send a `mark` right after a chunk of speech to learn when playback of everything queued so far
has finished. Twilio sends the same `mark` back to you once that audio has played:
```json
{ "event": "mark", "streamSid": "MZ...", "mark": { "name": "reply-1-end" } }
```

When you receive an inbound `mark` event with `mark.name == "reply-1-end"`, playback of that
response is complete. Typical loop: stream TTS media frames, append a `mark`; on VAD detecting
caller speech, send `clear` and stop generating; ignore the now-stale `mark` echo.

## 5. Custom parameters (e.g. contact name)

Attach via nested `<Parameter>` (query strings on the wss URL are not allowed):
```xml
<Connect>
  <Stream url="wss://your-host/stream">
    <Parameter name="ContactName" value="Jane Doe" />
    <Parameter name="LeadId" value="12345" />
  </Stream>
</Connect>
```
Read them in the `start` event at `start.customParameters` (e.g. `start["customParameters"]["ContactName"]`).
Names are returned as given; values are strings.

## 6. Trial-account + connectivity constraints

- Trial accounts can only call/text VERIFIED numbers (verify callees in the Twilio console first), and calls include a trial notice message.
- Media Streams requires a publicly reachable `wss://` URL. In local dev, expose FastAPI via ngrok (e.g. `ngrok http 8000`) and use the `wss://` form of the forwarding host. Plain `ws://` is rejected.

## 7. Outbound PSTN pricing (US, for the cost meter)

- Outbound calls to US numbers: about $0.014 per minute (inbound about $0.0085 to $0.013/min).
- Twilio phone number rental: roughly $1 to $2 per number per month.
- Use ~$0.014/min as the per-minute outbound estimate; bill per minute, rounded up.

Sources: twilio.com/docs/voice/media-streams, twilio.com/docs/voice/twiml/stream,
twilio.com/docs/voice/pricing, twilio.com/en-us/voice/pricing/us.
