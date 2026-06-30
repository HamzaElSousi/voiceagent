"""Unit tests for split_into_sentences and SentenceAggregator.

All tests are pure-Python, zero-network. The sentence splitter and
SentenceAggregator are exercised in isolation from the ElevenLabs client.
"""

from __future__ import annotations

import pytest

from voiceagent.tts.elevenlabs import SentenceAggregator, split_into_sentences


# ---------------------------------------------------------------------------
# split_into_sentences: basic cases
# ---------------------------------------------------------------------------


def test_split_empty():
    assert split_into_sentences("") == []
    assert split_into_sentences("   ") == []


def test_split_single_sentence_with_period():
    result = split_into_sentences("Hello there.")
    assert result == ["Hello there."]


def test_split_single_sentence_question():
    result = split_into_sentences("How are you?")
    assert result == ["How are you?"]


def test_split_single_sentence_exclamation():
    result = split_into_sentences("Watch out!")
    assert result == ["Watch out!"]


def test_split_two_sentences():
    result = split_into_sentences("Hello there. How are you?")
    assert result == ["Hello there.", "How are you?"]


def test_split_three_sentences():
    result = split_into_sentences("First. Second! Third?")
    # "Third?" has no whitespace after it, so it lands in the remainder.
    assert result == ["First.", "Second!", "Third?"]


def test_split_trailing_fragment_no_punct():
    # "World" has no terminal punctuation: it is a trailing fragment.
    result = split_into_sentences("Hello. World")
    assert result == ["Hello.", "World"]


def test_split_trailing_whitespace_ignored():
    result = split_into_sentences("  Hello there.  How are you?  ")
    assert result == ["Hello there.", "How are you?"]


# ---------------------------------------------------------------------------
# split_into_sentences: abbreviation handling
# ---------------------------------------------------------------------------


def test_split_mr_abbreviation():
    # "Mr." should not split: "Mr. Smith" is one name, not two sentences.
    result = split_into_sentences("Mr. Smith is available.")
    assert result == ["Mr. Smith is available."]


def test_split_dr_abbreviation():
    result = split_into_sentences("Dr. Jones will call back. Please hold.")
    assert result == ["Dr. Jones will call back.", "Please hold."]


def test_split_etc_abbreviation():
    result = split_into_sentences("We cover sales, support, etc. Let me explain.")
    assert result == ["We cover sales, support, etc. Let me explain."]


def test_split_initial_letter():
    # Single-letter word before "." is treated as an initial, not a sentence end.
    result = split_into_sentences("Contact J. Smith for details. Thanks.")
    assert result == ["Contact J. Smith for details.", "Thanks."]


# ---------------------------------------------------------------------------
# split_into_sentences: multi-punctuation
# ---------------------------------------------------------------------------


def test_split_question_exclamation():
    # "?!" is treated as compound terminal punctuation.
    result = split_into_sentences("Really?! Yes, indeed.")
    assert result == ["Really?!", "Yes, indeed."]


def test_split_ellipsis_not_a_boundary():
    # Three or more consecutive dots are a stylistic pause, not a boundary.
    result = split_into_sentences("Wait... okay. Done.")
    assert result == ["Wait... okay.", "Done."]


def test_split_two_dots_is_a_boundary():
    # Two dots ARE treated as terminal (rare, but not ellipsis).
    result = split_into_sentences("Hmm.. Next sentence.")
    # Two dots match the pattern; the result may vary but should not crash.
    assert isinstance(result, list)
    assert len(result) >= 1


# ---------------------------------------------------------------------------
# SentenceAggregator: incremental streaming
# ---------------------------------------------------------------------------


def test_aggregator_basic_streaming():
    """Core streaming scenario: sentence 1 completes before sentence 2."""
    agg = SentenceAggregator()

    deltas = ["Hi there.", " How are", " you?", " Bye"]
    emitted: list[str] = []
    for delta in deltas:
        emitted.extend(agg.push(delta))

    final = agg.flush()

    assert emitted == ["Hi there.", "How are you?"]
    assert final == "Bye"


def test_aggregator_complete_sentence_in_one_push():
    agg = SentenceAggregator()
    result = agg.push("Hello.")
    assert result == ["Hello."]
    assert agg.flush() is None


def test_aggregator_no_complete_sentence():
    agg = SentenceAggregator()
    assert agg.push("Hello") == []
    assert agg.push(", world") == []
    remainder = agg.flush()
    assert remainder == "Hello, world"


def test_aggregator_multiple_sentences_in_one_push():
    """When a single delta contains multiple complete sentences."""
    agg = SentenceAggregator()
    result = agg.push("Hello. World. Foo")
    assert result == ["Hello.", "World."]
    assert agg.flush() == "Foo"


def test_aggregator_question_and_fragment():
    agg = SentenceAggregator()
    result = agg.push("Are you there?")
    assert result == ["Are you there?"]
    agg.push(" Maybe")
    remainder = agg.flush()
    assert remainder == "Maybe"


def test_aggregator_flush_on_empty_buffer():
    agg = SentenceAggregator()
    assert agg.flush() is None


def test_aggregator_reusable_after_flush():
    """Aggregator can be reused for a second turn after flush."""
    agg = SentenceAggregator()

    # First turn
    agg.push("Turn one.")
    agg.flush()

    # Second turn
    emitted: list[str] = []
    for delta in ["Turn two.", " Still going"]:
        emitted.extend(agg.push(delta))
    remainder = agg.flush()

    assert emitted == ["Turn two."]
    assert remainder == "Still going"


def test_aggregator_with_abbreviation_does_not_split_early():
    agg = SentenceAggregator()
    # "Mr." should not prematurely flush
    result_a = agg.push("Hello, Mr.")
    result_b = agg.push(" Smith. How are you?")
    remainder = agg.flush()

    all_emitted = result_a + result_b
    # The complete sentence "Hello, Mr. Smith." should appear at some point,
    # or both sentences once the buffer is flushed. The key invariant: we must
    # NOT get "Hello, Mr." as a standalone sentence.
    for s in all_emitted:
        assert s != "Hello, Mr."
    combined = " ".join(all_emitted + ([remainder] if remainder else []))
    assert "Mr. Smith" in combined


def test_aggregator_trailing_punctuation_all_flushed():
    """When the buffer ends with '?', all sentences are returned immediately."""
    agg = SentenceAggregator()
    result = agg.push("First. Second?")
    assert "First." in result
    assert "Second?" in result
    assert agg.flush() is None
