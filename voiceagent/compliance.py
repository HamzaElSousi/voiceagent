"""Compliance: the safety layer for outbound sales calls.

Outbound calls carry legal weight (TCPA and similar), so compliance is on by default, not an
afterthought. This module owns:

  - AI disclosure: a clear line that the caller is an AI, injected into the greeting.
  - Opt-out / do-not-call: a "remove me" / "do not call" utterance is a hard stop that suppresses
    the contact and is logged. Suppressed numbers are persisted and checked before dialing.
  - Recording consent: an optional consent line and gate.
  - Calling-hour limits: timezone-aware, so a contact is never dialed outside allowed hours.

File I/O here happens before/after a call (loading the DNC list, suppressing a number), never in
the real-time audio loop, so plain synchronous file access is fine.
"""

from __future__ import annotations

import csv
import re
from datetime import datetime, time
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from .config import ComplianceConfig


def normalize_phone(phone: str) -> str:
    """Normalize to digits with an optional leading +. Lets '+1 (415) 555-1234' and
    '14155551234' compare equal-ish against the DNC list."""

    phone = phone.strip()
    plus = phone.startswith("+")
    digits = re.sub(r"\D", "", phone)
    return ("+" + digits) if plus else digits


class ComplianceGate:
    """Stateful compliance checks for one campaign/agent config."""

    def __init__(self, config: ComplianceConfig, dnc_path: Optional[str | Path] = None):
        self.config = config
        self.dnc_path = Path(dnc_path) if dnc_path else Path(config.do_not_call_file)
        self._tz = ZoneInfo(config.calling_hours.timezone)
        self._opt_out_phrases = [p.lower() for p in config.opt_out_phrases]

    # -- AI disclosure ----------------------------------------------------------------------

    def disclosure_line(self) -> str:
        return self.config.ai_disclosure

    def apply_disclosure(self, greeting: str) -> str:
        """Prepend the AI-disclosure line to the greeting when configured to disclose up front."""

        if self.config.disclose_in_greeting and self.config.ai_disclosure:
            return f"{self.config.ai_disclosure} {greeting}".strip()
        return greeting

    # -- Opt-out / do-not-call --------------------------------------------------------------

    def is_opt_out(self, text: str) -> bool:
        low = text.lower()
        return any(phrase in low for phrase in self._opt_out_phrases)

    def is_suppressed(self, phone: str) -> bool:
        """True if `phone` is on the do-not-call list."""

        # Compare on digits-only so "+14155551234" and "14155551234" match.
        target = re.sub(r"\D", "", phone)
        return target in self._load_dnc()

    def suppress(self, phone: str, reason: str = "opt_out") -> None:
        """Add a number to the do-not-call list (idempotent). Logs the reason and timestamp."""

        target = normalize_phone(phone)
        if self.is_suppressed(target):
            return
        self.dnc_path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.dnc_path.exists()
        with self.dnc_path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if new_file:
                writer.writerow(["phone", "reason", "suppressed_at"])
            writer.writerow([target, reason, datetime.now(self._tz).isoformat()])

    def _load_dnc(self) -> set[str]:
        if not self.dnc_path.exists():
            return set()
        out: set[str] = set()
        with self.dnc_path.open(newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            for i, row in enumerate(reader):
                if not row:
                    continue
                if i == 0 and row[0].strip().lower() == "phone":
                    continue  # header
                # Normalize to digits-only so "+14155551234" and "14155551234" compare equal.
                out.add(re.sub(r"\D", "", row[0]))
        return out

    # -- Recording consent ------------------------------------------------------------------

    def recording_consent_line(self) -> Optional[str]:
        if self.config.require_recording_consent:
            return self.config.recording_consent
        return None

    # -- Calling hours ----------------------------------------------------------------------

    def within_calling_hours(self, now: Optional[datetime] = None) -> bool:
        """True if `now` (in the configured timezone) falls inside the allowed calling window.
        Handles a window that wraps past midnight."""

        now = now.astimezone(self._tz) if now else datetime.now(self._tz)
        current: time = now.timetz().replace(tzinfo=None)
        start = self.config.calling_hours.start
        end = self.config.calling_hours.end
        if start <= end:
            return start <= current <= end
        # Wrapping window, e.g. 20:00 to 06:00.
        return current >= start or current <= end

    def calling_hours_reason(self) -> str:
        h = self.config.calling_hours
        return (
            f"outside calling hours {h.start.strftime('%H:%M')}-{h.end.strftime('%H:%M')} "
            f"{h.timezone}"
        )

    def can_dial(self, phone: str, now: Optional[datetime] = None) -> tuple[bool, str]:
        """Single pre-dial gate. Returns (allowed, reason). Reason is empty when allowed."""

        if self.is_suppressed(phone):
            return False, "on do-not-call list"
        if not self.within_calling_hours(now):
            return False, self.calling_hours_reason()
        return True, ""
