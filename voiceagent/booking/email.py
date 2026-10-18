"""Capturing an email address over the phone: turn what the STT heard ("john dot smith at gmail
dot com") into an address, and read it back in a way the contact can check."""

from __future__ import annotations

import re
from typing import Optional

_EMAIL_RE = re.compile(r"[a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}")

_SPOKEN = [
    (re.compile(r"\s+at\s+|\s*@\s*"), "@"),
    (re.compile(r"\s+dot\s+|\s*\.\s*"), "."),
    (re.compile(r"\s+(?:underscore|under score)\s+"), "_"),
    (re.compile(r"\s+(?:dash|hyphen)\s+"), "-"),
]

# Conversational words around the address. Removed as whole words anywhere in the turn, so
# "no, it's pam at ..." does not collapse into "spam@...".
_FILLER_RE = re.compile(
    r"\b(?:yeah|yes|no|nope|sure|okay|ok|so|um+|uh+|actually|sorry|it'?s|it is|my email is|"
    r"my email|email is|the email is|that'?s|you can use|use|send it to|it should be)\b[,.]?"
)


def parse_spoken_email(text: str) -> Optional[str]:
    """Best-effort email from a transcript. Returns None when it does not look like an address."""

    low = text.lower().strip().rstrip(".!?")
    direct = _EMAIL_RE.search(low.replace(" ", ""))
    if "@" in low and direct:
        return direct.group(0)
    low = " " + _FILLER_RE.sub(" ", low) + " "
    for pattern, repl in _SPOKEN:
        low = pattern.sub(repl, low)
    # Spelled letters ("j o h n") and stray spaces collapse together.
    candidate = re.sub(r"\s+", "", low)
    m = _EMAIL_RE.search(candidate)
    return m.group(0) if m else None


def speak_email(email: str) -> str:
    """Read an address back: spell a short local part letter by letter, say the domain normally.
    "jo.smith@gmail.com" -> "J-O dot S-M-I-T-H at gmail dot com"."""

    local, _, domain = email.partition("@")

    def spell(part: str) -> str:
        if len(part) > 12 or not part:
            return part
        return "-".join(ch.upper() if ch.isalpha() else ch for ch in part)

    local_spoken = " dot ".join(spell(p) for p in local.split("."))
    domain_spoken = " dot ".join(domain.split("."))
    return f"{local_spoken} at {domain_spoken}"
