"""Sentence aggregation for sentence-level TTS streaming.

The pipeline feeds the LLM's streamed text deltas into a SentenceAggregator and gets back
complete sentences as soon as they form, so synthesis of sentence 1 can start while the model is
still writing sentence 2. This is the heart of the latency budget.
"""

from __future__ import annotations

import re

# End-of-sentence punctuation followed by whitespace or end of string.
_SENTENCE_END = re.compile(r"([.!?])(\s+|$)")
# A few abbreviations that should not be treated as sentence ends.
_ABBREV = {"mr.", "mrs.", "ms.", "dr.", "st.", "vs.", "e.g.", "i.e.", "etc."}


class SentenceAggregator:
    """Accumulates streaming text and emits complete sentences incrementally."""

    def __init__(self) -> None:
        self._buf = ""

    def add(self, delta: str) -> list[str]:
        """Add a text delta. Return any newly complete sentences (possibly empty)."""

        self._buf += delta
        out: list[str] = []
        while True:
            sentence = self._take_one()
            if sentence is None:
                break
            if sentence.strip():
                out.append(sentence.strip())
        return out

    def flush(self) -> list[str]:
        """Emit any trailing text as a final sentence."""

        rest = self._buf.strip()
        self._buf = ""
        return [rest] if rest else []

    def _take_one(self) -> str | None:
        for match in _SENTENCE_END.finditer(self._buf):
            end = match.end()
            candidate = self._buf[:end]
            last_word = candidate.strip().split()[-1].lower() if candidate.strip() else ""
            if last_word in _ABBREV:
                continue  # not a real sentence end, keep scanning
            self._buf = self._buf[end:]
            return candidate
        return None


def split_into_sentences(text: str) -> list[str]:
    """Split a complete string into sentences (non-streaming convenience wrapper)."""

    agg = SentenceAggregator()
    out = agg.add(text)
    out.extend(agg.flush())
    return out
