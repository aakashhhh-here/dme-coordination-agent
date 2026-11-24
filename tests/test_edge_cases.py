"""Guards found weak by mutation testing: each test here fails if its guard is removed."""

from datetime import date, timedelta

import pytest

from dme.app import build
from dme.clock import Task
from dme.data_loader import load_case, load_suppliers
from dme.events import CallCompleted, CaseOpened, SupplierUpdated
from dme.models import DateFact, DeliveryConfirmationOutcome, PatientCallOutcome, TextFact
from dme.rules.phone import usable_number
from dme.sim.counterparty import Scenario, World, personas
from dme.sim.offline import OfflineLLM
from dme.store import EventStore
from dme.view import CaseView
from dme.workflows.base import Phone
from dme.workflows.delivery import SimpleMonitor, usable_date
from tests.fake_llm import FakeLLM
from tests.helpers import LAKESHORE, T0, at, calls, run

DUE = date(2026, 11, 25)


@pytest.mark.parametrize(
    "day, ok",
    [
        (date(2026, 11, 24), True),
        (date(2026, 11, 23), False),  # today
        (date(2026, 11, 20), False),  # past
        (date(2026, 11, 26), False),  # Thanksgiving
        (date(2026, 11, 28), False),  # Saturday
        (date(2027, 2, 1), False),  # too far out
    ],
)
def test_only_plausible_delivery_dates_are_accepted(day, ok):
    fact = DateFact(value=day, confidence="high", evidence_quote="x")
    assert (usable_date(fact, T0) == day) is ok
    assert usable_date(fact.model_copy(update={"confidence": "low"}), T0) is None


class Line:
    """A phone line where everyone lets it ring."""

    def counterparty(self, brief, view):
        class Silent:
            def answers(self):
                return False

        return Silent()


def committed_view(*earlier: CallCompleted):
    view = CaseView()
    view.apply(CaseOpened(at=T0, case=load_case()))
    for call in earlier:
        view.apply(call)
    view.apply(
        SupplierUpdated(
            at=at(24), supplier_id=LAKESHORE, status="committed", committed_delivery_date=DUE
        )
    )
    return view


def monitor():
    return SimpleMonitor(
        Phone(FakeLLM({}), Line(), load_suppliers()), World(FakeLLM({}), Scenario(name="x"))
    )


def test_confirmation_attempts_for_an_earlier_supplier_do_not_count():
    old = [
        CallCompleted(
            at=at(20 + i), call_type="patient_confirm", party_id="patient", outcome=DeliveryConfirmationOutcome(connected=False),
        )
        for i in range(3)
    ]  # fmt: skip
    task = Task(kind="confirm_delivery", supplier_id=LAKESHORE)
    result = monitor().handle(task, committed_view(*old), at(27, 10))
    assert result.schedule and result.escalate is None


def test_unconfirmed_delivery_escalates_after_bounded_attempts():
    view, when = committed_view(), at(27, 10)
    task = Task(kind="confirm_delivery", supplier_id=LAKESHORE)
    for _ in range(2):
        result = monitor().handle(task, view, when)
        assert result.escalate is None
        for e in result.events:
            e.at = when
            view.apply(e)
        when += timedelta(days=1)
    assert monitor().handle(task, view, when).escalate[0] == "delivery_unconfirmed"


def test_stale_followup_is_cancelled_when_a_document_arrives(tmp_path, monkeypatch):
    monkeypatch.setitem(personas()["pcp"], "odd", {"sends": ["wrong_code", "none", "correct"]})
    view, events = run(tmp_path, pcp="odd")
    assert view.state == "closed" and len(calls(events, "pcp_followup")) == 1


def test_no_go_ahead_means_no_go(tmp_path, monkeypatch):
    """The patient gives an address but never says yes: no supplier may be involved.
    The doctor's paperwork is requested anyway; it commits nobody to anything."""
    monkeypatch.setitem(personas()["patient"], "undecided", {"proceed": "unsure"})
    view, events = run(tmp_path, patient="undecided")
    assert view.escalations[0].reason == "patient_info_incomplete"
    assert not calls(events, "supplier_screen") and view.orders_sent == []


def test_unknown_facts_are_never_known():
    out = PatientCallOutcome(connected=True)
    assert not out.wants_to_proceed.yes and not out.wants_to_proceed.no


def test_followup_budget_restarts_when_a_document_arrives(tmp_path, monkeypatch):
    sends = ["none", "none", "wrong_code", "none", "correct"]
    monkeypatch.setitem(personas()["pcp"], "slow", {"sends": sends})
    view, events = run(tmp_path, pcp="slow")
    assert view.state == "closed" and len(calls(events, "pcp_followup")) == 3


def test_clear_withdrawal_is_not_called_again(tmp_path):
    _, events = run(tmp_path, suppliers={LAKESHORE: "supplier_withdraws"})
    checkins = [c for c in calls(events, "supplier_checkin") if c.party_id == LAKESHORE]
    assert len(checkins) == 1


def test_date_without_a_clear_yes_is_not_a_commitment(tmp_path):
    class Hedging(OfflineLLM):
        def parse(self, *, schema, **kw):
            out = super().parse(schema=schema, **kw)
            if hasattr(out, "can_schedule"):
                out.can_schedule.confidence = "low"
            return out

    view, _ = run(tmp_path, llm=Hedging())
    assert all(r.committed_delivery_date is None for r in view.case.suppliers.values())


def test_nothing_happens_after_the_case_is_finished(tmp_path):
    for i, kw in enumerate([{}, {"default_supplier": "supplier_not_taking_new"}]):
        _, events = run(tmp_path, name=f"t{i}", **kw)
        end = next(
            e.at for e in events if e.type == "state_changed" and e.new in ("closed", "escalated")
        )
        assert events[-1].at == end


@pytest.mark.parametrize("supplier", ["supplier_happy", "supplier_confirms_then_no_show"])
def test_unclear_about_the_item_is_asked_again_not_assumed(tmp_path, supplier):
    class Unclear(OfflineLLM):
        def parse(self, *, schema, **kw):
            out = super().parse(schema=schema, **kw)
            if hasattr(out, "wrong_or_damaged_item_arrived"):
                out.wrong_or_damaged_item_arrived.confidence = "low"
            return out

    view, events = run(tmp_path, llm=Unclear(), suppliers={LAKESHORE: supplier})
    assert view.escalations[0].reason == "delivery_unconfirmed"
    assert len(calls(events, "patient_confirm")) == 3


def test_replaced_supplier_is_told_to_stop(tmp_path):
    suppliers = {LAKESHORE: "supplier_confirms_then_no_show"}
    orch = build(Scenario(name="t", suppliers=suppliers), OfflineLLM(), EventStore(tmp_path))
    orch.run()
    box = orch.handlers["checkin"].outbox
    assert box.withdrawn == box.sent_orders[:1]


def test_no_fax_number_means_no_commitment(tmp_path):
    view, _ = run(tmp_path, suppliers={LAKESHORE: "supplier_no_fax"})
    assert view.case.suppliers[LAKESHORE].reason == "could_not_schedule"
    assert LAKESHORE not in view.order_sent_to and view.state == "closed"


@pytest.mark.parametrize(
    "value, quote, ok",
    [
        ("(312) 555-0150", "It's 312-555-0150.", True),
        ("1-312-555-0150", "It's 1 312 555 0150.", True),
        ("(312) 555-0151", "It's 312-555-0150.", False),  # misheard
        ("555-0150", "It's 555-0150.", False),
        (None, "", False),
    ],
)
def test_usable_number(value, quote, ok):
    fact = TextFact(value=value, confidence="high", evidence_quote=quote)
    assert (usable_number(fact) is not None) is ok


def test_chair_cannot_arrive_if_the_address_was_not_sent(tmp_path, monkeypatch):
    monkeypatch.setattr("dme.workflows.delivery.delivery_packet", lambda view, when: "order only")
    assert run(tmp_path)[0].escalations[0].reason == "no_supplier"


def test_a_patient_without_texts_is_phoned_the_news_but_not_forever(tmp_path):
    view, events = run(
        tmp_path / "a", patient="patient_no_texts", suppliers={LAKESHORE: "supplier_withdraws"}
    )
    # Scheduled, fell through, scheduled again.
    told = calls(events, "patient_update")
    assert view.state == "closed" and len(told) == 3 and view.messages == []
    view, events = run(tmp_path / "b", patient="patient_no_texts_then_gone")
    due = view.active_supplier().committed_delivery_date
    missed = calls(events, "patient_update")
    assert 1 <= len(missed) <= 3 and all(c.at.date() <= due for c in missed)
