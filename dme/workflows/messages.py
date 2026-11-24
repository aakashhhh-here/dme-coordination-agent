"""Milestone messages to the patient. The LLM words them; code checks them and adds the cost."""

from __future__ import annotations

import json
import re

from dme.events import Event, MessageSent, OrderReceived, SupplierUpdated
from dme.llm.client import LLM, LLMError
from dme.models import Case, Supplier, SupplierStatus
from dme.rules.cost import cost_line

S = SupplierStatus

TEMPLATES = {
    "order_confirmed": "Good news: {doctor} has sent a complete written order for your {item}.",
    "supplier_found": "We found a supplier for your {item}: {supplier}. Their phone number is {supplier_phone}.",
    "delivery_scheduled": "{supplier} is set to deliver your {item} on {delivery_date}. Their phone number is {supplier_phone}.",
    "delivery_moved": "{supplier} moved the delivery of your {item} to {delivery_date}. Their phone number is {supplier_phone}.",
    "delivery_problem": "{supplier} did not come through with your {item}. Your order is still good and we are finding another supplier now.",
    "delivered": "We have confirmed that your {item} was delivered by {supplier}.",
}

_MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"
_DAYS = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
_CLAIMS = re.compile(
    rf"\d+(?:[.,]\d+)*|\bon (?:its|the) way\b|\bshipped\b|\b(?:{_MONTHS}|{_DAYS})\b|\b(?:today|tonight|tomorrow|yesterday|next week)\b"
    r"|[$%]|\b(?:dollars?|percent|free|no cost|co-?pays?|deductible|medicare|medigap)\b"
    r"|\b(?:call|contact|reach|reply|questions?|promise|guarantee)\b",
    re.I,
)


def unsupported_claims(draft: str, facts: dict[str, str]) -> list[str]:
    """Dates, numbers, money words and invitations in the draft that no fact contains."""
    allowed = {m.lower() for v in facts.values() for m in _CLAIMS.findall(v)}
    return [m for m in _CLAIMS.findall(draft) if m.lower() not in allowed]


_BY_STATUS = {
    S.QUALIFIED: "supplier_found",
    S.COMMITTED: "delivery_scheduled",
    S.GHOSTED: "delivery_problem",
    S.DELIVERED: "delivered",
}


def milestone_of(e: Event) -> str | None:
    if isinstance(e, OrderReceived):
        return None if e.defects else "order_confirmed"
    if isinstance(e, SupplierUpdated):
        if e.status is S.DECLINED and e.reason in ("could_not_schedule", "wrong_billing_code"):
            return "delivery_problem"  # the patient was already told about this supplier
        return "delivery_moved" if e.reason == "rescheduled" else _BY_STATUS.get(e.status)
    return None


# A patient who wants no texts is phoned with these instead.
CALLED_IF_NO_TEXTS = {"delivery_scheduled", "delivery_moved", "delivery_problem"}


class Messenger:
    def __init__(self, llm: LLM, suppliers: list[Supplier]):
        self.llm = llm
        self.suppliers = {s.supplier_id: s for s in suppliers}

    def for_event(self, e: Event, case: Case) -> MessageSent | None:
        """The message this event calls for, if any."""
        milestone = milestone_of(e)
        if milestone is None or not case.patient.texts_ok:
            return None
        facts = {"item": case.equipment.description.lower(), "doctor": case.pcp.name}
        if isinstance(e, SupplierUpdated):
            supplier = self.suppliers[e.supplier_id]
            facts |= {"supplier": supplier.name, "supplier_phone": supplier.phone}
            if e.status is S.COMMITTED:
                facts["delivery_date"] = (
                    f"{(d := e.committed_delivery_date):%A, %B} {d.day}, {d.year}"
                )
        return self.send(milestone, facts, case)

    def send(self, milestone: str, facts: dict[str, str], case: Case) -> MessageSent:
        template = TEMPLATES[milestone].format_map(facts)
        text, source = template, "template"
        try:
            draft = self.llm.complete(
                purpose=f"message:{milestone}",
                system="You write a short, warm, plain-language text message to an older patient about "
                "their medical equipment. Two or three sentences. Use only the facts given. Do not "
                "mention cost, Medicare, dates, times or numbers that are not in the facts. Do not "
                "invite a reply, suggest calling anyone, or promise anything. No greeting line, no sign-off, no markdown.",
                messages=[{"role": "user", "content": self._prompt(milestone, template, facts)}],
            )
            # A draft that adds facts, or drops the delivery date, is thrown away.
            date_ok = facts.get("delivery_date", "") in draft
            if date_ok and not unsupported_claims(draft, facts):
                text, source = draft, "llm"
        except LLMError:
            pass  # the template is always good enough
        if milestone == "delivery_scheduled":  # when it matters; not repeated in every text
            text = f"{text} {cost_line(case)}"
        return MessageSent(milestone=milestone, text=text, facts=facts, source=source)

    @staticmethod
    def _prompt(milestone: str, template: str, facts: dict[str, str]) -> str:
        return f"MILESTONE: {milestone}\nMEANING: {template}\nFACTS: {json.dumps(facts)}"
