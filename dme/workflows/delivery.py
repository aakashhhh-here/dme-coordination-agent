"""Schedule the delivery and make sure the chair actually arrives.

[Option B slot: a multi-checkpoint watchdog behind DeliveryMonitor]
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Protocol

from dme import config
from dme.clock import Task, add_business_days, is_business_day
from dme.events import CheckinPassed, Event, OrderSent, OrderWithdrawn, SupplierUpdated
from dme.models import DateFact, SupplierStatus
from dme.rules.phone import usable_number
from dme.view import CaseView
from dme.workflows.base import OrderOutbox, Phone, Result

S = SupplierStatus


def usable_date(fact: DateFact, now: datetime) -> date | None:
    """A date we will act on: clearly stated, in the future, not absurdly far, a business day."""
    if not fact.known or not is_business_day(fact.value):
        return None
    latest = now.date() + timedelta(days=config.MAX_DELIVERY_DAYS_OUT)
    return fact.value if now.date() < fact.value <= latest else None


def delivery_packet(view: CaseView, when: date) -> str:
    """Everything the supplier needs to deliver. Built here, never left to the call."""
    p, item = view.case.patient, view.case.equipment
    return (
        f"Deliver to: {p.name}, {p.address.one_line()}\n"
        f"Phone: {p.phone or f'ask care coordination, {config.OUR_FAX}'}\n"
        f"Delivery date: {when}\nItem: {item.line()}\n\n{view.orders_received[-1].document}"
    )


class DeliveryMonitor(Protocol):
    def watch(self, supplier_id: str, delivery: date, now: datetime) -> list[tuple[datetime, Task]]:
        """Checkpoints to schedule once a supplier commits to a date."""

    def handle(self, task: Task, view: CaseView, now: datetime) -> Result: ...


class Scheduler:
    """Offers the valid order to the qualified supplier and gets a delivery date,
    then sends them the order itself."""

    def __init__(self, phone: Phone, outbox: OrderOutbox, monitor: DeliveryMonitor):
        self.phone, self.outbox, self.monitor = phone, outbox, monitor

    def handle(self, task: Task, view: CaseView, now: datetime) -> Result:
        case, sid = view.case, task.supplier_id
        call = self.phone.call(
            "supplier_schedule", sid, view, now,
            goal="They said they can provide the item. We now have the signed order. Get a "
            "delivery date.",
            facts=[
                f"Patient: {case.patient.name}, {case.patient.address.one_line()}",
                f"Item: {case.equipment.line()}",
                f"Complete signed order from {case.pcp.name} in hand; we fax it with the delivery details after this call.",
                "Coverage: Original Medicare Part B",
            ],
            questions=["Can they schedule the delivery?", "On exactly which calendar date?", "Which fax number should the signed order and delivery details go to?"],
        )  # fmt: skip
        out, result = call.outcome, Result(events=[call])
        when, fax = usable_date(out.delivery_date, now), usable_number(out.order_fax)
        first_try = not view.calls_to(sid, "supplier_schedule")
        if out.can_schedule.yes and when and fax:
            update = dict(status=S.COMMITTED, committed_delivery_date=when)
            packet = delivery_packet(view, when)
            self.outbox.send_order(fax, packet)
            result.events.append(OrderSent(supplier_id=sid, fax=fax, packet=packet))
            result.schedule = self.monitor.watch(sid, when, now)
        elif out.can_schedule.no or not first_try:
            update = dict(status=S.DECLINED, reason="could_not_schedule")
        else:
            result.schedule = [(now + config.CALLBACK_DELAY, task)]
            return result
        result.events.append(SupplierUpdated(supplier_id=sid, **update))
        return result


class SimpleMonitor:
    """Check in with the supplier the day before, the patient the day after."""

    def __init__(self, phone: Phone, outbox: OrderOutbox):
        self.phone, self.outbox = phone, outbox

    def _replace(self, supplier_id: str, reason: str, view: CaseView) -> list[Event]:
        """Drop a supplier that has our order, and tell them not to deliver or bill."""
        sent = next(e for e in view.orders_sent if e.supplier_id == supplier_id)
        self.outbox.withdraw_order(sent.fax, sent.packet)
        return [
            SupplierUpdated(supplier_id=supplier_id, status=S.GHOSTED, reason=reason),
            OrderWithdrawn(supplier_id=supplier_id),
        ]

    def watch(self, supplier_id: str, delivery: date, now: datetime) -> list[tuple[datetime, Task]]:
        before = datetime.combine(add_business_days(delivery, -1), time(10))
        after = datetime.combine(add_business_days(delivery, 1), time(10))
        return [
            (max(before, now + timedelta(hours=1)), Task(kind="checkin", supplier_id=supplier_id)),
            (after, Task(kind="confirm_delivery", supplier_id=supplier_id)),
        ]

    def handle(self, task: Task, view: CaseView, now: datetime) -> Result:
        return (self._checkin if task.kind == "checkin" else self._confirm)(task, view, now)

    def _checkin(self, task: Task, view: CaseView, now: datetime) -> Result:
        case, sid = view.case, task.supplier_id
        call = self.phone.call(
            "supplier_checkin", sid, view, now,
            goal="Confirm the delivery they committed to is still on track.",
            facts=[
                f"Patient: {case.patient.name}",
                f"Item: {case.equipment.line()}",
                f"Committed delivery date: {case.suppliers[sid].committed_delivery_date:%A, %B %d, %Y}",
            ],
            questions=[
                "Is the delivery still on track for that date?",
                "If not: can they still deliver it, and on which new calendar date?",
            ],
        )  # fmt: skip
        result, on_track = Result(events=[call]), call.outcome.on_track_for_committed_date
        if on_track.yes:
            result.events.append(CheckinPassed(supplier_id=sid))
        elif (
            on_track.no and (new := usable_date(call.outcome.new_delivery_date, now))
            and not any(c.outcome.on_track_for_committed_date.no for c in view.calls_to(sid, "supplier_checkin"))
        ):  # fmt: skip
            # One slip is cheaper than starting over; a second one is not.
            result.cancel = ["confirm_delivery"]
            result.schedule = self.watch(sid, new, now)
            result.events.append(
                SupplierUpdated(
                    supplier_id=sid, status=S.COMMITTED, reason="rescheduled",
                    committed_delivery_date=new,
                )
            )  # fmt: skip
        elif not on_track.no and not view.calls_to(sid, "supplier_checkin"):
            result.schedule.append((now + config.CHECKIN_RETRY, task))  # silent or vague: once more
        else:
            reason = "withdrew_before_delivery" if on_track.no else "silent_before_delivery"
            result.events += self._replace(sid, reason, view)
        return result

    def _confirm(self, task: Task, view: CaseView, now: datetime) -> Result:
        case, sid = view.case, task.supplier_id
        due = case.suppliers[sid].committed_delivery_date
        call = self.phone.call(
            "patient_confirm", "patient", view, now,
            goal="Find out whether the right wheelchair arrived.",
            facts=[
                f"Patient: {case.patient.name}",
                f"{self.phone.names[sid]} was due to deliver a {case.equipment.description.lower()} on {due:%A, %B %d, %Y}.",
                "If it did not arrive, or is wrong or damaged: tell them we are following up on it.",
            ],
            questions=[
                "Did the wheelchair arrive?",
                "If it arrived: is it the right chair, undamaged? If not: did anything else arrive?",
            ],
        )  # fmt: skip
        result, received = Result(events=[call]), call.outcome.delivery_received
        wrong_item = call.outcome.wrong_or_damaged_item_arrived
        # Attempts for THIS delivery only; an earlier supplier's calls don't count.
        past = [c for c in view.calls_to("patient", "patient_confirm") if c.at.date() > due]
        if wrong_item.yes:
            # Not a no-show: a second supplier now would mean two chairs and two claims.
            result.escalate = ("wrong_item_delivered", call.outcome.summary)
        elif received.yes and wrong_item.no:
            result.events.append(SupplierUpdated(supplier_id=sid, status=S.DELIVERED))
        elif received.no and wrong_item.no:
            result.events += self._replace(sid, "not_delivered", view)
        elif len(past) + 1 < config.MAX_PATIENT_ATTEMPTS:
            result.schedule.append((now + config.PATIENT_RETRY, task))
        else:
            result.escalate = ("delivery_unconfirmed", call.outcome.summary)
        return result
