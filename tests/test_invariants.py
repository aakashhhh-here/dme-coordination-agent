"""Safety properties that must hold for ANY combination of people, plus crash recovery."""

import random

import pytest

from dme.app import build
from dme.clock import business_time
from dme.data_loader import load_suppliers
from dme.escalation import DECISIONS
from dme.llm.client import LLMError
from dme.models import CaseState, FactBase, OrderStatus, SupplierStatus
from dme.rules.order_check import check
from dme.sim.counterparty import Scenario, personas
from dme.sim.offline import OfflineLLM
from dme.store import EventStore
from dme.view import ACTIVE, CaseView

S = SupplierStatus


def random_scenario(seed):
    rng = random.Random(seed)
    p = personas()
    # Weight the patient and PCP towards cooperative, or most runs end before sourcing.
    return Scenario(
        name=f"r{seed}",
        patient=rng.choice([*p["patient"], *["patient_cooperative"] * 12]),
        pcp=rng.choice([*p["pcp"], *["pcp_cooperative", "pcp_messy"] * 4]),
        suppliers={s.supplier_id: rng.choice(list(p["supplier"])) for s in load_suppliers()},
    )


C = CaseState
ALLOWED = {  # the state diagram, written independently of the orchestrator
    C.NEW: {C.NEED_PATIENT_INFO},
    C.NEED_PATIENT_INFO: {C.WORKING},
    C.WORKING: {C.READY_TO_MATCH},
    C.READY_TO_MATCH: {C.DELIVERY_SCHEDULED, C.WORKING},
    C.DELIVERY_SCHEDULED: {C.AWAITING_DELIVERY_CONFIRMATION, C.WORKING, C.CLOSED},
    C.AWAITING_DELIVERY_CONFIRMATION: {C.CLOSED, C.WORKING},
}


class FlakyLLM(OfflineLLM):
    def __init__(self, seed):
        super().__init__()
        self.rng = random.Random(seed)

    def complete(self, **kw):
        if self.rng.random() < 0.03:
            raise LLMError("overloaded")
        return super().complete(**kw)


def check_invariants(events, view):
    replay = CaseView()
    for e in events:
        before_state = replay.state if replay.case else None
        before = {k: r.status for k, r in replay.case.suppliers.items()} if replay.case else {}
        assert replay.now is None or e.at >= replay.now, "time went backwards"
        replay.apply(e)
        case = replay.case
        active = [r for r in case.suppliers.values() if r.status in ACTIVE]
        assert len(active) <= 1, "two suppliers on the hook at once"
        if e.type == "state_changed":
            assert e.old == before_state and e.new in ALLOWED[e.old] | {C.ESCALATED}
            if e.new is CaseState.READY_TO_MATCH:
                assert active and case.order_status is OrderStatus.VALID
                assert check(case.written_order, case, e.at.date()) == []
            if e.new is CaseState.CLOSED:
                got = [r.supplier_id for r in case.suppliers.values() if r.status is S.DELIVERED]
                assert got and got[0] in replay.order_sent_to
        if e.type == "message_sent":
            assert case.patient.texts_ok
        if e.type == "call_completed":
            if e.party_id == "patient" and not e.verified_dob:
                facts = [f for f in vars(e.outcome).values() if isinstance(f, FactBase)]
                assert not any(f.known for f in facts), "believed an unverified voice"
            assert business_time(e.at) == e.at, "call outside business hours"
            if e.call_type == "supplier_schedule":
                assert case.order_status is OrderStatus.VALID
        if e.type == "supplier_updated" and e.status is S.COMMITTED:
            moved = e.reason == "rescheduled"
            assert before[e.supplier_id] is (S.COMMITTED if moved else S.QUALIFIED)
            assert e.committed_delivery_date > e.at.date()
            assert case.order_status is OrderStatus.VALID
    assert view.state in (CaseState.CLOSED, CaseState.ESCALATED), "case got stuck"
    assert view.timers == {}
    assert bool(view.escalations) == (view.state is CaseState.ESCALATED)
    assert all(e.reason in DECISIONS for e in view.escalations)
    if view.escalations and view.escalations[0].reason == "no_supplier":
        # Never give up on sourcing while someone in range is still untried.
        from dme.rules.ranking import rank_suppliers

        in_range = rank_suppliers(load_suppliers(), view.case.patient.address.zip)
        assert all(
            view.case.suppliers[s.supplier_id].status is not S.UNCONTACTED for s, _ in in_range
        )
        for s, _ in in_range:
            rec = view.case.suppliers[s.supplier_id]
            if rec.status is S.NO_ANSWER:
                assert len(view.calls_to(s.supplier_id, "supplier_screen")) == 2
    # Whoever we replaced after sending them the order was told to stop; nobody else was.
    withdrawn = [e.supplier_id for e in events if e.type == "order_withdrawn"]
    ghosted = [r.supplier_id for r in view.case.suppliers.values() if r.status is S.GHOSTED]
    assert sorted(withdrawn) == sorted(ghosted)
    limits = {}
    for c in view.calls:
        limits[c.party_id, c.call_type] = limits.get((c.party_id, c.call_type), 0) + 1
    assert all(n <= 12 for (p, _), n in limits.items() if p != "patient"), "pestering someone"
    assert replay == view


@pytest.mark.parametrize("flaky", [False, True])
def test_invariants_hold_for_random_worlds(tmp_path, flaky):
    ends = {}
    for seed in range(150):
        store = EventStore(tmp_path)
        llm = FlakyLLM(seed) if flaky else OfflineLLM()
        orch = build(random_scenario(seed), llm, store)
        view = orch.run()
        check_invariants(store.events(orch.case_id), store.load(orch.case_id))
        key = view.escalations[0].reason if view.escalations else "closed"
        ends[key] = ends.get(key, 0) + 1
    assert ends["closed"] > 20 and len(ends) > 4, f"sample too narrow: {ends}"


@pytest.mark.parametrize("seed", range(12))
def test_killing_the_process_after_any_step_changes_nothing(tmp_path, seed):
    """Restart from the log after every single step; end exactly where one run would."""
    sc = random_scenario(seed)
    straight = EventStore(tmp_path / "a")
    a = build(sc, OfflineLLM(), straight)
    a.run()
    chopped = EventStore(tmp_path / "b")
    while True:
        b = build(sc, OfflineLLM(), chopped)  # a "new process" each time
        if not b.view.timers:
            break
        b.clock._fire(b.step)
    assert chopped.load(b.case_id) == straight.load(a.case_id)
