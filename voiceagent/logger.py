"""Outcome logging: CSV and webhook sinks.

Every finished call is logged with its outcome, cost, duration, latency, and transcript so a
campaign is auditable and the cost claims are verifiable. Conforms to the OutcomeSink Protocol.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Optional, Sequence

import aiofiles

from .contracts import CallResult, OutcomeSink

_CSV_FIELDS = [
    "call_sid",
    "contact_name",
    "contact_phone",
    "outcome",
    "duration_seconds",
    "total_usd",
    "p50_speech_to_speech_ms",
    "barge_in_safe",
    "end_reason",
    "error",
    "transcript",
]


def result_to_row(result: CallResult) -> dict:
    latencies = [s.endpoint_to_first_audio_ms for s in result.latencies if s.endpoint_to_first_audio_ms]
    p50 = 0.0
    if latencies:
        latencies.sort()
        p50 = round(latencies[len(latencies) // 2], 1)
    transcript = " | ".join(f"{t.speaker}: {t.text}" for t in result.transcript)
    return {
        "call_sid": result.call_sid,
        "contact_name": result.contact_name,
        "contact_phone": result.contact_phone,
        "outcome": result.outcome.value,
        "duration_seconds": result.duration_seconds,
        "total_usd": result.cost.total_usd,
        "p50_speech_to_speech_ms": p50,
        "barge_in_safe": "",
        "end_reason": result.end_reason,
        "error": result.error or "",
        "transcript": transcript,
    }


def result_to_dict(result: CallResult) -> dict:
    """Full structured payload for a webhook."""

    return {
        "call_sid": result.call_sid,
        "contact_name": result.contact_name,
        "contact_phone": result.contact_phone,
        "outcome": result.outcome.value,
        "duration_seconds": result.duration_seconds,
        "end_reason": result.end_reason,
        "error": result.error,
        "cost": {
            "total_usd": result.cost.total_usd,
            "deepgram_seconds": result.cost.deepgram_seconds,
            "llm_input_tokens": result.cost.llm_input_tokens,
            "llm_output_tokens": result.cost.llm_output_tokens,
            "tts_characters": result.cost.tts_characters,
            "telephony_seconds": result.cost.telephony_seconds,
        },
        "latencies_ms": [s.endpoint_to_first_audio_ms for s in result.latencies],
        "transcript": [
            {"speaker": t.speaker, "text": t.text, "state": t.state.value}
            for t in result.transcript
        ],
    }


class CsvOutcomeLogger:
    """Appends one row per call to a CSV file. Creates the header on first write."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    async def record(self, result: CallResult) -> None:
        row = result_to_row(result)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not self.path.exists()
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=_CSV_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)
        async with aiofiles.open(self.path, "a", encoding="utf-8", newline="") as fh:
            await fh.write(buf.getvalue())


class WebhookOutcomeLogger:
    """POSTs the structured result to a webhook URL. Fail-soft: a webhook error never breaks a
    campaign, it is swallowed and the call is still logged elsewhere."""

    def __init__(self, url: str, timeout: float = 5.0):
        self.url = url
        self.timeout = timeout

    async def record(self, result: CallResult) -> None:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                await client.post(self.url, json=result_to_dict(result))
        except Exception:
            pass


class CompositeOutcomeSink:
    """Fans a result out to several sinks (e.g. CSV plus a webhook)."""

    def __init__(self, sinks: Sequence[OutcomeSink]):
        self.sinks = list(sinks)

    async def record(self, result: CallResult) -> None:
        for sink in self.sinks:
            try:
                await sink.record(result)
            except Exception:
                pass


def build_outcome_sink(
    csv_path: Optional[str | Path] = None, webhook_url: Optional[str] = None
) -> OutcomeSink:
    sinks: list[OutcomeSink] = []
    if csv_path:
        sinks.append(CsvOutcomeLogger(csv_path))
    if webhook_url:
        sinks.append(WebhookOutcomeLogger(webhook_url))
    if not sinks:
        sinks.append(CsvOutcomeLogger("call_logs/outcomes.csv"))
    return sinks[0] if len(sinks) == 1 else CompositeOutcomeSink(sinks)
