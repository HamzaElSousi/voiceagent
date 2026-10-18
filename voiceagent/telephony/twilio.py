"""Twilio telephony and Media Streams implementation.

Implements the Telephony and MediaStream contracts from voiceagent.contracts.

Audio convention: all frames crossing these interfaces are raw bytes of 8 kHz
mono mu-law (G.711 u-law). Twilio transmits audio as base64-encoded strings
inside JSON messages over a WebSocket. This module handles the encoding and
decoding boundary so callers only ever deal with plain bytes.

The twilio.rest.Client is imported lazily inside TwilioTelephony so that
importing this module never fails when credentials are absent (unit tests,
CI, etc.).
"""

from __future__ import annotations

import asyncio
import base64
import xml.sax.saxutils as _saxutils
from typing import Any, AsyncIterator


def _xml_attr(value: str) -> str:
    """Escape a string for safe embedding in a double-quoted XML attribute value.

    xml.sax.saxutils.escape handles & < >; the extra entities map adds " and '
    so that a contact name such as O'Brien or Say "Hi" cannot break the TwiML.
    """
    return _saxutils.escape(str(value), {'"': "&quot;", "'": "&apos;"})


# ---------------------------------------------------------------------------
# Pure TwiML builder
# ---------------------------------------------------------------------------


def build_stream_twiml(stream_url: str, parameters: dict | None = None) -> str:
    """Return a minimal bidirectional <Connect><Stream> TwiML string.

    Uses <Connect><Stream> (not <Start><Stream>), which is required for
    bidirectional audio: Twilio sends inbound audio AND plays back whatever
    the server sends, pausing subsequent TwiML until the socket is closed.

    Custom parameters are embedded as <Parameter> children because Twilio
    does not allow query strings on wss:// stream URLs.

    Args:
        stream_url: The wss:// URL of the server WebSocket endpoint.
        parameters: Optional key-value pairs injected as <Parameter> tags,
            accessible from the start event at start["customParameters"].

    Returns:
        A TwiML XML string, suitable for passing as the `twiml` argument
        to client.calls.create().
    """
    param_xml = ""
    if parameters:
        for name, value in parameters.items():
            esc_name = _xml_attr(name)
            esc_value = _xml_attr(value)
            param_xml += f'<Parameter name="{esc_name}" value="{esc_value}"/>'

    return (
        f"<Response>"
        f"<Connect>"
        f'<Stream url="{stream_url}">'
        f"{param_xml}"
        f"</Stream>"
        f"</Connect>"
        f"</Response>"
    )


# ---------------------------------------------------------------------------
# Pure message helpers (no I/O)
# ---------------------------------------------------------------------------


def decode_media_payload(msg: dict) -> bytes | None:
    """Extract and base64-decode the mu-law payload from an inbound Twilio media event.

    Returns None for any non-media event or a message missing the payload field,
    so callers can safely skip transparently in a receive loop.

    Args:
        msg: A parsed Twilio WebSocket JSON message.

    Returns:
        Raw mu-law bytes, or None if the message is not a decodable media event.
    """
    if msg.get("event") != "media":
        return None
    media = msg.get("media", {})
    payload = media.get("payload")
    if payload is None:
        return None
    return base64.b64decode(payload)


def encode_media_message(stream_sid: str, frame: bytes) -> dict:
    """Build the outbound media JSON envelope for one mu-law audio frame.

    Args:
        stream_sid: The streamSid captured from the Twilio start event.
        frame: Raw mu-law bytes (160 bytes = 20 ms at 8 kHz).

    Returns:
        A dict ready for JSON serialization and sending over the WebSocket.
    """
    return {
        "event": "media",
        "streamSid": stream_sid,
        "media": {
            "payload": base64.b64encode(frame).decode("ascii"),
        },
    }


def clear_message(stream_sid: str) -> dict:
    """Build a clear event to flush Twilio's buffered outbound audio.

    Send this when the contact starts speaking over the agent (barge-in).
    Twilio discards all audio queued for playback the instant it receives
    this message, so the contact stops hearing the agent immediately.

    Args:
        stream_sid: The streamSid captured from the Twilio start event.
    """
    return {"event": "clear", "streamSid": stream_sid}


def mark_message(stream_sid: str, name: str) -> dict:
    """Build a mark event to detect when queued audio has finished playing.

    After the last audio frame of an utterance is sent, send a mark. Twilio
    echoes the same mark back once all previously queued audio has played out.
    On detecting caller speech before receiving the echo, send a clear and
    ignore the stale mark when it eventually arrives.

    Args:
        stream_sid: The streamSid captured from the Twilio start event.
        name: A caller-chosen label echoed verbatim in the inbound mark event.
    """
    return {"event": "mark", "streamSid": stream_sid, "mark": {"name": name}}


def parse_start_event(msg: dict) -> tuple[str, str, dict]:
    """Extract (stream_sid, call_sid, custom_parameters) from a Twilio start event.

    Exposed as a pure function so tests can exercise the handshake parse without
    a real WebSocket connection.

    Args:
        msg: The parsed Twilio start event dict.

    Returns:
        A 3-tuple of (stream_sid, call_sid, custom_parameters). Fields missing
        from the message default to empty string / empty dict.
    """
    start = msg.get("start", {})
    stream_sid = start.get("streamSid", "")
    call_sid = start.get("callSid", "")
    custom_parameters = dict(start.get("customParameters", {}))
    return stream_sid, call_sid, custom_parameters


# ---------------------------------------------------------------------------
# MediaStream implementation
# ---------------------------------------------------------------------------


class TwilioMediaStream:
    """MediaStream backed by a Twilio WebSocket connection.

    Wraps a Starlette/FastAPI WebSocket (duck-typed: any object that provides
    async receive_json() and async send_json()). The Twilio handshake (reading
    the connected event then the start event) is performed automatically at the
    beginning of receive_audio() so callers need not manage it separately.

    Inbound mu-law frames (contact speaking) are base64-decoded out of Twilio
    media events before being yielded. Outbound mu-law frames (agent speaking)
    are base64-encoded into Twilio media events before being sent.

    Barge-in: calling clear() sends a Twilio clear event, which causes Twilio
    to discard its outbound audio buffer immediately. The contact stops hearing
    the agent within one round-trip latency regardless of how much audio was
    queued ahead.
    """

    def __init__(self, websocket: Any) -> None:
        """
        Args:
            websocket: A Starlette/FastAPI WebSocket (or any duck-type with
                async receive_json() and async send_json()).
        """
        self._ws = websocket
        self._call_sid: str = ""
        self._stream_sid: str = ""
        self._custom_parameters: dict = {}
        self._handshake_done: bool = False

    # ------------------------------------------------------------------
    # Properties (populated after the start handshake inside receive_audio)
    # ------------------------------------------------------------------

    @property
    def call_sid(self) -> str:
        """The Twilio call SID (CA...), set after the start handshake."""
        return self._call_sid

    @property
    def stream_sid(self) -> str:
        """The Twilio stream SID (MZ...), set after the start handshake."""
        return self._stream_sid

    @property
    def custom_parameters(self) -> dict:
        """Custom parameters from the <Parameter> TwiML tags, set after handshake."""
        return self._custom_parameters

    # ------------------------------------------------------------------
    # MediaStream contract methods
    # ------------------------------------------------------------------

    async def start_handshake(self) -> dict:
        """Read the connected and start events up front, capturing session ids and custom
        parameters, without consuming any media frames. Idempotent: safe to call once before
        receive_audio() so the server can read contact info (custom_parameters) before building
        the engine. receive_audio() skips the handshake if it was already done here.

        Returns:
            The custom_parameters dict from the start event (contact info via <Parameter> tags).
        """
        if self._handshake_done:
            return self._custom_parameters
        # Consume the connected handshake frame (no useful fields).
        await self._ws.receive_json()
        # Consume the start frame and capture session identifiers.
        start_msg = await self._ws.receive_json()
        if start_msg.get("event") == "start":
            (
                self._stream_sid,
                self._call_sid,
                self._custom_parameters,
            ) = parse_start_event(start_msg)
        self._handshake_done = True
        return self._custom_parameters

    async def receive_audio(self) -> AsyncIterator[bytes]:
        """Perform the Twilio handshake (if not already done), then yield decoded mu-law frames.

        Handshake sequence (per Twilio Media Streams spec):
          1. Read the connected event (protocol acknowledgement, discarded).
          2. Read the start event, capturing streamSid, callSid, and
             customParameters for the session.
          3. Enter the media loop: yield decoded inbound frames from media
             events, skip dtmf/mark events, stop on a stop event or any
             WebSocket read error.

        Yields:
            Raw mu-law bytes (160 bytes per 20 ms frame at 8 kHz) as they
            arrive from the contact.
        """
        # Steps 1 and 2: the start handshake, unless start_handshake() already ran it.
        if not self._handshake_done:
            await self.start_handshake()

        # Step 3: stream media events until stop or WebSocket closure.
        while True:
            try:
                msg = await self._ws.receive_json()
            except Exception:
                # WebSocket closed or a read error; end the iterator cleanly.
                break

            event = msg.get("event")
            if event == "stop":
                break
            if event == "media":
                frame = decode_media_payload(msg)
                if frame is not None:
                    yield frame
            # dtmf, mark, and other event types are silently skipped here.
            # Upper layers can inspect custom_parameters for DTMF if needed.

    async def send_audio(self, frame: bytes) -> None:
        """Send one mu-law audio frame (160 bytes, 20 ms) to the contact.

        Args:
            frame: Raw mu-law bytes. Twilio expects 8 kHz mono mu-law;
                non-conforming sizes are passed through as-is (Twilio buffers
                and plays at real time regardless of chunk size).
        """
        await self._ws.send_json(encode_media_message(self._stream_sid, frame))

    async def clear(self) -> None:
        """Flush Twilio's outbound audio buffer (barge-in support).

        Sends a clear event to Twilio, which immediately discards all audio
        queued for playback. Call this as soon as VAD detects the contact
        speaking so they hear silence instead of the agent continuing mid-word.
        """
        await self._ws.send_json(clear_message(self._stream_sid))

    async def close(self) -> None:
        """No-op: WebSocket lifecycle is managed by the FastAPI route handler."""


# ---------------------------------------------------------------------------
# Telephony implementation
# ---------------------------------------------------------------------------


class TwilioTelephony:
    """Telephony implementation that places and hangs up outbound calls via Twilio REST.

    The twilio.rest.Client is constructed lazily on first call to place_call()
    or hang_up() so that importing this module never requires credentials to be
    present. All synchronous Twilio SDK calls are dispatched via asyncio.to_thread
    to avoid blocking the event loop.
    """

    def __init__(self, account_sid: str, auth_token: str) -> None:
        """
        Args:
            account_sid: Twilio Account SID (AC...).
            auth_token: Twilio Auth Token.
        """
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._client: Any = None  # twilio.rest.Client, created on first use

    def _get_client(self) -> Any:
        """Return the twilio.rest.Client, creating it on first call."""
        if self._client is None:
            from twilio.rest import Client  # imported lazily: no credentials at import time

            self._client = Client(self._account_sid, self._auth_token)
        return self._client

    async def place_call(
        self,
        to: str,
        from_: str,
        stream_url: str,
        parameters: dict | None = None,
        amd_callback_url: str | None = None,
    ) -> str:
        """Dial `to` with a bidirectional Media Stream pointed at `stream_url`.

        Embeds the stream URL in inline TwiML using <Connect><Stream> so no
        public answer URL is required. The synchronous Twilio SDK call is run
        in a thread to keep the event loop free.

        The optional `parameters` dict is injected as <Parameter> children of
        <Stream> so the server can read contact info from the Twilio start event
        at start["customParameters"]. This keeps the Telephony Protocol's
        positional signature (to, from_, stream_url) intact; `parameters` is an
        additive optional kwarg and the Protocol check is structural (method
        presence only), so conformance is preserved.

        Args:
            to: E.164 destination number (e.g. "+15558675310").
            from_: E.164 Twilio caller number (e.g. "+15551112222").
            stream_url: wss:// URL of the server WebSocket endpoint.
            parameters: Optional contact key-value pairs forwarded to the server
                via TwiML <Parameter> tags (e.g. name, phone, email,
                suggested_time). Values are XML-escaped inside build_stream_twiml.
            amd_callback_url: When set, enable async answering-machine detection and
                have Twilio POST the result (AnsweredBy) to this URL.

        Returns:
            The Twilio call SID (CA...) of the newly created call.
        """
        client = self._get_client()
        twiml = build_stream_twiml(stream_url, parameters)
        kwargs: dict[str, Any] = {"to": to, "from_": from_, "twiml": twiml}
        if amd_callback_url:
            # Async AMD: the call connects (and the greeting starts) immediately; Twilio posts
            # AnsweredBy to the callback once it has decided, after the beep for a machine. Sync
            # AMD would hold a human in silence for several seconds before the greeting.
            kwargs.update(
                machine_detection="DetectMessageEnd",
                async_amd="true",
                async_amd_status_callback=amd_callback_url,
                async_amd_status_callback_method="POST",
            )
        call = await asyncio.to_thread(client.calls.create, **kwargs)
        return call.sid

    async def hang_up(self, call_sid: str) -> None:
        """End an in-progress call by updating its status to 'completed'.

        Args:
            call_sid: The Twilio call SID (CA...) to terminate.
        """
        client = self._get_client()
        await asyncio.to_thread(
            client.calls(call_sid).update,
            status="completed",
        )
