"""Sentence aggregation tests: streaming deltas to complete sentences."""

from __future__ import annotations

from voiceagent.text import SentenceAggregator, split_into_sentences


def test_split_basic():
    assert split_into_sentences("Hi there. How are you?") == ["Hi there.", "How are you?"]


def test_aggregator_streams_incrementally():
    agg = SentenceAggregator()
    out = []
    for delta in ["Hi there.", " How are", " you?", " Bye"]:
        out.extend(agg.add(delta))
    # The first complete sentence emerges before the stream ends.
    assert out[0] == "Hi there."
    assert "How are you?" in out
    out.extend(agg.flush())
    assert out[-1] == "Bye"


def test_abbreviation_not_split():
    out = split_into_sentences("Call Dr. Smith now. Thanks!")
    assert out == ["Call Dr. Smith now.", "Thanks!"]


def test_first_sentence_ready_while_writing_second():
    agg = SentenceAggregator()
    first = agg.add("We cut delays by 40%. ")
    assert first == ["We cut delays by 40%."]   # ready immediately
    # A fragment with no terminal is buffered, not emitted yet.
    held = agg.add("Want a demo")
    assert held == []
    # Once the terminal arrives, the sentence emits.
    done = agg.add("?")
    assert done == ["Want a demo?"]
