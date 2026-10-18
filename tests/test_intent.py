"""Intent classifier tests: regex priority order and the LLM fallback."""

from __future__ import annotations

import pytest

from tests.fakes import FakeLLM
from voiceagent.contracts import CallState, Intent
from voiceagent.intent import IntentClassifier


@pytest.fixture
def classifier(cfg):
    return IntentClassifier(cfg.script, cfg.compliance, llm=None)


async def test_booking_signal(classifier):
    r = await classifier.classify("yeah okay sounds good", CallState.VALUE_PROP)
    assert r.intent == Intent.BOOKING


async def test_objection_trigger(classifier):
    r = await classifier.classify("honestly we're too busy right now", CallState.VALUE_PROP)
    assert r.intent == Intent.OBJECTION
    assert r.matched_trigger == "too busy"


async def test_end_signal(classifier):
    r = await classifier.classify("goodbye", CallState.VALUE_PROP)
    assert r.intent == Intent.END


async def test_opt_out_beats_everything(classifier):
    # Contains a booking-ish word but opt-out must win as a compliance hard stop.
    r = await classifier.classify("okay sure, but please remove me from your list", CallState.VALUE_PROP)
    assert r.intent == Intent.OPT_OUT
    assert r.source == "compliance"


async def test_empty_is_silence(classifier):
    r = await classifier.classify("   ", CallState.GREETING)
    assert r.intent == Intent.SILENCE


async def test_neutral_without_llm(classifier):
    r = await classifier.classify("hmm, tell me a bit more about it", CallState.VALUE_PROP)
    assert r.intent == Intent.NEUTRAL


async def test_llm_fallback_labels_objection(cfg):
    # Ambiguous turn, no regex match; the LLM fallback labels it.
    llm = FakeLLM(["objection"])
    classifier = IntentClassifier(cfg.script, cfg.compliance, llm=llm)
    r = await classifier.classify("that sounds expensive for us", CallState.VALUE_PROP)
    assert r.intent == Intent.OBJECTION
    assert r.source == "llm"


async def test_llm_fallback_not_used_in_greeting(cfg):
    llm = FakeLLM(["objection"])
    classifier = IntentClassifier(cfg.script, cfg.compliance, llm=llm)
    r = await classifier.classify("who is this", CallState.GREETING)
    # Greeting state does not consult the LLM fallback.
    assert r.source == "regex"
    assert r.intent == Intent.NEUTRAL
