"""Current state of a case, rebuilt by replaying its events."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from dme.clock import Timer
from dme.events import (
    CallCompleted,
    CaseOpened,
    CheckinPassed,
    Escalated,
    Event,
    MessageSent,
    OrderReceived,
    OrderSent,
    PatientAddressSet,
    StateChanged,
    SupplierUpdated,
    TimerClosed,
    TimerScheduled,
)
from dme.models import Case, CaseState, OrderStatus, SupplierRecord, SupplierStatus
from dme.rules.order_check import Defect

ACTIVE = {SupplierStatus.QUALIFIED, SupplierStatus.COMMITTED}


class CaseView(BaseModel):
    """Everything the rules and workflows may look at. Built only by apply()."""

    case: Case | None = None
    now: datetime | None = None
    timers: dict[int, Timer] = {}
    next_timer_id: int = 1
    calls: list[CallCompleted] = []
    defects: list[Defect] = []
    orders_received: list[OrderReceived] = []
    checkin_passed: bool = False
    orders_sent: list[OrderSent] = []
    messages: list[MessageSent] = []
    escalations: list[Escalated] = []

    # --- queries ---------------------------------------------------------
    @property
    def state(self) -> CaseState:
        return self.case.state

    def calls_to(self, party_id: str, *call_types: str) -> list[CallCompleted]:
        """Calls placed to someone; every type unless some are named."""
        calls = [c for c in self.calls if c.party_id == party_id]
        return [c for c in calls if not call_types or c.call_type in call_types]

    def pcp_contacts(self, *call_types: str) -> list[CallCompleted]:
        return [c for c in self.calls_to("pcp", *call_types) if c.connected]

    @property
    def order_sent_to(self) -> list[str]:
        return [e.supplier_id for e in self.orders_sent]

    def open_tasks(self, *kinds: str) -> list[Timer]:
        return [t for t in self.timers.values() if t.task.kind in kinds]

    def active_supplier(self) -> SupplierRecord | None:
        """The one supplier we are currently counting on, if any."""
        return next((r for r in self.case.suppliers.values() if r.status in ACTIVE), None)

    # --- reducer ---------------------------------------------------------
    def apply(self, e: Event) -> None:
        if e.at is not None:
            self.now = e.at
        match e:
            case CaseOpened():
                self.case = e.case.model_copy(deep=True)
            case StateChanged():
                self.case.state = e.new
            case CallCompleted():
                self.calls.append(e)
                self.case.patient.date_of_birth = e.verified_dob or self.case.patient.date_of_birth
                first = self.case.order_status is OrderStatus.VERBAL_ONLY
                if first and e.party_id == "pcp" and e.connected:
                    self.case.order_status = OrderStatus.REQUESTED
            case PatientAddressSet():
                self.case.patient.address = e.address
                self.case.patient.phone = e.phone or self.case.patient.phone
                self.case.patient.texts_ok = e.texts_ok
            case SupplierUpdated():
                rec = self.case.suppliers[e.supplier_id]
                rec.status, rec.reason = e.status, e.reason
                rec.committed_delivery_date = (
                    e.committed_delivery_date or rec.committed_delivery_date
                )
                self.checkin_passed = False
            case CheckinPassed():
                self.checkin_passed = True
            case OrderSent():
                self.orders_sent.append(e)
            case OrderReceived():
                self.orders_received.append(e)
                self.case.written_order, self.defects = e.order, e.defects
                self.case.order_status = OrderStatus.DEFECTIVE if e.defects else OrderStatus.VALID
            case TimerScheduled():
                self.timers[e.timer.timer_id] = e.timer
                self.next_timer_id = max(self.next_timer_id, e.timer.timer_id + 1)
            case TimerClosed():
                self.timers.pop(e.timer_id, None)
            case MessageSent():
                self.messages.append(e)
            case Escalated():
                self.escalations.append(e)
