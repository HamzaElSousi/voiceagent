"""Real booking during the call: slot helpers, spoken-email capture, the BookingFlow conversation
through the engine and session, and the Cal.com client against a mocked HTTP transport.
Zero network: FakeBooker and httpx.MockTransport stand in for the calendar."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from tests.conftest import drain
from tests.fakes import FakeBooker, FakeLLM, FakeMediaStream, FakeSTT, FakeTTS
from voiceagent.booking.calcom import CalcomBooker
from voiceagent.booking.email import parse_spoken_email, speak_email
from voiceagent.booking.slots import choose_offer, is_rejection, resolve_choice, speak_slot
from voiceagent.compliance import ComplianceGate
from voiceagent.config import AgentConfig, AgentConfigError
from voiceagent.contracts import BookingRequest, CallState, CostRates, Outcome, Slot
from voiceagent.cost import DollarCostMeter
from voiceagent.engine import ConversationEngine
from voiceagent.intent import IntentClassifier
from voiceagent.pipeline.assembled import AssembledPipeline
from voiceagent.session import CallSession

TZ = "America/New_York"
NY = ZoneInfo(TZ)
# Monday 2026-10-19, 08:00 New York.
NOW = datetime(2026, 10, 19, 8, 0, tzinfo=NY)


def at(day_offset: int, hour: int, minute: int = 0) -> Slot:
    start = (NOW + timedelta(days=day_offset)).replace(hour=hour, minute=minute)
    return Slot(start=start, end=start + timedelta(minutes=15))


TUE_10 = at(1, 10)
TUE_14 = at(1, 14)
WED_15 = at(2, 15, 30)
THU_9 = at(3, 9)
SLOTS = [TUE_10, TUE_14, WED_15, THU_9]

OFFER = "Would you be open to a quick 15-minute demo this week?"


# --------------------------------------------------------------------------------------------
# Slot helpers
# --------------------------------------------------------------------------------------------


def test_speak_slot_is_natural():
    assert speak_slot(at(0, 15), TZ, NOW) == "today at 3 PM"
    assert speak_slot(TUE_10, TZ, NOW) == "tomorrow at 10 AM"
    assert speak_slot(WED_15, TZ, NOW) == "Wednesday at 3:30 PM"
    assert speak_slot(at(0, 12), TZ, NOW) == "today at noon"
    assert speak_slot(at(9, 9), TZ, NOW) == "Wednesday the 28th at 9 AM"


def test_speak_slot_uses_listener_timezone():
    # 10 AM New York is 7 AM Los Angeles.
    assert speak_slot(TUE_10, "America/Los_Angeles", NOW) == "tomorrow at 7 AM"


def test_choose_offer_spreads_options():
    picked = choose_offer([TUE_10, at(1, 10, 30), at(1, 11), TUE_14], 2)
    assert picked == [TUE_10, TUE_14]  # 10:30 and 11:00 are too close to 10:00 to be a real choice


def test_choose_offer_excludes_already_offered():
    assert choose_offer(SLOTS, 2, exclude=[TUE_10, TUE_14]) == [WED_15, THU_9]


@pytest.mark.parametrize(
    "reply, expected",
    [
        ("the first one", 0),
        ("the second one works", 1),
        ("tomorrow at 10 is good", 0),
        ("ten works for me", 0),
        ("2 pm please", 1),
        ("the later one", 1),
        ("wednesday", None),        # not one of the offered slots
        ("tomorrow", None),         # both offered slots are tomorrow: ambiguous, do not guess
        ("hmm let me think", None),
        ("neither of those works", None),
    ],
)
def test_resolve_choice(reply, expected):
    assert resolve_choice(reply, [TUE_10, TUE_14], TZ, NOW) == expected


def test_resolve_choice_by_weekday():
    assert resolve_choice("wednesday works", [TUE_10, WED_15], TZ, NOW) == 1
    assert resolve_choice("three thirty on wednesday", [TUE_10, WED_15], TZ, NOW) == 1


def test_pronoun_one_is_not_one_oclock():
    one_pm = at(1, 13)
    assert resolve_choice("the second one", [TUE_10, one_pm], TZ, NOW) == 1
    assert resolve_choice("one pm", [TUE_10, one_pm], TZ, NOW) == 1


def test_is_rejection():
    assert is_rejection("neither of those works for me")
    assert is_rejection("do you have another day")
    assert not is_rejection("the first one works")


# --------------------------------------------------------------------------------------------
# Spoken email
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "heard, expected",
    [
        ("john at gmail dot com", "john@gmail.com"),
        ("it's john dot smith at acme dot co dot uk", "john.smith@acme.co.uk"),
        ("my email is j o h n at gmail dot com", "john@gmail.com"),
        ("sure, jane_doe@example.com", "jane_doe@example.com"),
        ("jane underscore doe at example dot com", "jane_doe@example.com"),
        ("I don't really want to give that out", None),
    ],
)
def test_parse_spoken_email(heard, expected):
    assert parse_spoken_email(heard) == expected


def test_speak_email_spells_local_part():
    assert speak_email("jo.smith@gmail.com") == "J-O dot S-M-I-T-H at gmail dot com"


# --------------------------------------------------------------------------------------------
# Engine + BookingFlow
# --------------------------------------------------------------------------------------------


@pytest.fixture
def make_booking_engine(cfg, dnc_path, contact):
    def _make(booker, llm_responses=None, contact_overrides=None, config=None):
        c = dict(contact, **(contact_overrides or {}))
        conf = config or cfg
        meter = DollarCostMeter(per_call_cap_usd=1.0, rates=CostRates())
        engine = ConversationEngine(
            conf,
            FakeLLM(llm_responses or [OFFER]),
            IntentClassifier(conf.script, conf.compliance),
            ComplianceGate(conf.compliance, dnc_path=dnc_path),
            c,
            cost_meter=meter,
            booker=booker,
        )
        engine.booking._now = lambda: NOW
        engine.prepare()
        return engine

    return _make


async def _agree(engine) -> str:
    await drain(engine.open())
    await drain(await engine.handle("yeah sure, who is this"))  # LLM offers the demo
    reply = await engine.handle("okay sounds good, let's do it")
    assert reply.state == CallState.BOOKING
    return await drain(reply)


async def test_agreement_offers_real_slots(make_booking_engine):
    engine = make_booking_engine(FakeBooker(SLOTS))
    offer = await _agree(engine)
    assert offer == "Great. I have tomorrow at 10 AM or tomorrow at 2 PM. Which works better for you?"
    assert engine._booking_confirmed is False  # nothing booked yet


async def test_choice_books_the_slot_and_closes(make_booking_engine):
    booker = FakeBooker(SLOTS)
    engine = make_booking_engine(booker)
    await _agree(engine)
    reply = await engine.handle("the second one works")
    text = await drain(reply)
    assert "let me lock that in" in text
    assert "You're all set for tomorrow at 2 PM" in text
    assert reply.end_after is False  # two-beat close: invite a last word first
    assert booker.requests[0].slot == TUE_14
    assert booker.requests[0].email == "john@example.com"
    assert engine.booking.booking_ref == "bk_1"
    bye = await engine.handle("nope, that's everything")
    assert bye.end_after is True and bye.outcome == Outcome.BOOKED_MEETING


async def test_rejection_offers_new_times(make_booking_engine):
    engine = make_booking_engine(FakeBooker(SLOTS))
    await _agree(engine)
    text = await drain(await engine.handle("neither of those works"))
    assert "Wednesday at 3:30 PM" in text and "Thursday at 9 AM" in text
    assert "tomorrow" not in text  # never re-offers the same times


async def test_question_mid_choice_goes_to_llm_with_slots_in_prompt(make_booking_engine):
    llm = FakeLLM([OFFER, "It's a quick screen share. Which of those times works?"])
    engine = make_booking_engine(FakeBooker(SLOTS))
    engine.llm = llm
    await _agree(engine)
    text = await drain(await engine.handle("what happens on the call?"))
    assert "screen share" in text
    assert engine.booking.active  # still choosing


async def test_slot_taken_offers_next(make_booking_engine):
    booker = FakeBooker(SLOTS, taken=[TUE_10])
    engine = make_booking_engine(booker)
    await _agree(engine)
    text = await drain(await engine.handle("the first one"))
    assert "someone just grabbed that one" in text
    assert "Wednesday at 3:30 PM" in text
    text = await drain(await engine.handle("yes that works"))
    assert "You're all set for Wednesday at 3:30 PM" in text
    assert engine.booking.booked_slot == WED_15


async def test_calendar_error_keeps_the_agreement(make_booking_engine):
    engine = make_booking_engine(FakeBooker(SLOTS, fail=True))
    await _agree(engine)
    text = await drain(await engine.handle("the first one"))
    assert "I'll email you to confirm tomorrow at 10 AM" in text
    assert engine._booking_confirmed is True
    assert engine.booking.booking_ref is None


async def test_no_slots_falls_back_to_link(make_booking_engine):
    engine = make_booking_engine(FakeBooker([]))
    offer = await _agree(engine)
    assert "send you a link" in offer
    assert engine._booking_confirmed is True


async def test_missing_email_is_captured_and_read_back(make_booking_engine):
    booker = FakeBooker(SLOTS)
    engine = make_booking_engine(booker, contact_overrides={"email": ""})
    await _agree(engine)
    text = await drain(await engine.handle("the first one"))
    assert "Tomorrow at 10 AM it is" in text and "email" in text
    text = await drain(await engine.handle("it's sam at acme dot com"))
    assert "S-A-M at acme dot com" in text
    text = await drain(await engine.handle("yes that's right"))
    assert "You're all set" in text
    assert booker.requests[0].email == "sam@acme.com"


async def test_email_correction(make_booking_engine):
    booker = FakeBooker(SLOTS)
    engine = make_booking_engine(booker, contact_overrides={"email": ""})
    await _agree(engine)
    await drain(await engine.handle("the first one"))
    await drain(await engine.handle("sam at acme dot com"))
    text = await drain(await engine.handle("no, it's pam at acme dot com"))
    assert "P-A-M" in text
    await drain(await engine.handle("yes"))
    assert booker.requests[0].email == "pam@acme.com"


async def test_opt_out_still_wins_during_booking(make_booking_engine):
    engine = make_booking_engine(FakeBooker(SLOTS))
    await _agree(engine)
    reply = await engine.handle("actually remove me from your list")
    await drain(reply)
    assert reply.outcome == Outcome.OPTED_OUT


async def test_without_booker_legacy_booking_is_unchanged(make_engine):
    engine, _m, _c = make_engine(llm_responses=[OFFER])
    await drain(engine.open())
    await drain(await engine.handle("yeah sure, who is this"))
    text = await drain(await engine.handle("okay sounds good, let's do it"))
    assert "Anything else before I let you go?" in text
    assert engine._booking_confirmed is True


async def test_booking_hint_is_in_prompt_when_booker_configured(make_booking_engine):
    engine = make_booking_engine(FakeBooker(SLOTS))
    assert "never promise a specific day or time" in engine.booking.context()


# --------------------------------------------------------------------------------------------
# Inbound mode
# --------------------------------------------------------------------------------------------


def _inbound_cfg(cfg) -> AgentConfig:
    raw = cfg.model_dump(mode="json")
    raw["mode"] = "inbound"
    return AgentConfig.from_dict(raw)


async def test_inbound_prompt_is_a_receptionist(make_booking_engine, cfg):
    engine = make_booking_engine(FakeBooker(SLOTS), config=_inbound_cfg(cfg))
    prompt = engine._system_prompt()
    assert "answering an inbound phone call" in prompt
    assert "how you got their number" not in prompt
    assert "outbound" not in prompt


async def test_outbound_prompt_keeps_contact_list_answer(make_booking_engine):
    prompt = make_booking_engine(FakeBooker(SLOTS))._system_prompt()
    assert "how you got their number" in prompt


def test_inbound_example_config_loads():
    from pathlib import Path

    from voiceagent.config import load_agent_config

    cfg = load_agent_config(Path(__file__).resolve().parents[1] / "examples" / "inbound.yaml")
    assert cfg.mode == "inbound"


def test_calcom_config_requires_event_type(cfg):
    raw = cfg.model_dump(mode="json")
    raw["booking"] = {"provider": "calcom"}
    with pytest.raises(AgentConfigError, match="event_type"):
        AgentConfig.from_dict(raw)


def test_config_survives_json_round_trip(cfg):
    # Configs stored in the DB / sent over the API come back with "09:00:00" times.
    again = AgentConfig.from_dict(cfg.model_dump(mode="json"))
    assert again.compliance.calling_hours == cfg.compliance.calling_hours


def test_from_dict_rejects_secrets(cfg):
    raw = cfg.model_dump(mode="json")
    raw["booking"]["api_key"] = "cal_live_123"
    with pytest.raises(AgentConfigError, match="Secrets must live"):
        AgentConfig.from_dict(raw)


# --------------------------------------------------------------------------------------------
# Session: booking recorded on the result, voicemail detection
# --------------------------------------------------------------------------------------------


def _session(cfg, dnc_path, contact, turns, booker=None, silence_timeout=0.3):
    meter = DollarCostMeter(per_call_cap_usd=1.0, rates=CostRates())
    engine = ConversationEngine(
        cfg,
        FakeLLM([OFFER]),
        IntentClassifier(cfg.script, cfg.compliance),
        ComplianceGate(cfg.compliance, dnc_path=dnc_path),
        contact,
        cost_meter=meter,
        booker=booker,
    )
    if engine.booking is not None:
        engine.booking._now = lambda: NOW
    stt = FakeSTT.from_turns(turns, include_speech_start=False)
    tts = FakeTTS()
    pipeline = AssembledPipeline(stt, tts, cost_meter=meter)
    session = CallSession(
        engine,
        pipeline,
        FakeMediaStream(inbound_frames=[b"\xff" * 160] * 4),
        meter,
        contact=contact,
        silence_timeout=silence_timeout,
        max_silences=1,
        events_dir=None,
        prewarm_llm=False,
    )
    return session, tts


async def test_session_records_booking_ref(cfg, dnc_path, contact):
    booker = FakeBooker(SLOTS)
    session, _tts = _session(
        cfg,
        dnc_path,
        contact,
        ["yeah who is this", "sounds good let's do it", "the first one", "no that's all"],
        booker=booker,
    )
    result = await session.run()
    assert result.outcome == Outcome.BOOKED_MEETING
    assert result.booking_ref == "bk_1"
    assert result.booked_slot == TUE_10.start.isoformat()
    assert booker.slot_queries == 1  # prefetched once at call start


async def test_machine_detected_leaves_voicemail(cfg, dnc_path, contact):
    session, tts = _session(cfg, dnc_path, contact, [], silence_timeout=5.0)
    session.notify_machine("machine_end_beep")
    result = await session.run()
    assert result.outcome == Outcome.VOICEMAIL
    assert "left a message" in result.end_reason
    assert any("Sorry I missed you" in s for s in tts.spoken)


async def test_machine_detected_hang_up(cfg, dnc_path, contact):
    session, tts = _session(cfg, dnc_path, contact, [], silence_timeout=5.0)
    session.voicemail_action = "hang_up"
    session.notify_machine()
    result = await session.run()
    assert result.outcome == Outcome.VOICEMAIL
    assert not any("Sorry I missed you" in s for s in tts.spoken)


# --------------------------------------------------------------------------------------------
# Cal.com client (mocked HTTP)
# --------------------------------------------------------------------------------------------


def _calcom(handler) -> CalcomBooker:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CalcomBooker("cal_test_key", event_type_id=42, client=client)


async def test_calcom_get_slots_parses_and_sends_headers():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["version"] = request.headers["cal-api-version"]
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "2026-10-21": [{"start": "2026-10-21T18:00:00.000Z"}],
                    "2026-10-20": [{"start": "2026-10-20T14:00:00.000Z"}],
                },
            },
        )

    booker = _calcom(handler)
    start = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
    slots = await booker.get_slots(start, start + timedelta(days=7), TZ)
    assert [s.start.isoformat() for s in slots] == [
        "2026-10-20T14:00:00+00:00",
        "2026-10-21T18:00:00+00:00",
    ]
    assert seen["auth"] == "Bearer cal_test_key"
    assert seen["version"] == "2024-09-04"
    assert seen["params"]["eventTypeId"] == "42"
    assert seen["params"]["timeZone"] == TZ


async def test_calcom_get_slots_returns_empty_on_error():
    booker = _calcom(lambda r: httpx.Response(401, json={"status": "error"}))
    start = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
    assert await booker.get_slots(start, start + timedelta(days=1), TZ) == []


def _request() -> BookingRequest:
    return BookingRequest(
        slot=Slot(start=datetime(2026, 10, 20, 14, tzinfo=timezone.utc)),
        name="John Smith",
        email="john@example.com",
        timezone=TZ,
        phone="+15555550100",
    )


async def test_calcom_create_booking_success():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["version"] = request.headers["cal-api-version"]
        return httpx.Response(201, json={"status": "success", "data": {"uid": "bk_abc"}})

    result = await _calcom(handler).create_booking(_request())
    assert result.ok and result.booking_ref == "bk_abc"
    assert seen["version"] == "2024-08-13"
    assert seen["body"]["start"] == "2026-10-20T14:00:00.000Z"
    assert seen["body"]["eventTypeId"] == 42
    assert seen["body"]["attendee"]["email"] == "john@example.com"
    assert seen["body"]["attendee"]["phoneNumber"] == "+15555550100"


async def test_calcom_create_booking_slot_taken():
    def handler(request):
        return httpx.Response(
            400,
            json={"status": "error", "error": {"message": "User either already has booking at this time or is not available"}},
        )

    result = await _calcom(handler).create_booking(_request())
    assert not result.ok and result.slot_taken


async def test_calcom_create_booking_network_error_does_not_raise():
    def handler(request):
        raise httpx.ConnectError("down")

    result = await _calcom(handler).create_booking(_request())
    assert not result.ok and not result.slot_taken


def test_calcom_requires_event_type():
    with pytest.raises(ValueError):
        CalcomBooker("k")
