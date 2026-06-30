"""ConversationEngine state machine tests. Assert on outcomes and transitions, not timing."""

from __future__ import annotations

from voiceagent.contracts import CallState, Outcome
from tests.conftest import drain


async def test_greeting_includes_disclosure_and_name(make_engine):
    engine, _meter, _comp = make_engine()
    greeting = await drain(engine.open())
    assert engine.state == CallState.GREETING
    assert "AI assistant" in greeting
    assert "John Smith" in greeting


async def test_greeting_to_value_prop(make_engine):
    engine, _m, _c = make_engine()
    await drain(engine.open())
    text = await drain(await engine.handle("yeah sure, who is this?"))
    assert engine.state == CallState.VALUE_PROP
    assert "40%" in text


async def test_objection_handled_and_loops_back(make_engine):
    engine, _m, _c = make_engine()
    await drain(engine.open())
    await drain(await engine.handle("hi"))
    reply = await engine.handle("eh we're too busy right now")
    text = await drain(reply)
    assert reply.intent.intent.value == "objection"
    assert engine.objection_count == 1
    assert "2-minute video" in text
    assert engine.state == CallState.VALUE_PROP  # looped back to the pitch


async def test_booking_books_meeting_and_ends(make_engine):
    engine, _m, _c = make_engine()
    await drain(engine.open())
    await drain(await engine.handle("hi"))
    reply = await engine.handle("okay sounds good, let's do it")
    assert reply.outcome == Outcome.BOOKED_MEETING
    assert reply.end_after is True
    text = await drain(reply)
    assert "Tuesday at 2pm" in text and "john@example.com" in text


async def test_opt_out_suppresses_and_ends(make_engine):
    engine, _m, compliance = make_engine()
    await drain(engine.open())
    reply = await engine.handle("please remove me from your list")
    assert reply.outcome == Outcome.OPTED_OUT
    assert reply.end_after is True
    await drain(reply)
    assert compliance.is_suppressed("+14155551234")


async def test_objection_limit_ends_not_interested(make_engine):
    engine, _m, _c = make_engine(llm_responses=["a", "b", "c"])
    await drain(engine.open())
    await drain(await engine.handle("hi"))
    await drain(await engine.handle("honestly we're too busy"))
    await drain(await engine.handle("we already have something in place"))
    reply = await engine.handle("still too busy")  # 3rd objection, over max of 2
    assert reply.outcome == Outcome.NOT_INTERESTED
    assert engine.state == CallState.END


async def test_end_signal_ends_call(make_engine):
    engine, _m, _c = make_engine()
    await drain(engine.open())
    reply = await engine.handle("goodbye")
    assert reply.outcome == Outcome.NOT_INTERESTED
    assert reply.end_after is True


async def test_silence_does_not_advance_state(make_engine):
    engine, _m, _c = make_engine()
    await drain(engine.open())
    reply = await engine.handle("   ")
    await drain(reply)
    assert engine.state == CallState.GREETING  # unchanged
