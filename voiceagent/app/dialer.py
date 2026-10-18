"""Place a platform call: everything Twilio needs to dial a lead with a workspace's own account,
open the media stream back to this instance, and report AMD / final status per call."""

from __future__ import annotations

from typing import Callable, Optional

from ..config import Settings
from ..contracts import Telephony
from .calltoken import sign_call
from .models import Call, Lead

# (account_sid, auth_token) -> Telephony. Injected so the worker and tests can swap Twilio out.
TelephonyFactory = Callable[[str, str], Telephony]


def twilio_factory(account_sid: str, auth_token: str) -> Telephony:
    from ..telephony.twilio import TwilioTelephony

    return TwilioTelephony(account_sid, auth_token)


def stream_url(public_base_url: str) -> str:
    base = public_base_url.rstrip("/")
    return base.replace("https://", "wss://").replace("http://", "ws://") + "/stream"


def stream_parameters(call: Call, lead: Optional[Lead] = None) -> dict:
    return {
        "call_id": call.id,
        "token": sign_call(call.id),
        "name": (lead.name if lead else call.contact_name) or "there",
        "phone": call.contact_phone,
        "email": lead.email if lead else "",
        "timezone": lead.timezone if lead else "",
    }


async def place_call(
    call: Call,
    lead: Optional[Lead],
    settings: Settings,
    *,
    detect_voicemail: bool,
    factory: TelephonyFactory = twilio_factory,
) -> str:
    """Dial and return the Twilio call SID. Raises on a provider error (the caller records it)."""

    base = (settings.public_base_url or "").rstrip("/")
    telephony = factory(settings.twilio_account_sid or "", settings.twilio_auth_token or "")
    return await telephony.place_call(
        to=call.contact_phone,
        from_=settings.twilio_from_number or "",
        stream_url=stream_url(base),
        parameters=stream_parameters(call, lead),
        amd_callback_url=f"{base}/voice/amd?call_id={call.id}" if detect_voicemail else None,
        status_callback_url=f"{base}/voice/status?call_id={call.id}",
    )


async def configure_inbound_webhook(settings: Settings, e164: str, voice_url: str) -> str:
    """Point a Twilio number's "A call comes in" webhook at this instance. Returns the number's
    SID. Runs the sync Twilio SDK in a thread."""

    import asyncio

    def _do() -> str:
        from twilio.rest import Client

        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        matches = client.incoming_phone_numbers.list(phone_number=e164, limit=1)
        if not matches:
            raise LookupError(f"{e164} is not a number on this Twilio account")
        number = matches[0].update(voice_url=voice_url, voice_method="POST")
        return number.sid

    return await asyncio.to_thread(_do)
