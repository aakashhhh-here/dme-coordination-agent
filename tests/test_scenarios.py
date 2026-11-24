"""Steps 5-11: whole cases on the simulated clock, offline."""

import re
import subprocess
import sys
from datetime import date

import pytest

from dme import config
from dme.app import build
from dme.clock import is_business_day
from dme.models import OrderStatus, SupplierStatus
from dme.sim.counterparty import Scenario
from dme.sim.offline import OfflineLLM
from dme.store import EventStore
from tests.helpers import HALSTED, LAKESHORE, LINCOLN, WINDY, at, calls, run, states

S = SupplierStatus
ALL_SCENARIOS = sorted(p.stem for p in config.SCENARIOS_DIR.glob("*.yaml"))


def reason(view):
    return view.escalations[0].reason if view.escalations else None


# --- Step 5: patient -----------------------------------------------------------


def test_cooperative_patient_moves_case_to_working_with_zip(tmp_path):
    view, events = run(tmp_path)
    assert view.case.patient.address.zip == "60614"
    assert states(events)[1] == "working"


def test_unreachable_patient_escalates_after_n_days(tmp_path):
    view, events = run(tmp_path, patient="patient_unreachable")
    tries = calls(events, "patient_intake")
    assert reason(view) == "patient_unreachable"
    assert [c.at for c in tries] == [at(23), at(24, 12), at(25, 15)]  # different times of day
    assert not [e for e in events if e.type == "llm_called"]


@pytest.mark.parametrize(
    "persona, why",
    [
        ("patient_declines", "patient_declined"),
        ("patient_unknown_zip", "zip_unknown"),
        ("patient_far_away", "no_supplier"),
    ],
)
def test_other_patient_dead_ends(tmp_path, persona, why):
    assert reason(run(tmp_path, patient=persona)[0]) == why


# --- Step 6: sourcing ----------------------------------------------------------


def test_third_supplier_chosen_after_no_answer_and_out_of_stock(tmp_path):
    view, events = run(
        tmp_path, suppliers={LAKESHORE: "supplier_no_answer", LINCOLN: "supplier_out_of_stock"}
    )
    final = {r.supplier_id: (r.status, r.reason) for r in view.case.suppliers.values()}
    assert view.state == "closed"
    assert final[LAKESHORE][0] is S.NO_ANSWER
    assert final[LINCOLN] == (S.DECLINED, "out_of_stock")
    assert final[HALSTED][0] is S.DELIVERED
    assert [c.party_id for c in calls(events, "supplier_screen")] == [LAKESHORE, LINCOLN, HALSTED]
    assert all(
        s is S.UNCONTACTED for i, (s, _) in final.items() if i not in (LAKESHORE, LINCOLN, HALSTED)
    )


def test_no_answer_gets_one_callback_after_four_hours_then_is_skipped(tmp_path):
    view, events = run(
        tmp_path,
        default_supplier="supplier_not_taking_new",
        suppliers={LAKESHORE: "supplier_no_answer"},
    )
    got = [c.at for c in calls(events, "supplier_screen") if c.party_id == LAKESHORE]
    assert got == [at(23, 9, 15), at(23, 13, 15)]
    assert reason(view) == "no_supplier"


def test_callback_can_win(tmp_path):
    view, _ = run(
        tmp_path,
        default_supplier="supplier_not_taking_new",
        suppliers={LAKESHORE: "supplier_slow_phone"},
    )
    assert view.state == "closed" and view.case.suppliers[LAKESHORE].status is S.DELIVERED


def test_dropped_callback_is_honoured_after_a_ghosting(tmp_path):
    """Lakeshore missed our call, Lincoln qualified first (callback dropped), then ghosted.
    Everyone else declines: Lakeshore must be tried again, not written off."""
    view, _ = run(
        tmp_path,
        default_supplier="supplier_not_taking_new",
        suppliers={LAKESHORE: "supplier_slow_phone", LINCOLN: "supplier_yes_then_ghost"},
    )
    assert view.case.suppliers[LAKESHORE].status is S.DELIVERED
    assert view.state == "closed"


def test_vague_supplier_never_qualifies(tmp_path):
    view, _ = run(tmp_path, suppliers={LAKESHORE: "supplier_vague"})
    rec = view.case.suppliers[LAKESHORE]
    assert (rec.status, rec.reason) == (S.NO_ANSWER, "unclear_answers")
    assert view.state == "closed"


def test_screening_call_reveals_only_the_zip(tmp_path):
    seen = []

    class Spy(OfflineLLM):
        def complete(self, *, purpose, system, messages, role="agent"):
            if purpose == "agent:supplier_screen":
                seen.append(system)
            return super().complete(purpose=purpose, system=system, messages=messages, role=role)

    run(tmp_path, llm=Spy())
    assert seen and "60614" in seen[0]
    assert "Eleanor" not in seen[0] and "Halsted St" not in seen[0]


# --- Steps 7 and 8: order --------------------------------------------------------


def test_cooperative_pcp_sends_on_first_contact(tmp_path):
    _, events = run(tmp_path)
    assert len(calls(events, "pcp_request")) == 1 and not calls(events, "pcp_followup")


def test_lost_request_recovered_by_first_followup_on_day_two(tmp_path):
    view, events = run(tmp_path, pcp="pcp_lost_request")
    (first,), (followup,) = calls(events, "pcp_request"), calls(events, "pcp_followup")
    assert (first.at.date(), followup.at.date()) == (date(2026, 11, 23), date(2026, 11, 25))
    assert view.state == "closed"


def test_wrong_code_is_corrected(tmp_path):
    view, events = run(tmp_path, pcp="pcp_sends_wrong_code")
    checks = [[d.field for d in e.defects] for e in events if e.type == "order_received"]
    assert checks == [["hcpcs_code"], []]
    assert len(calls(events, "pcp_correction")) == 1 and not calls(events, "pcp_followup")
    assert view.state == "closed" and view.case.written_order.hcpcs_code == "K0001"


def test_correction_call_names_the_defect(tmp_path):
    seen = []

    class Spy(OfflineLLM):
        def complete(self, *, purpose, system, messages, role="agent"):
            if purpose == "agent:pcp_correction":
                seen.append(system)
            return super().complete(purpose=purpose, system=system, messages=messages, role=role)

    run(tmp_path, llm=Spy(), pcp="pcp_sends_wrong_code")
    assert "billing code K0003 is not right" in seen[0]


def test_unsigned_order_is_corrected(tmp_path):
    view, events = run(tmp_path, pcp="pcp_forgets_signature")
    checks = [[d.field for d in e.defects] for e in events if e.type == "order_received"]
    assert checks == [["signature"], []] and view.state == "closed"


@pytest.mark.parametrize(
    "persona, why, kind, n",
    [
        ("pcp_never_sends", "order_stalled", "pcp_followup", 2),
        ("pcp_always_wrong", "order_defective", "pcp_correction", 2),
        ("pcp_refuses", "order_refused", "pcp_request", 1),
        ("pcp_no_answer", "order_stalled", "pcp_request", 3),
    ],
)
def test_pcp_dead_ends_escalate_after_bounded_tries(tmp_path, persona, why, kind, n):
    view, events = run(tmp_path, pcp=persona)
    assert reason(view) == why and len(calls(events, kind)) == n
    assert "ready_to_match" not in states(events)
    assert not calls(events, "supplier_schedule")


# --- Step 9: delivery --------------------------------------------------------------


def test_ghosting_detected_day_before_and_recovered(tmp_path):
    view, events = run(tmp_path, suppliers={LAKESHORE: "supplier_yes_then_ghost"})
    ghost = next(e for e in events if e.type == "supplier_updated" and e.status is S.GHOSTED)
    assert (ghost.supplier_id, ghost.at, ghost.reason) == (
        LAKESHORE,
        at(24, 12),
        "silent_before_delivery",
    )
    assert view.case.suppliers[LINCOLN].status is S.DELIVERED and view.state == "closed"
    assert len(calls(events, "pcp_request")) == 1  # the valid order is kept


def test_no_show_caught_by_patient_call(tmp_path):
    view, events = run(tmp_path, suppliers={LAKESHORE: "supplier_confirms_then_no_show"})
    assert view.case.suppliers[LAKESHORE].reason == "not_delivered"
    assert view.case.suppliers[LINCOLN].status is S.DELIVERED
    assert states(events).count("working") == 2


def test_supplier_that_cannot_schedule_is_replaced(tmp_path):
    view, _ = run(tmp_path, suppliers={LAKESHORE: "supplier_refuses_to_schedule"})
    assert view.case.suppliers[LAKESHORE].reason == "could_not_schedule"
    assert view.case.suppliers[LINCOLN].status is S.DELIVERED


def test_supplier_that_withdraws_at_checkin_is_replaced(tmp_path):
    view, _ = run(tmp_path, suppliers={LAKESHORE: "supplier_withdraws"})
    assert view.case.suppliers[LAKESHORE].reason == "withdrew_before_delivery"
    assert view.case.suppliers[LINCOLN].status is S.DELIVERED


def test_everyone_ghosts_escalates(tmp_path):
    view, _ = run(tmp_path, default_supplier="supplier_yes_then_ghost")
    assert reason(view) == "no_supplier"
    assert all(r.status is S.GHOSTED for r in view.case.suppliers.values())


# --- Step 11: demo scenarios ---------------------------------------------------------


@pytest.mark.parametrize("name", ALL_SCENARIOS)
def test_demo_scenario_ends_as_declared(tmp_path, name):
    sc = Scenario.load(name)
    view = build(sc, OfflineLLM(), EventStore(tmp_path)).run()
    assert (view.state.value, reason(view)) == (sc.expect_state, sc.expect_reason)


def test_messy_scenario_really_contains_the_mess(tmp_path):
    sc = Scenario.load("messy_but_recovers")
    store = EventStore(tmp_path)
    orch = build(sc, OfflineLLM(), store)
    view = orch.run()
    events = store.events(orch.case_id)
    assert view.case.suppliers[LAKESHORE].status is S.NO_ANSWER
    assert view.case.suppliers[LINCOLN].reason == "out_of_stock"
    assert view.case.suppliers[HALSTED].status is S.GHOSTED
    assert view.case.suppliers[WINDY].status is S.DELIVERED
    assert calls(events, "pcp_followup") and calls(events, "pcp_correction")


def test_escalation_packet_is_actionable(tmp_path):
    view, _ = run(tmp_path, default_supplier="supplier_out_of_stock")
    (esc,) = view.escalations
    assert esc.decision_needed.endswith("?")
    assert len([t for t in esc.tried if "supplier_screen" in t]) == 12
    assert esc.snapshot["suppliers"][LAKESHORE] == "declined (out_of_stock)"
    assert view.timers == {}


@pytest.mark.parametrize("flags", [["--offline"], ["--offline", "--transcripts"]])
def test_run_demo_cli(flags):
    out = subprocess.run(
        [sys.executable, "run_demo.py", "--scenario", "messy_but_recovers", *flags],
        capture_output=True, text=True, cwd=config.ROOT,
    )  # fmt: skip
    assert out.returncode == 0, out.stderr
    assert "RESULT: CLOSED" in out.stdout and re.search(r"GHOSTED", out.stdout)


def test_calls_are_only_placed_on_business_days(tmp_path):
    _, events = run(tmp_path, pcp="pcp_messy", default_supplier="supplier_yes_then_ghost")
    for e in events:
        if e.type == "call_completed":
            assert is_business_day(e.at.date()) and 9 <= e.at.hour < 17
    assert all(e.at.date() != date(2026, 11, 26) for e in events if e.type == "call_completed")


def test_order_status_is_valid_when_closed(tmp_path):
    assert run(tmp_path)[0].case.order_status is OrderStatus.VALID


def test_signed_order_goes_to_each_committed_supplier(tmp_path):
    orch = build(
        Scenario(name="t", suppliers={LAKESHORE: "supplier_yes_then_ghost"}),
        OfflineLLM(), EventStore(tmp_path),
    )  # fmt: skip
    view = orch.run()
    sent = orch.handlers["schedule_delivery"].outbox.sent_orders
    document = view.orders_received[-1].document
    assert [fax for fax, _ in sent] == ["(312) 555-0150"] * 2
    for _, packet in sent:
        for needed in ("Eleanor Martinez, 2230 N Halsted St", "(312) 555-0123", document):
            assert needed in packet
    assert view.order_sent_to == [LAKESHORE, LINCOLN]


def test_first_pcp_call_does_not_ask_about_an_earlier_request(tmp_path):
    seen = {}

    class Spy(OfflineLLM):
        def complete(self, *, purpose, system, messages, role="agent"):
            seen.setdefault(purpose, system)
            return super().complete(purpose=purpose, system=system, messages=messages, role=role)

    run(tmp_path, llm=Spy(), pcp="pcp_lost_request")
    assert "earlier request" not in seen["agent:pcp_request"]
    assert "earlier request" in seen["agent:pcp_followup"]


def test_unconfirmed_address_is_not_used(tmp_path, monkeypatch):
    from dme.sim.counterparty import personas

    monkeypatch.setitem(personas()["patient"], "unsure", {"confirms": "unsure"})
    view, events = run(tmp_path, patient="unsure")
    assert view.case.patient.address is None
    assert view.escalations[0].reason == "patient_info_incomplete"
    assert not calls(events, "supplier_screen")
