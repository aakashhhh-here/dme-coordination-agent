"""Step 4: call loop, extraction grounding, SDK wrapper. No network."""

import json
from datetime import date

import anthropic
import httpx2
import pytest

from dme.llm.call_agent import CallBrief, run_call
from dme.llm.client import ClaudeLLM, LLMError
from dme.llm.extractor import extract_call, extract_order, ground_order, quote_is_real
from dme.models import (
    Address,
    AddressFact,
    PatientCallOutcome,
    SupplierCallOutcome,
    SupplierStatus,
    Turn,
    WrittenOrder,
)
from dme.rules.screening import screen_supplier
from dme.sim.counterparty import SimCounterparty
from tests.fake_llm import FakeLLM
from tests.helpers import T0, good_order, no, said, yes

BRIEF = CallBrief(
    call_type="supplier_screen", party_id="s", party_name="Acme DME", goal="g",
    facts=["f"], questions=["q"], now=T0,
)  # fmt: skip


def person(llm, answers=True):
    return SimCounterparty(llm, "Acme", {}, "", answers, T0.date())


def test_call_runs_until_agent_ends_it():
    llm = FakeLLM(
        {
            "sim:counterparty": ["Acme, hello?", "Yes."],
            "agent:supplier_screen": ["In stock?", "Bye [END_CALL]"],
        }
    )
    turns = run_call(llm, BRIEF, person(llm))
    assert [t.text for t in turns] == ["Acme, hello?", "In stock?", "Yes.", "Bye"]
    agent_req = [r for r in llm.requests if r["purpose"].startswith("agent")][-1]
    assert [m["role"] for m in agent_req["messages"]] == ["user", "assistant", "user"]


def test_counterparty_can_hang_up():
    llm = FakeLLM({"sim:counterparty": ["Not interested. [HANG_UP]"]})
    assert [t.text for t in run_call(llm, BRIEF, person(llm))] == ["Not interested."]


def test_call_is_capped():
    llm = FakeLLM({"sim:counterparty": ["hm"] * 9, "agent:supplier_screen": ["so?"] * 9})
    assert len(run_call(llm, BRIEF, person(llm), max_turns=3)) == 6


def test_ringing_out_uses_no_model():
    llm = FakeLLM({})
    assert run_call(llm, BRIEF, person(llm, answers=False)) == []
    assert extract_call(llm, [], SupplierCallOutcome, T0).connected is False
    assert llm.requests == []


def extract(outcome, *lines):
    return extract_call(
        FakeLLM({"extract:supplier_screen": [outcome]}), said(*lines), SupplierCallOutcome, T0
    )


def four(**kw):
    base = dict(
        taking_new_medicare_patients=yes("we take new Medicare patients"),
        has_k0001_in_stock=yes("we have K0001"),
        accepts_medicare_assignment=yes("we accept assignment"),
        serves_patient_area=yes("we deliver there"),
    )
    return SupplierCallOutcome(connected=True, **(base | kw))


LINES = (
    "We take new Medicare patients, and we have K0001.",
    "We accept assignment. We deliver there.",
)


def test_extraction_maps_to_qualified_and_declined():
    assert screen_supplier(extract(four(), *LINES))[0] is SupplierStatus.QUALIFIED
    out = extract(four(has_k0001_in_stock=no("no K0001 right now")), *LINES, "No K0001 right now.")
    assert screen_supplier(out) == (SupplierStatus.DECLINED, "out_of_stock")


def test_invented_quote_cannot_qualify_a_supplier():
    out = extract(four(has_k0001_in_stock=yes("plenty of K0001 chairs here")), *LINES)
    assert out.has_k0001_in_stock.confidence == "low"
    assert screen_supplier(out)[0] is SupplierStatus.NO_ANSWER


def test_agents_own_words_are_not_evidence():
    turns = [
        Turn(speaker="agent", text="So you have K0001."),
        Turn(speaker="counterparty", text="Hmm."),
    ]
    assert not quote_is_real("you have K0001", turns)
    assert not quote_is_real("", turns)


def test_quote_matches_whole_words_and_ellipses():
    turns = said("We know stock is low", "but we can do it")
    assert not quote_is_real("no", turns)  # 'know' must not count as 'no'
    assert quote_is_real("stock is low... we can do it", turns)


def test_silent_line_counts_as_not_connected():
    assert extract(four(), "").connected is False


@pytest.mark.parametrize("zip_, ok", [("60614", True), ("60641", False), ("606", False)])
def test_zip_must_have_been_spoken(zip_, ok):
    quote = "2230 N Halsted St, Chicago 60614"
    out = PatientCallOutcome(
        connected=True,
        address=AddressFact(
            value=Address(street="2230 N Halsted St", city="Chicago", state="IL", zip=zip_),
            confidence="high", evidence_quote=quote,
        ),
    )  # fmt: skip
    llm = FakeLLM({"extract:patient_intake": [out]})
    assert extract_call(llm, said(f"It's {quote}."), PatientCallOutcome, T0).address.known is ok


def test_order_fields_not_in_document_are_dropped():
    doc = "Eleanor Martinez, Sarah Chen MD, code K0003, NPI: 1234567893"
    order = ground_order(good_order(), doc)
    assert order.hcpcs_code is None  # the model "corrected" K0003 to K0001
    assert order.prescriber_npi == "1234567893" and order.patient_name
    assert extract_order(FakeLLM({"extract:written_order": [good_order()]}), doc).hcpcs_code is None


# --- the real SDK wrapper against a fake HTTP server ------------------------


def sdk(handler):
    transport = httpx2.MockTransport(handler)
    client = anthropic.Anthropic(
        api_key="test", max_retries=0, http_client=anthropic.DefaultHttpxClient(transport=transport)
    )
    return ClaudeLLM(client)


def reply(text, stop="end_turn", status=200):
    body = {
        "id": "m", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": text}], "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }  # fmt: skip
    return httpx2.Response(status, json=body)


MSG = [{"role": "user", "content": "hi"}]


def test_sdk_complete_sends_valid_request_and_logs_usage():
    seen, logged = [], []
    llm = sdk(lambda r: (seen.append(json.loads(r.content)), reply("hello"))[1])
    llm.on_call = logged.append
    assert llm.complete(purpose="p", system="s", messages=MSG) == "hello"
    assert seen[0]["model"] == "claude-opus-5-5"
    assert seen[0]["output_config"] == {"effort": "medium"}
    assert not {"temperature", "thinking", "tool_choice"} & set(seen[0])  # 400s on this model
    assert (logged[0].purpose, logged[0].input_tokens, logged[0].output_tokens) == ("p", 10, 5)


@pytest.mark.parametrize("schema", [SupplierCallOutcome, PatientCallOutcome, WrittenOrder])
def test_sdk_parse_sends_strict_schema(schema):
    seen = []
    sample = good_order() if schema is WrittenOrder else schema(connected=True)
    llm = sdk(lambda r: (seen.append(json.loads(r.content)), reply(sample.model_dump_json()))[1])
    assert llm.parse(purpose="p", system="s", messages=MSG, schema=schema) == sample
    config = seen[0]["output_config"]
    assert config["effort"] == "medium" and config["format"]["type"] == "json_schema"
    root = config["format"]["schema"]
    for obj in [root, *root.get("$defs", {}).values()]:
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])


@pytest.mark.parametrize(
    "handler, fatal",
    [
        (lambda r: reply("", stop="refusal"), False),
        (lambda r: reply("cut of", stop="max_tokens"), False),
        (lambda r: httpx2.Response(529, json={"error": {}}), False),
        (lambda r: httpx2.Response(401, json={"error": {}}), True),
        (lambda r: httpx2.Response(400, json={"error": {}}), True),
    ],
)
def test_sdk_failures_become_llm_errors(handler, fatal):
    with pytest.raises(LLMError) as e:
        sdk(handler).complete(purpose="p", system="s", messages=MSG)
    assert e.value.fatal is fatal


def test_sdk_garbage_json_becomes_llm_error():
    with pytest.raises(LLMError):
        sdk(lambda r: reply('{"quantity": "lots"}')).parse(
            purpose="p", system="s", messages=MSG, schema=WrittenOrder
        )


def test_document_dates_parse():
    assert WrittenOrder(order_date="2026-11-23").order_date == date(2026, 11, 23)
