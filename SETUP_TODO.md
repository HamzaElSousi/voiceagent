# VoiceAgent Setup Todo

The full pipeline is built and verified with mocked providers (zero network, zero real calls). The only thing left is account and key setup plus one live test call to your own phone. Everything below is a one-time setup of about 20 to 30 minutes, mostly free trials.

## Before you start

You can already run a full mocked call right now with zero keys:

```bash
# Full mocked conversation in your terminal (zero API keys)
python -m voiceagent.cli test-voice

# Or the demo web console at http://localhost:8000/ui
python -m voiceagent.cli serve
```

Try these first to see a complete conversation end to end. The code is production-ready; you are just plugging in the keys.

## Set up your accounts and keys

Each step is free or has a free trial. You will collect six values to put in `.env`.

- [ ] **Twilio (telephony)**. Sign up at [https://www.twilio.com/try-twilio](https://www.twilio.com/try-twilio). Navigate to Console > Phone Numbers > Buy a Number, purchase a Voice-capable phone number (any area code), and note the E.164 format like `+1...`. On a trial account, you must verify your own personal phone number before the test call connects: go to Console > Phone Numbers > Verified Caller IDs and verify your number. Copy three values from the Console dashboard and your purchased number:
  - `TWILIO_ACCOUNT_SID` (found on the Console home dashboard)
  - `TWILIO_AUTH_TOKEN` (found on the Console home dashboard, click the eye icon to show it)
  - `TWILIO_FROM_NUMBER` (the Voice number you just bought, e.g., `+16175551234`)

- [ ] **Deepgram (speech-to-text)**. Sign up at [https://console.deepgram.com/signup](https://console.deepgram.com/signup). Create a new API key from the dashboard. Copy the key value to `DEEPGRAM_API_KEY`. The free trial has generous credit for streaming STT.

- [ ] **ElevenLabs (text-to-speech)**. Sign up at [https://elevenlabs.io/](https://elevenlabs.io/). Navigate to Profile > Settings, find your API key, and copy it to `ELEVENLABS_API_KEY`. Then open the Voice Library, pick any voice you like (the Turbo model is used for latency), and save its `voice_id`. This `voice_id` goes in your `agent.yaml` under `agent.voice_id`, not in `.env`.

- [ ] **Anthropic (Claude LLM)**. Sign up at [https://console.anthropic.com/](https://console.anthropic.com/). Create an API key and copy it to `ANTHROPIC_API_KEY`. The code defaults to Claude Haiku, the fastest model.

- [ ] **ngrok (public HTTPS tunnel)**. Download and install from [https://ngrok.com/download](https://ngrok.com/download). You will run this in Terminal 2 (see below) to expose your local server to Twilio. No key required for basic use.

- [ ] **A phone to receive the test call**. Use your own verified phone number (the one you verified in Twilio above). The first campaign stays at one contact (you) until latency and barge-in behave on a real call.

## Put the keys in place

```bash
cp .env.example .env
```

Open `.env` in your editor and fill in the six keys plus the PUBLIC_BASE_URL:

```env
TWILIO_ACCOUNT_SID=<from Twilio Console>
TWILIO_AUTH_TOKEN=<from Twilio Console>
TWILIO_FROM_NUMBER=<your purchased number, e.g. +1...>
DEEPGRAM_API_KEY=<from Deepgram dashboard>
ELEVENLABS_API_KEY=<from ElevenLabs Settings>
ANTHROPIC_API_KEY=<from Anthropic Console>
PUBLIC_BASE_URL=<you will paste this from ngrok in the next step>
```

**Important**: secrets live in `.env` only (it is gitignored). Never put them in `agent.yaml`.

## Run the live test call

Open three terminals and follow this exact sequence:

**Terminal 1: Start the server**
```bash
python -m voiceagent.cli serve
```
You should see: "Uvicorn running on http://0.0.0.0:8000". Leave it running.

**Terminal 2: Expose the server with ngrok**
```bash
ngrok http 8000
```
You will see output like:
```
Session Status                online
Forwarding                    https://abc123.ngrok-free.app -> http://localhost:8000
```
Copy the `https://...` URL (e.g., `https://abc123.ngrok-free.app`). Paste it into your `.env` file as `PUBLIC_BASE_URL`. The app converts https to wss and appends `/stream` automatically.

**Terminal 3: Place the test call to your own phone**
```bash
python -m voiceagent.cli dial "+1YOURNUMBER" --name "Your Name"
```
Replace `+1YOURNUMBER` with your verified phone number (the same one you verified in Twilio). Your phone should ring; answer it and have a conversation with the agent.

The agent will greet you, make a pitch, handle objections, and attempt to book a meeting, all from your `agent.yaml` config.

## After the call

The call transcript and metrics are logged. Measure the real speech-to-speech latency (p50 and p95 percentiles) and the per-hop breakdown (endpoint to first token, first token to first audio). Check the API usage logs for the real per-call cost. Fill in the README "Measured" section with these numbers. These measurements are the resume bullets for the project.

## Verify your keys loaded

If you are unsure whether your keys are set up correctly, run:
```bash
python -m voiceagent.cli dial "+1YOURNUMBER" --name "Test"
```
The CLI will print a clear list of any missing env vars and stop before spending anything. Or call `GET /health` on the running server; it returns `missing_for_live_call` if any keys are missing.
