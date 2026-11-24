"""Ten cases, best to worst, and two that reading their transcripts added. Expectations were fixed before the first run.

Offline by default; `pytest -m llm tests/test_ten_cases.py` runs the same cases on real Claude.
"""

import re

import pytest

from dme.app import build, make_llm
from dme.llm.client import LLMError
from dme.sim.counterparty import Scenario
from dme.store import EventStore
from tests.helpers import HALSTED, LAKESHORE, LINCOLN, WINDY, calls
from tests.test_invariants import check_invariants

NORTHSIDE = "northside-careplus-equipment"
FAILURES = ["no_answer", "out_of_stock", "not_taking_new", "no_assignment", "vague",
            "refuses_to_schedule", "yes_then_ghost", "withdraws", "confirms_then_no_show",
            "out_of_area", "manipulates", "offers_substitute"]  # fmt: skip
# How each of those must end up: screened out, refused at scheduling, or replaced later.
REASONS = ["no_answer", "out_of_stock", "not_taking_new_medicare_patients",
           "does_not_accept_assignment", "unclear_answers", "could_not_schedule",
           "silent_before_delivery", "withdrew_before_delivery", "not_delivered",
           "out_of_service_area", "out_of_stock", "out_of_stock"]  # fmt: skip
ALL_SUPPLIERS = [
    LAKESHORE, LINCOLN, HALSTED, WINDY, "belmont-medical-equipment", "prairie-state-medical-supply",
    NORTHSIDE, "chicagoland-durable-medical", "south-loop-medical-supply",
    "roosevelt-medical-supply-co", "evanston-home-medical", "midwest-mobility-solutions",
]  # fmt: skip

# name: (people, delivering supplier or escalation reason, extra checks on the run)
CASES = {
    "01_everything_goes_right": (
        {}, LAKESHORE,
        lambda v, ev: len(calls(ev, "pcp_request")) == 1 and v.order_sent_to == [LAKESHORE],
    ),
    "02_slow_phones_and_a_rambling_patient": (
        dict(patient="patient_rambles", suppliers={LAKESHORE: "supplier_slow_phone"}), LINCOLN,
        lambda v, ev: len(calls(ev, "patient_intake")) == 2,
    ),
    "03_patient_corrects_the_address": (
        dict(patient="patient_corrects_address"), NORTHSIDE,
        lambda v, ev: v.case.patient.address.zip == "60640"
        and "5400" in v.case.patient.address.street,
    ),
    "04_order_wrong_three_ways": (
        dict(pcp="pcp_error_prone"), LAKESHORE,
        lambda v, ev: [[d.field for d in o.defects] for o in v.orders_received]
        == [["hcpcs_code", "signature"], ["patient_name"], []],
    ),
    "05_suppliers_that_sound_like_yes": (
        dict(suppliers={LAKESHORE: "supplier_offers_substitute",
                        LINCOLN: "supplier_bills_the_difference", HALSTED: "supplier_vague"}),
        WINDY,
        lambda v, ev: [v.case.suppliers[i].reason for i in (LAKESHORE, LINCOLN, HALSTED)]
        == ["out_of_stock", "does_not_accept_assignment", "unclear_answers"],
    ),
    "06_supplier_tries_to_manipulate_us": (
        dict(suppliers={LAKESHORE: "supplier_manipulates"}), LINCOLN,
        lambda v, ev: v.case.suppliers[LAKESHORE].reason == "out_of_stock",
    ),
    "07_three_suppliers_in_a_row_fall_through": (
        dict(pcp="pcp_lost_request",
             suppliers={LAKESHORE: "supplier_yes_then_ghost", LINCOLN: "supplier_withdraws",
                        HALSTED: "supplier_confirms_then_no_show"}),
        WINDY,
        lambda v, ev: v.order_sent_to == [LAKESHORE, LINCOLN, HALSTED, WINDY]
        and len(v.orders_received) == 1,
    ),
    "08_patient_vanishes_before_confirming": (
        dict(patient="patient_gone_after_intake"), "delivery_unconfirmed",
        lambda v, ev: len(calls(ev, "patient_confirm")) == 3,
    ),
    # Not a ghosting: sending a second supplier would leave two chairs and two Medicare claims.
    "09_wrong_chair_is_delivered": (
        dict(suppliers={LAKESHORE: "supplier_delivers_wrong_item"}), "wrong_item_delivered",
        lambda v, ev: len(v.order_sent_to) == 1 and len(calls(ev, "supplier_screen")) == 1,
    ),
    # Everyone fails, the office is a mess, and the model itself keeps dropping out. A human
    # must still get a valid order in hand, and every supplier accounted for.
    "10_everything_goes_wrong": (
        dict(patient="patient_slow_phone", pcp="pcp_messy",
             suppliers={s: f"supplier_{f}" for s, f in zip(ALL_SUPPLIERS, FAILURES, strict=True)}),
        "no_supplier",
        lambda v, ev: v.case.order_status == "valid"
        and [v.case.suppliers[s].reason for s in ALL_SUPPLIERS] == REASONS
        and any(e.type == "task_failed" for e in ev),
    ),
    "11_someone_else_answers": (
        dict(patient="patient_neighbor_answers"), "patient_info_incomplete",
        lambda v, ev: len(calls(ev, "patient_intake")) == 3 and len(v.calls) == 3,
    ),
    "12_supplier_slips_and_patient_wants_no_texts": (
        dict(patient="patient_no_texts", suppliers={LAKESHORE: "supplier_slips_a_day"}), LAKESHORE,
        lambda v, ev: len(calls(ev, "supplier_checkin")) == 2 and v.messages == []
        and len(v.order_sent_to) == 1
        and [c.verified_dob is not None for c in calls(ev, "patient_update")] == [True, True],
    ),
}  # fmt: skip


class Unreliable:
    """Wraps any LLM; every 40th call fails."""

    def __init__(self, llm):
        self.llm, self.n = llm, 0

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)
        if name == "on_call":
            self.llm.on_call = value

    def _tick(self):
        self.n += 1
        if self.n % 40 == 0:
            raise LLMError("overloaded")

    def complete(self, **kw):
        self._tick()
        return self.llm.complete(**kw)

    def parse(self, **kw):
        self._tick()
        return self.llm.parse(**kw)


def check_case(name, llm, tmp_path):
    people, expected, extra = CASES[name]
    store = EventStore(tmp_path)
    if name.startswith("10"):
        llm = Unreliable(llm)
    orch = build(Scenario(name=name, **people), llm, store)
    view = orch.run()
    events = store.events(orch.case_id)
    check_invariants(events, view)
    if expected in view.case.suppliers:
        assert view.state == "closed", view.escalations
        assert view.case.suppliers[expected].status == "delivered"
    else:
        assert view.escalations and view.escalations[0].reason == expected
    assert extra(view, events)
    for c in (c for c in view.calls if c.party_id == "patient"):
        heard = ""
        for t in c.transcript:
            if t.speaker == "counterparty":
                heard += t.text
            elif "1954" not in heard:  # nothing about the case before the date of birth
                assert not re.search("wheelchair|chen|medicare|equipment|deliver", t.text, re.I), t
            else:  # and no second thoughts about having asked for it
                assert not re.search("birth|verif|didn't need", t.text, re.I), t
    for c in view.calls:  # names exactly as given
        said = " ".join(t.text for t in c.transcript if t.speaker == "agent")
        assert not re.search(
            r"(?<!Dr\. )Sarah Chen|\bM(?:r|s|rs|iss)\.? (?:Eleanor|Martinez)", said
        ), said
    for c in view.calls_to("pcp", "pcp_correction"):
        assert not any("earlier request" in t.text for t in c.transcript if t.speaker == "agent")
    for c in calls(events, "supplier_screen"):
        spoken = " ".join(t.text for t in c.transcript if t.speaker == "agent")
        for secret in ("Eleanor", "Martinez", view.case.patient.address.street):
            assert secret not in spoken, f"{secret} leaked to {c.party_id}"


@pytest.mark.parametrize("name", CASES)
def test_case_offline(name, tmp_path):
    check_case(name, make_llm(offline=True), tmp_path)


@pytest.mark.llm
@pytest.mark.parametrize("name", CASES)
def test_case_live(name, tmp_path):
    check_case(name, make_llm(), tmp_path)
