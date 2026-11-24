"""Calls to the patient: intake, and delivery news for a patient who wants no texts."""

from __future__ import annotations

from datetime import datetime
from itertools import takewhile

from dme import config
from dme.clock import Task
from dme.events import PatientAddressSet
from dme.models import Supplier, SupplierStatus
from dme.rules.cost import cost_line
from dme.rules.geo import locate
from dme.rules.phone import usable_number
from dme.view import CaseView
from dme.workflows.base import Phone, Result


class PatientWorkflow:
    def __init__(self, phone: Phone):
        self.phone = phone

    def handle(self, task: Task, view: CaseView, now: datetime) -> Result:
        case = view.case
        call = self.phone.call(
            "patient_intake", "patient", view, now,
            goal="Get the patient's full home address, explain their share of the cost, and find "
            "out whether they want to go ahead.",
            facts=[
                f"Patient: {case.patient.name}",
                f"Their doctor, {case.pcp.name}, wants them to have: {case.equipment.line()}.",
                f"Cost explanation (say it in full): {cost_line(case)}",
            ],
            questions=[
                "What is the full home address for delivery, including ZIP code?",
                "Read the full address back: is it correct? (Wait for the answer.)",
                "What phone number should the delivery driver call?",
                "May we text updates about the wheelchair to that number?",
                "Is the cost explanation clear?",
                "Do they want to go ahead?",
            ],
        )  # fmt: skip
        out, result = call.outcome, Result(events=[call])
        if out.wants_to_proceed.no:
            result.escalate = ("patient_declined", out.summary)
        elif (
            (out.address.known and out.address_confirmed.yes)
            and (out.wants_to_proceed.yes and not out.understands_cost_share.no)
        ):  # fmt: skip
            address, phone = out.address.value, usable_number(out.phone_for_delivery)
            if locate(address.zip):
                result.events.append(
                    PatientAddressSet(
                        address=address, phone=phone, texts_ok=bool(phone) and out.texts_ok.yes
                    )
                )
            else:
                result.escalate = ("zip_unknown", address.one_line())
        elif (
            len(past := view.calls_to("patient", "patient_intake")) + 1
            < config.MAX_PATIENT_ATTEMPTS
        ):
            result.schedule.append((now + config.PATIENT_RETRY, task))
        else:
            reached = any(c.connected for c in [*past, call])
            result.escalate = (
                "patient_info_incomplete" if reached else "patient_unreachable",
                out.summary,
            )
        return result


class PatientUpdates:
    """Says where the delivery stands now, however many changes led here."""

    def __init__(self, phone: Phone, suppliers: list[Supplier]):
        self.phone, self.phones = phone, {s.supplier_id: s.phone for s in suppliers}

    def handle(self, task: Task, view: CaseView, now: datetime) -> Result:
        item, active = view.case.equipment.description.lower(), view.active_supplier()
        if active and active.status is SupplierStatus.COMMITTED:
            due, sid = active.committed_delivery_date, active.supplier_id
            if due < now.date():
                return Result()  # no longer news
            news = (
                f"{self.phone.names[sid]}, phone {self.phones[sid]}, is set to deliver their {item} "
                f"on {due:%A, %B %d, %Y}."
                + (" This replaces the earlier date." if active.reason == "rescheduled" else "")
            )
        else:
            news = (
                f"The delivery of their {item} fell through. Their doctor's order is still good, "
                "and we are finding another supplier."
            )
        past = view.calls_to("patient", "patient_update")
        call = self.phone.call(
            "patient_update", "patient", view, now,
            goal="Give the patient this delivery news. They asked us not to text.",
            facts=[news], questions=["Did they understand the news?"],
        )  # fmt: skip
        result = Result(events=[call])
        missed = sum(1 for _ in takewhile(lambda c: not c.verified_dob, [call, *reversed(past)]))
        if 0 < missed < config.MAX_PATIENT_ATTEMPTS:
            result.schedule.append((now + config.PATIENT_RETRY, task))
        return result
