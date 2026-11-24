"""Step 3: the state machine alone, with stub handlers."""

import pytest

from dme.clock import Task
from dme.data_loader import load_case
from dme.events import (
    CheckinPassed,
    OrderReceived,
    OrderSent,
    PatientAddressSet,
    StateChanged,
    SupplierUpdated,
)
from dme.llm.client import LLMError
from dme.models import Address, CaseState, SupplierStatus
from dme.orchestrator import Orchestrator
from dme.store import EventStore
from dme.workflows.base import Result
from tests.helpers import HALSTED, good_order, states

ADDR = Address(street="1 A St", city="Chicago", state="IL", zip="60614")


class Stub:
    def __init__(self, fn):
        self.fn = fn

    def handle(self, task, view, now):
        return self.fn(task, view, now)


class StubSourcer(Stub):
    def next_actions(self, view):
        if view.active_supplier() or view.open_tasks("supplier_screen"):
            return []
        return [Task(kind="supplier_screen", supplier_id=HALSTED)]

    def exhausted(self, view):
        return view.case.suppliers[HALSTED].status is SupplierStatus.DECLINED


def update(status, **kw):
    return Result(events=[SupplierUpdated(supplier_id=HALSTED, status=status, **kw)])


def patient(task, view, now):
    return Result(events=[PatientAddressSet(address=ADDR)])


def order(task, view, now):
    return Result(
        events=[OrderReceived(contact_no=1, document="d", order=good_order(), defects=[])]
    )


def schedule(task, view, now):
    r = update("committed", committed_delivery_date="2026-11-25")
    r.events.append(OrderSent(supplier_id=HALSTED, fax="f", packet="p"))
    r.schedule = [(now, Task(kind="checkin", supplier_id=HALSTED))]
    return r


def monitor(task, view, now):
    if task.kind == "checkin":
        return Result(
            events=[CheckinPassed(supplier_id=HALSTED)],
            schedule=[(now, Task(kind="confirm_delivery", supplier_id=HALSTED))],
        )
    return update("delivered")


def make(tmp_path, screen=lambda *a: update("qualified"), **over):
    fns = {
        "patient": patient,
        "order": order,
        "scheduler": schedule,
        "monitor": monitor,
        "updates": lambda *a: Result(),
    } | over
    store = EventStore(tmp_path)
    orch = Orchestrator(
        case=load_case(), store=store, sourcer=StubSourcer(screen),
        **{k: Stub(f) for k, f in fns.items()},
    )  # fmt: skip
    return orch, store


def test_happy_path_walks_every_state(tmp_path):
    orch, store = make(tmp_path)
    assert orch.run().state is CaseState.CLOSED
    assert states(store.events(orch.case_id)) == [
        "need_patient_info", "working", "ready_to_match", "delivery_scheduled",
        "awaiting_delivery_confirmation", "closed",
    ]  # fmt: skip


def test_no_qualified_supplier_never_reaches_ready_to_match(tmp_path):
    orch, store = make(tmp_path, screen=lambda *a: update("declined"))
    assert orch.run().state is CaseState.ESCALATED
    assert "ready_to_match" not in states(store.events(orch.case_id))


def test_qualified_supplier_without_valid_order_never_matches(tmp_path):
    orch, store = make(tmp_path, order=lambda *a: Result())
    assert orch.run().state is CaseState.WORKING
    assert "ready_to_match" not in states(store.events(orch.case_id))


def test_commitment_without_the_order_sent_does_not_schedule(tmp_path):
    commit = update("committed", committed_delivery_date="2026-11-25")
    orch, _ = make(tmp_path, scheduler=lambda *a: commit)
    assert orch.run().state is CaseState.READY_TO_MATCH


def test_handlers_cannot_change_state(tmp_path):
    cheat = Result(events=[StateChanged(old="need_patient_info", new="closed", reason="x")])
    orch, store = make(tmp_path, patient=lambda *a: cheat)
    with pytest.raises(RuntimeError, match="may not change"):
        orch.run()
    assert store.load(orch.case_id).state is CaseState.NEED_PATIENT_INFO


def test_transient_llm_failure_is_retried(tmp_path):
    attempts = []

    def flaky(*a):
        attempts.append(1)
        if len(attempts) < 3:
            raise LLMError("529")
        return patient(*a)

    orch, _ = make(tmp_path, patient=flaky)
    assert orch.run().state is CaseState.CLOSED and len(attempts) == 3


@pytest.mark.parametrize("fatal, tries", [(False, 3), (True, 1)])
def test_persistent_llm_failure_goes_to_a_human(tmp_path, fatal, tries):
    attempts = []

    def broken(*a):
        attempts.append(1)
        raise LLMError("down", fatal=fatal)

    orch, _ = make(tmp_path, patient=broken)
    view = orch.run()
    assert view.escalations[0].reason == "system_error" and len(attempts) == tries
    assert view.timers == {}


def test_crash_mid_step_leaves_log_resumable(tmp_path):
    def crash(*a):
        raise KeyboardInterrupt

    orch, store = make(tmp_path, scheduler=crash)
    with pytest.raises(KeyboardInterrupt):
        orch.run()
    assert store.load(orch.case_id).open_tasks("schedule_delivery")  # still owed
    again, _ = make(tmp_path)
    assert again.run().state is CaseState.CLOSED
