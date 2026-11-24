"""Supplier sourcing.  A parallel sourcer could replace SimpleSourcer behind SupplierSourcer."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from dme import config
from dme.clock import Task
from dme.events import SupplierUpdated
from dme.models import CallOutcome, Supplier, SupplierStatus
from dme.rules.ranking import rank_suppliers
from dme.rules.screening import screen_supplier
from dme.view import CaseView
from dme.workflows.base import Phone, Result

S = SupplierStatus


class SupplierSourcer(Protocol):
    def next_actions(self, view: CaseView) -> list[Task]: ...

    def exhausted(self, view: CaseView) -> bool: ...

    def on_outcome(
        self, supplier_id: str, outcome: CallOutcome, view: CaseView, now: datetime
    ) -> Result: ...

    def handle(self, task: Task, view: CaseView, now: datetime) -> Result: ...


class SimpleSourcer:
    """One call at a time in distance order; stop at the first QUALIFIED.

    While a callback is pending we move on to the next supplier.
    """

    def __init__(self, phone: Phone, suppliers: list[Supplier]):
        self.phone, self.suppliers = phone, suppliers

    def _next_candidate(self, view: CaseView) -> str | None:
        """Nearest supplier not yet called; failing that, one still owed a callback (it is
        dropped when someone else qualifies, and matters again if that one ghosts)."""
        waiting = {t.task.supplier_id for t in view.open_tasks("supplier_screen")}
        ranked = [
            s.supplier_id for s, _ in rank_suppliers(self.suppliers, view.case.patient.address.zip)
        ]
        free = [view.case.suppliers[i] for i in ranked if i not in waiting]
        fresh = [r for r in free if r.status is S.UNCONTACTED]
        owed = [
            r for r in free if r.status is S.NO_ANSWER and self._attempts(r.supplier_id, view) < 2
        ]
        return next((r.supplier_id for r in fresh + owed), None)

    @staticmethod
    def _attempts(supplier_id: str, view: CaseView) -> int:
        return len(view.calls_to(supplier_id, "supplier_screen"))

    def next_actions(self, view: CaseView) -> list[Task]:
        calling = any(not t.task.callback for t in view.open_tasks("supplier_screen"))
        if calling or view.active_supplier():
            return []
        supplier_id = self._next_candidate(view)
        return [Task(kind="supplier_screen", supplier_id=supplier_id)] if supplier_id else []

    def exhausted(self, view: CaseView) -> bool:
        busy = view.active_supplier() or view.open_tasks("supplier_screen")
        return not busy and self._next_candidate(view) is None

    def handle(self, task: Task, view: CaseView, now: datetime) -> Result:
        if view.active_supplier():
            return Result()  # a callback that came due after someone else said yes
        item, zip_code = view.case.equipment, view.case.patient.address.zip
        call = self.phone.call(
            "supplier_screen", task.supplier_id, view, now,
            goal="Find out whether this supplier can provide the item to our patient. "
            "One clear no settles it: thank them and end the call.",
            # Minimum necessary: no name or street address until they have qualified.
            facts=[
                f"Item: {item.line()}",
                "Coverage: Original Medicare Part B",
                f"Patient ZIP code: {zip_code}",
                "The written order from the doctor is being obtained separately.",
            ],
            questions=[
                "Are you taking new Medicare patients?",
                f"Do you have {item.hcpcs} in stock now?",
                "Do you accept Medicare assignment for it?",
                f"Do you deliver to ZIP {zip_code}?",
                "How many days until the earliest delivery? (A number is enough; no calendar date.)",
            ],
        )  # fmt: skip
        result = self.on_outcome(task.supplier_id, call.outcome, view, now)
        result.events.insert(0, call)
        return result

    def on_outcome(
        self, supplier_id: str, outcome: CallOutcome, view: CaseView, now: datetime
    ) -> Result:
        status, reason = screen_supplier(outcome)
        result = Result([SupplierUpdated(supplier_id=supplier_id, status=status, reason=reason)])
        if status is S.NO_ANSWER and self._attempts(supplier_id, view) == 0:
            task = Task(kind="supplier_screen", supplier_id=supplier_id, callback=True)
            result.schedule.append((now + config.CALLBACK_DELAY, task))
        return result
