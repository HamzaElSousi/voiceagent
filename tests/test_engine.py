"""ConversationEngine tests for the LLM-driven core with deterministic guardrails.

Assert on outcomes, transitions, and which turns are LLM-driven vs guarded, not on timing. The
FakeLLM replays scripted agent replies so the whole engine runs with zero network.
"""

from __future__ import annotations

from voiceagent.contracts import CallState, Outcome
from tests.conftest import drain


async def test_greeting_includes_disclosure_and_name(make_engine):
    engine, _meter, _comp = make_engine()
    greeting = await drain(engine.open())
    assert engine.state == CallState.GREETING
    assert "AI assistant" in greeting
    assert "John Smith" in greeting


async def test_first_contact_turn_is_llm_driven(make_engine):
    # Any real turn after the greeting is answered by the LLM, not a fixed script line.
    engine, _m, _c = make_engine(llm_responses=["Sure! I'm Alex with Acme, we help teams ship faster."])
    await drain(engine.open())
    reply = await engine.handle("yeah sure, who is this?")
    text = await drain(reply)
    assert engine.state == CallState.VALUE_PROP
    assert "Alex with Acme" in text  # came straight from the LLM
    assert reply.end_after is False


async def test_legitimacy_question_is_answered_not_ended(make_engine):
    # "who is this / how did you get my number" must be handled, never hang up.
    engine, _m, _c = make_engine(
        llm_responses=["I'm an AI assistant reaching out on behalf of Acme, you're on our contact list."]
    )
    await drain(engine.open())
    reply = await engine.handle("wait, how did you get my number?")
    text = await drain(reply)
    assert reply.end_after is False
    assert reply.outcome is None
    assert "contact list" in text
    assert engine.state == CallState.VALUE_PROP


async def test_objections_do_not_end_the_call(make_engine):
    # Repeated pushback is handled by the LLM turn after turn; no hard objection limit ends it.
    engine, _m, _c = make_engine(llm_responses=["a", "b", "c", "d"])
    await drain(engine.open())
    await drain(await engine.handle("hi"))
    await drain(await engine.handle("honestly we're too busy"))
    await drain(await engine.handle("we already have something in place"))
    reply = await engine.handle("still too busy")
    await drain(reply)
    assert reply.end_after is False
    assert reply.outcome is None
    assert engine.state == CallState.VALUE_PROP


async def test_early_agreement_is_not_a_booking(make_engine):
    # A "sure" before the agent has offered a meeting must NOT book or hang up (the live-call bug).
    engine, _m, _c = make_engine(llm_responses=["Great, glad to hear it! Quick question first."])
    await drain(engine.open())
    reply = await engine.handle("sure")
    assert reply.outcome is None
    assert reply.end_after is False
    assert engine._meeting_offered is False
    assert engine.state == CallState.VALUE_PROP


async def test_booking_only_after_meeting_offered_then_two_beat_close(make_engine):
    # Once the agent has actually offered the demo, agreement CONFIRMS (beat 1) without hanging up,
    # then the contact's next turn gets a warm goodbye + end (beat 2). This fixes the abrupt hangup.
    engine, _m, _c = make_engine(
        llm_responses=["Would you be open to a quick 15-minute demo this week?"]
    )
    await drain(engine.open())
    await drain(await engine.handle("okay, what's this about?"))  # LLM offers the demo
    assert engine._meeting_offered is True

    # Beat 1: agreement confirms but does NOT hang up (gives the contact a last word).
    beat1 = await engine.handle("yeah okay, let's do it")
    assert engine._booking_confirmed is True
    assert beat1.end_after is False
    assert beat1.outcome is None
    text1 = await drain(beat1)
    assert "anything else" in text1.lower()

    # Beat 2: their next turn (or a silence) closes warmly with the booking outcome.
    beat2 = await engine.handle("no, that's all, thanks")
    assert beat2.outcome == Outcome.BOOKED_MEETING
    assert beat2.end_after is True
    assert engine.state == CallState.END
    text2 = await drain(beat2)
    assert "take care" in text2.lower() or "thanks" in text2.lower()


async def test_affirmative_inside_info_request_does_not_book(make_engine):
    # "Yes, tell me more" contains an affirmative but is an info request; it must NOT book (the
    # live-call false-booking bug, ISSUES.md O12). The LLM should keep talking instead.
    engine, _m, _c = make_engine(
        llm_responses=[
            "Open to a quick 15-minute demo this week?",  # offers the meeting
            "Sure! We help teams cut project delays by 40 percent.",  # answers the info request
        ]
    )
    await drain(engine.open())
    await drain(await engine.handle("what's this about?"))  # LLM offers the demo
    assert engine._meeting_offered is True
    reply = await engine.handle("yes, tell me more")
    assert reply.outcome is None
    assert reply.end_after is False
    assert engine.state == CallState.VALUE_PROP
    assert engine._booking_confirmed is False  # still not booked
    # A clean confirmation on the next turn DOES book (beat 1: confirm, no hangup yet).
    reply2 = await engine.handle("okay yes let's do it")
    assert reply2.end_after is False
    assert engine._booking_confirmed is True


async def test_opt_out_suppresses_and_ends(make_engine):
    engine, _m, compliance = make_engine()
    await drain(engine.open())
    reply = await engine.handle("please remove me from your list")
    assert reply.outcome == Outcome.OPTED_OUT
    assert reply.end_after is True
    await drain(reply)
    assert compliance.is_suppressed("+14155551234")


async def test_explicit_goodbye_ends_call(make_engine):
    engine, _m, _c = make_engine()
    await drain(engine.open())
    reply = await engine.handle("sorry, I gotta go, goodbye")
    assert reply.outcome == Outcome.NOT_INTERESTED
    assert reply.end_after is True


async def test_soft_not_interested_does_not_end(make_engine):
    # A soft "not interested" is left to the LLM to handle gracefully, not an instant hangup.
    engine, _m, _c = make_engine(llm_responses=["Totally fair, may I ask what you use today?"])
    await drain(engine.open())
    await drain(await engine.handle("hi"))
    reply = await engine.handle("eh, not interested honestly")
    await drain(reply)
    assert reply.end_after is False
    assert engine.state == CallState.VALUE_PROP


async def test_silence_does_not_advance_state(make_engine):
    engine, _m, _c = make_engine()
    await drain(engine.open())
    reply = await engine.handle("   ")
    await drain(reply)
    assert engine.state == CallState.GREETING  # unchanged


async def test_system_prompt_grounds_the_llm(make_engine):
    # The grounding prompt carries persona, goal, honesty rules, and the objection hints.
    engine, _m, _c = make_engine()
    prompt = engine._system_prompt()
    assert "15-minute demo" in prompt
    assert "AI" in prompt
    assert "not pushy" in prompt.lower() or "never be pushy" in prompt.lower()
    assert "too busy" in prompt  # an objection hint from agent.yaml
