"""The state machine. step() is the only place case state changes. No LLM in here.

    NEW -> NEED_PATIENT_INFO -> WORKING (supplier sourcing) -> READY_TO_MATCH
        -> DELIVERY_SCHEDULED -> AWAITING_DELIVERY_CONFIRMATION -> CLOSED
    The order chase runs alongside from the start; matching waits for both.
    any state -> ESCALATED;  ghosting / refusal -> back to WORKING (valid order kept)

One step = one timer: run its handler, buffer the resulting events, move the state as far
as the guards allow, then write the whole batch. A crash before the write leaves the timer
open, so a resumed run simply does that step again.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from dme import config
from dme.clock import SimClock, Task, Timer, business_time
from dme.escalation import build_escalation
from dme.events import (
    CaseOpened,
    Event,
    LLMCalled,
    PatientAddressSet,
    StateChanged,
    TaskFailed,
    TimerClosed,
    TimerScheduled,
)
from dme.llm.client import LLMError
from dme.models import Case, CaseState, OrderStatus, SupplierStatus
from dme.store import EventStore
from dme.view import CaseView
from dme.workflows.base import Handler, Result
from dme.workflows.messages import CALLED_IF_NO_TEXTS, Messenger, milestone_of
from dme.workflows.sourcing import SupplierSourcer

C = CaseState
TERMINAL = {C.CLOSED, C.ESCALATED}


class Orchestrator:
    def __init__(
        self,
        case: Case,
        store: EventStore,
        sourcer: SupplierSourcer,
        patient: Handler,
        order: Handler,
        scheduler: Handler,
        monitor: Handler,
        updates: Handler,
        messenger: Messenger | None = None,
        on_event: Callable[[Event], None] | None = None,
    ):
        self.case_id, self.store = case.case_id, store
        self.sourcer, self.messenger = sourcer, messenger
        self.on_event = on_event or (lambda e: None)
        self.handlers: dict[str, Handler] = {
            "patient_intake": patient,
            "patient_update": updates,
            "supplier_screen": sourcer,
            "order_call": order,
            "order_followup": order,
            "fax_check": order,
            "schedule_delivery": scheduler,
            "checkin": monitor,
            "confirm_delivery": monitor,
        }
        self._buffer: list[Event] = []
        self.view = store.load(self.case_id)  # resume if a log exists
        self.clock = SimClock(self.view.now or config.SIM_START)
        for timer in self.view.timers.values():
            self.clock.schedule(timer)
        if self.view.case is None:
            self._emit(CaseOpened(case=case))
            self._schedule(self.clock.now, Task(kind="start"))
            self._flush()

    # --- public ----------------------------------------------------------
    def run(self) -> CaseView:
        self.clock.advance_until_idle(self.step)
        return self.view

    def step(self, timer: Timer) -> None:
        """Process one due timer."""
        self._emit(TimerClosed(timer_id=timer.timer_id))
        self._run_task(timer.task)
        self._advance()
        self._flush()

    def record_llm_call(self, e: LLMCalled) -> None:
        """Written straight away: usage must survive even if the step is abandoned."""
        e.at = self.clock.now
        self.store.append(self.case_id, e)
        self.on_event(e)

    # --- one task --------------------------------------------------------
    def _run_task(self, task: Task) -> None:
        if task.kind == "start" or self.view.state in TERMINAL:
            return
        now = self.clock.now
        try:
            result = self.handlers[task.kind].handle(task, self.view, now)
        except LLMError as e:
            self._emit(TaskFailed(task=task, error=str(e)))
            if e.fatal or task.tries + 1 >= config.MAX_TASK_RETRIES:
                self._escalate("system_error", str(e))
            else:
                retry = task.model_copy(update={"tries": task.tries + 1})
                self._schedule(now + config.TASK_RETRY_DELAY, retry)
            return
        self._apply(result)

    def _apply(self, result: Result) -> None:
        if any(isinstance(e, StateChanged) for e in result.events):
            raise RuntimeError("Workflows may not change case state")
        self._cancel_all(*result.cancel)
        for e in result.events:
            self._emit(e)
            news = e
            if isinstance(e, PatientAddressSet) and self.view.case.order_status is OrderStatus.VALID:
                news = self.view.orders_received[-1]  # came before we could tell them
            if self.messenger and (message := self.messenger.for_event(news, self.view.case)):
                self._emit(message)
            elif not self.view.case.patient.texts_ok and milestone_of(e) in CALLED_IF_NO_TEXTS:
                self._cancel_all("patient_update")  # the call gives the latest news
                self._schedule(self.clock.now + config.CALL_GAP, Task(kind="patient_update"))
        for at, task in result.schedule:
            # A rescheduled task starts with a fresh failure budget.
            self._schedule(at, task.model_copy(update={"tries": 0}))
        if result.escalate:
            self._escalate(*result.escalate)

    # --- state transitions -------------------------------------------------
    def _advance(self) -> None:
        """Follow guards until nothing more can move, then keep sourcing going."""
        while move := self._next_transition():
            self._transition(*move)
        if self.view.state is C.WORKING:
            if self.sourcer.exhausted(self.view):
                self._escalate("no_supplier")
                return
            for task in self.sourcer.next_actions(self.view):
                self._schedule(self.clock.now + config.CALL_GAP, task)

    def _next_transition(self) -> tuple[CaseState, str] | None:
        v = self.view
        active = v.active_supplier()
        committed = active is not None and active.status is SupplierStatus.COMMITTED
        match v.state:
            case C.NEW:
                return C.NEED_PATIENT_INFO, "case opened; intake has no address"
            case C.NEED_PATIENT_INFO if v.case.patient.address:
                return C.WORKING, "address on file, patient wants to proceed"
            case C.WORKING if active and v.case.order_status is OrderStatus.VALID:
                return C.READY_TO_MATCH, "qualified supplier and valid order"
            case C.READY_TO_MATCH if committed and active.supplier_id in v.order_sent_to:
                return C.DELIVERY_SCHEDULED, f"delivery {active.committed_delivery_date}"
            case C.READY_TO_MATCH if active is None:
                return C.WORKING, "supplier fell through before scheduling"
            case C.DELIVERY_SCHEDULED | C.AWAITING_DELIVERY_CONFIRMATION if active is None:
                if any(r.status is SupplierStatus.DELIVERED for r in v.case.suppliers.values()):
                    return C.CLOSED, "patient confirmed delivery"
                return C.WORKING, "supplier ghosted; order still valid"
            case C.DELIVERY_SCHEDULED if v.checkin_passed:
                return C.AWAITING_DELIVERY_CONFIRMATION, "supplier confirmed on track"
        return None

    def _transition(self, new: CaseState, reason: str) -> None:
        self._emit(StateChanged(old=self.view.state, new=new, reason=reason))
        now = self.clock.now
        if new is C.NEED_PATIENT_INFO:
            self._schedule(now, Task(kind="patient_intake"))
            # The order needs nothing from the patient, so it doesn't wait for them.
            self._schedule(now + config.CALL_GAP, Task(kind="order_call"))
        elif new is C.WORKING:
            self._cancel_all("checkin", "confirm_delivery", "schedule_delivery")
        elif new is C.READY_TO_MATCH:
            self._cancel_all("supplier_screen")
            active = self.view.active_supplier()
            self._schedule(
                now + config.CALL_GAP,
                Task(kind="schedule_delivery", supplier_id=active.supplier_id),
            )
        elif new in TERMINAL:
            self._cancel_all(*self.handlers)

    def _escalate(self, reason: str, detail: str = "") -> None:
        self._emit(build_escalation(self.view, reason, detail))
        self._transition(C.ESCALATED, reason)

    # --- plumbing ----------------------------------------------------------
    def _schedule(self, at: datetime, task: Task) -> None:
        # Every task is a call or a document check, so all of them keep business hours.
        timer = Timer(timer_id=self.view.next_timer_id, at=business_time(at), task=task)
        self._emit(TimerScheduled(timer=timer))
        self.clock.schedule(timer)

    def _cancel(self, timer_id: int) -> None:
        self._emit(TimerClosed(timer_id=timer_id))
        self.clock.cancel(timer_id)

    def _cancel_all(self, *kinds: str) -> None:
        for t in self.view.open_tasks(*kinds):
            self._cancel(t.timer_id)

    def _emit(self, e: Event) -> None:
        e.at = self.clock.now
        self.view.apply(e)
        self._buffer.append(e)

    def _flush(self) -> None:
        batch, self._buffer = self._buffer, []
        self.store.append(self.case_id, *batch)
        for e in batch:
            self.on_event(e)
