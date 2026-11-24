"""Everything that can happen to a case. Facts only, no behaviour."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from dme.clock import Task, Timer
from dme.models import Address, CallOutcome, Case, CaseState, SupplierStatus, Turn, WrittenOrder
from dme.rules.order_check import Defect


class _Event(BaseModel):
    at: datetime | None = None  # stamped by the orchestrator (simulated time)


class CaseOpened(_Event):
    type: Literal["case_opened"] = "case_opened"
    case: Case


class StateChanged(_Event):
    type: Literal["state_changed"] = "state_changed"
    old: CaseState
    new: CaseState
    reason: str


class CallCompleted(_Event):
    type: Literal["call_completed"] = "call_completed"
    call_type: str  # a key of models.CALL_SCHEMAS
    party_id: str  # "patient", "pcp" or a supplier_id
    transcript: list[Turn] = []
    outcome: CallOutcome
    verified_dob: date | None = None  # patient calls: who answered was verified

    @property
    def connected(self) -> bool:
        return self.outcome.connected


class PatientAddressSet(_Event):
    type: Literal["patient_address_set"] = "patient_address_set"
    address: Address
    phone: str | None = None
    texts_ok: bool = False


class SupplierUpdated(_Event):
    type: Literal["supplier_updated"] = "supplier_updated"
    supplier_id: str
    status: SupplierStatus
    reason: str | None = None
    committed_delivery_date: date | None = None


class OrderSent(_Event):
    """Delivery details and the signed order, which the supplier needs before billing."""

    type: Literal["order_sent"] = "order_sent"
    supplier_id: str
    fax: str
    packet: str


class OrderWithdrawn(_Event):
    """A replaced supplier was told not to deliver or bill."""

    type: Literal["order_withdrawn"] = "order_withdrawn"
    supplier_id: str


class CheckinPassed(_Event):
    type: Literal["checkin_passed"] = "checkin_passed"
    supplier_id: str


class OrderReceived(_Event):
    type: Literal["order_received"] = "order_received"
    contact_no: int  # which PCP contact produced this document
    document: str
    order: WrittenOrder
    defects: list[Defect]


class TimerScheduled(_Event):
    type: Literal["timer_scheduled"] = "timer_scheduled"
    timer: Timer


class TimerClosed(_Event):
    type: Literal["timer_closed"] = "timer_closed"
    timer_id: int  # fired or cancelled


class MessageSent(_Event):
    type: Literal["message_sent"] = "message_sent"
    milestone: str
    text: str
    facts: dict[str, str]
    source: Literal["llm", "template"]


class Escalated(_Event):
    type: Literal["escalated"] = "escalated"
    reason: str
    decision_needed: str
    detail: str = ""
    tried: list[str] = []
    snapshot: dict[str, Any] = {}


class LLMCalled(_Event):
    type: Literal["llm_called"] = "llm_called"
    model: str
    purpose: str
    input_tokens: int = 0
    output_tokens: int = 0


class TaskFailed(_Event):
    type: Literal["task_failed"] = "task_failed"
    task: Task
    error: str


Event = Annotated[
    CaseOpened
    | StateChanged
    | CallCompleted
    | PatientAddressSet
    | SupplierUpdated
    | CheckinPassed
    | OrderSent
    | OrderWithdrawn
    | OrderReceived
    | TimerScheduled
    | TimerClosed
    | MessageSent
    | Escalated
    | LLMCalled
    | TaskFailed,
    Field(discriminator="type"),
]
