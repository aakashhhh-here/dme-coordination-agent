"""PCP order chase: request, follow up, read what arrives, send back what's wrong."""

from __future__ import annotations

from datetime import datetime, time

from dme import config
from dme.clock import Task, add_business_days
from dme.events import CallCompleted, OrderReceived
from dme.llm.client import LLM
from dme.llm.extractor import extract_order
from dme.models import OrderStatus
from dme.rules.order_check import check
from dme.view import CaseView
from dme.workflows.base import FaxInbox, Phone, Result


class OrderWorkflow:
    """Tasks: order_call (request or correction), order_followup, fax_check."""

    def __init__(self, phone: Phone, fax: FaxInbox, llm: LLM):
        self.phone, self.fax, self.llm = phone, fax, llm

    def handle(self, task: Task, view: CaseView, now: datetime) -> Result:
        status = view.case.order_status
        if status is OrderStatus.VALID:
            return Result()
        if task.kind == "fax_check":
            return self._fax_check(view, now)
        if task.kind == "order_call":
            kind = "pcp_correction" if status is OrderStatus.DEFECTIVE else "pcp_request"
            return self._call(kind, task, view, now)
        # Follow-ups are counted since the last document arrived.
        since = view.orders_received[-1].at if view.orders_received else datetime.min
        done = [c for c in view.pcp_contacts("pcp_followup") if c.at > since]
        if len(done) >= config.MAX_ORDER_FOLLOWUPS:
            return Result(escalate=("order_stalled", f"{len(done)} follow-ups, no usable order"))
        return self._call("pcp_followup", task, view, now)

    def _call(self, kind: str, task: Task, view: CaseView, now: datetime) -> Result:
        call = self._place_call(kind, view, now)
        result = Result(events=[call])
        if not call.connected:
            recent = view.calls_to("pcp")[1 - config.MAX_PCP_NO_ANSWERS :]
            missed = [c for c in [*recent, call] if not c.connected]
            if len(missed) >= config.MAX_PCP_NO_ANSWERS:
                result.escalate = ("order_stalled", "office is not answering")
            else:
                result.schedule.append((now + config.CALLBACK_DELAY, task))
        elif call.outcome.order_will_be_sent.no:
            result.escalate = ("order_refused", call.outcome.summary)
        else:
            # Even a vague "we'll see" gets a fax check and a follow-up date.
            due = add_business_days(now.date(), config.ORDER_FOLLOWUP_BUSINESS_DAYS)
            result.schedule = [
                (now + config.FAX_DELAY, Task(kind="fax_check")),
                (datetime.combine(due, time(config.OPEN_HOUR)), Task(kind="order_followup")),
            ]
        return result

    def _place_call(self, kind: str, view: CaseView, now: datetime) -> CallCompleted:
        case, sent = view.case, view.pcp_contacts()
        goal = {
            "pcp_request": "Ask the office to send a signed written order.",
            "pcp_followup": f"Follow up: we asked by phone on {sent[-1].at if sent else now:%B %d, %Y} and "
            "have no usable order. Have them confirm who has the request, and send the order.",
            "pcp_correction": "The order we received cannot be used. Ask for a corrected, newly "
            "signed order.",
        }[kind]
        return self.phone.call(
            kind, "pcp", view, now,
            goal=goal,
            facts=[
                f"Patient: {case.patient.name}",
                f"Item: {case.equipment.line()}",
                f"{case.pcp.name} saw the patient on {case.pcp_visit_date:%B %d, %Y} and gave a "
                "verbal order.",
                "The written order must show: patient name, order date, item description, billing "
                f"code {case.equipment.hcpcs}, quantity, {case.pcp.name}'s name and NPI, and a "
                "dated signature.",
                f"The order should be faxed to {config.OUR_FAX}.",
                *(f"Problem to fix on the order we received: {d.problem}" for d in view.defects),
            ],
            questions=(["Do they have our earlier request?"] if kind == "pcp_followup" else [])
            + ["Will they send the (corrected) order, and when?"]
            + ["Will it show everything required? (Say what is required, then wait.)"],
        )  # fmt: skip

    def _fax_check(self, view: CaseView, now: datetime) -> Result:
        contact = len(view.pcp_contacts())
        if view.orders_received and view.orders_received[-1].contact_no >= contact:
            return Result()  # already read this one
        document = self.fax.fax_for_contact(contact, view, now.date())
        if document is None:
            return Result()  # the follow-up timer covers it
        order = extract_order(self.llm, document)
        defects = check(order, view.case, now.date())
        received = OrderReceived(
            contact_no=contact, document=document, order=order, defects=defects
        )
        result = Result(events=[received], cancel=["order_followup"])
        if len(view.pcp_contacts("pcp_correction")) >= config.MAX_ORDER_CORRECTIONS and defects:
            result.escalate = ("order_defective", "; ".join(d.problem for d in defects))
        elif defects:
            result.schedule.append((now + config.CALL_GAP, Task(kind="order_call")))
        return result
