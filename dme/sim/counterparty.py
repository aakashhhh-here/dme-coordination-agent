"""Simulated world: the people on the other end of the phone, and the fax machine.

Everything here is a mock. It reads the case view to know what has already happened to
it (has this supplier committed? how often were we called?), so a run can be resumed.
"""

from __future__ import annotations

import json
from datetime import date
from functools import cache
from pathlib import Path

import yaml
from pydantic import BaseModel

from dme import config
from dme.clock import add_business_days
from dme.llm.call_agent import HANG_UP, CallBrief
from dme.llm.client import LLM
from dme.models import Turn
from dme.view import CaseView

DEFAULT_ADDRESS = "2230 N Halsted St, Chicago, IL 60614"
DATE_OF_BIRTH = "1954-03-14"
PCP_NPI = "1234567893"  # the standard documentation example NPI


@cache
def personas() -> dict[str, dict[str, dict]]:
    return yaml.safe_load((Path(__file__).parent / "personas.yaml").read_text())


UNSIGNED = {"Signature": "(blank)", "Signature date": "(blank)"}
FAX_MISTAKES = {
    "wrong_code": {"HCPCS code": "K0003"},
    "missing_signature": UNSIGNED,
    "wrong_code_unsigned": {"HCPCS code": "K0003"} | UNSIGNED,
    "wrong_patient": {"Patient name": "Elena Martinez"},
}


class Scenario(BaseModel):
    name: str
    description: str = ""
    patient: str = "patient_cooperative"
    pcp: str = "pcp_cooperative"
    default_supplier: str = "supplier_happy"
    suppliers: dict[str, str] = {}
    expect_state: str = "closed"
    expect_reason: str | None = None

    @classmethod
    def load(cls, name: str) -> Scenario:
        path = config.SCENARIOS_DIR / f"{name}.yaml"
        return cls(name=name, **yaml.safe_load(path.read_text()))


def _pick(value, index: int):
    """A list means 'first time, second time, ...' with the last value repeating."""
    return value[min(index, len(value) - 1)] if isinstance(value, list) else value


class SimCounterparty:
    def __init__(
        self, llm: LLM, who: str, facts: dict[str, str], style: str, answers: bool, today: date
    ):
        self.llm, self.who, self.facts, self.style = llm, who, facts, style
        self._answers, self.today = answers, today

    def answers(self) -> bool:
        return self._answers

    def reply(self, turns: list[Turn]) -> str:
        system = f"""You are role-playing {self.who} on a phone call. {self.style}
Today's date is {self.today}.

THE TRUTH (reveal an item when asked about it; "unsure" means you don't know and say so):
FACTS: {json.dumps(self.facts)}

- For small matters the facts don't cover, give an ordinary cooperative answer. Don't raise doubts.
- Speak naturally, one to three short sentences per turn. No markdown, no stage directions.
- Stay consistent with the facts. Never contradict them or hint at things not asked.
- Say dates, ZIP codes, street, phone and fax numbers in digits.
- Make clear yes/no statements when the fact is yes or no.
- If the caller says goodbye, say goodbye and add {HANG_UP}."""
        messages = [{"role": "user", "content": "(The phone rings. You pick up.)"}]
        for t in turns:
            role = "assistant" if t.speaker == "counterparty" else "user"
            messages.append({"role": role, "content": t.text or "..."})
        return self.llm.complete(
            purpose="sim:counterparty", system=system, messages=messages, role="sim"
        )


class World:
    """Implements PhoneLine, FaxInbox and OrderOutbox."""

    def __init__(self, llm: LLM, scenario: Scenario):
        self.llm, self.scenario = llm, scenario
        self.withdrawn: list[tuple[str, str]] = []
        self.sent_orders: list[tuple[str, str]] = []

    def _supplier(self, supplier_id: str) -> dict:
        name = self.scenario.suppliers.get(supplier_id, self.scenario.default_supplier)
        return personas()["supplier"][name]

    def counterparty(self, brief: CallBrief, view: CaseView) -> SimCounterparty:
        case, kind = view.case, brief.call_type
        if brief.party_id == "patient":
            p = personas()["patient"][self.scenario.patient]
            who = p.get("who", f"{case.patient.name}, a 72-year-old patient")
        elif brief.party_id == "pcp":
            p = personas()["pcp"][self.scenario.pcp]
            who = f"the front desk at {case.pcp.practice}"
        else:
            p = self._supplier(brief.party_id)
            who = f"the person answering the phone at {brief.party_name}, a medical supplier"
        today = brief.now.date()
        answers = _pick(p.get("answers", True), len(view.calls_to(brief.party_id)))
        if kind == "supplier_checkin":
            answers = p.get("answers_after_commit", answers)
        facts = self._facts(brief, p, view, today)
        style = p.get("style", "You are polite and brief.")
        return SimCounterparty(self.llm, who, facts, style, answers, today)

    def _facts(self, brief: CallBrief, p: dict, view: CaseView, today: date) -> dict[str, str]:
        """What this person knows to be true, for this kind of call."""
        kind = brief.call_type
        who = {"is the patient in person": p.get("is_patient", "yes"),
               "date of birth": p.get("dob", DATE_OF_BIRTH)}  # fmt: skip
        if kind.startswith("patient") and who["is the patient in person"] == "no":
            return who
        if kind == "patient_intake":
            return who | {
                "home address": p.get("address", DEFAULT_ADDRESS),
                "address read back correctly": p.get("confirms", "yes"),
                "phone for the delivery driver": "(312) 555-0123",
                "texts are fine": p.get("texts_ok", "yes"),
                "wants to proceed": p.get("proceed", "yes"),
                "understands cost share": "yes",
            }
        if kind == "patient_update":
            return who | {"understands the news": "yes"}
        if kind == "patient_confirm":
            sid, street = view.active_supplier().supplier_id, view.case.patient.address.street
            # Nobody can deliver to an address they were never sent.
            told = any(e.supplier_id == sid and street in e.packet for e in view.orders_sent)
            arrived = told and self._supplier(sid).get("delivers", True)
            if arrived == "wrong_item":
                return who | {
                    "wheelchair was received": "no",
                    "wrong or damaged item arrived": "yes",
                    "what came instead": "a transport chair with small wheels, left by the supplier",
                }
            return who | {
                "wheelchair was received": "yes" if arrived else "no",
                "wrong or damaged item arrived": "no",
            }
        if kind == "supplier_screen":
            return {
                "taking new Medicare patients": p.get("new_medicare", "yes"),
                "K0001 in stock": p.get("in_stock", "yes"),
                "accepts Medicare assignment": p.get("assignment", "yes"),
                "serves the patient ZIP": p.get("serves_area", "yes"),
                "earliest delivery days": "2",
            }
        if kind == "supplier_schedule":
            can = p.get("can_schedule", "yes")
            dated = {
                "delivery date": add_business_days(today, 2).isoformat(),
                "order fax number": p.get("fax", "(312) 555-0150"),
                "billing code on the claim": p.get("bills", view.case.equipment.hcpcs),
            }
            return {"can schedule delivery": can} | (dated if can == "yes" else {})
        if kind == "supplier_checkin":
            on_track = _pick(p.get("on_track", "yes"), len(view.calls_to(brief.party_id, kind)))
            if on_track == "yes":
                return {"on track for committed date": "yes"}
            days = p.get("new_date_in")
            date_ = add_business_days(today, days).isoformat() if days else "none, can't deliver it"
            return {"on track for committed date": "no", "new delivery date": date_}
        facts = {"will send the order": p.get("will_send", "yes"), "when": "today"}
        if kind == "pcp_request":
            return facts  # first contact: there is no earlier request to have
        lost = p.get("lost_first") and kind == "pcp_followup"
        return {"has our earlier request": "no" if lost else "yes"} | facts

    def send_order(self, fax: str, packet: str) -> None:
        self.sent_orders.append((fax, packet))  # mocked: nothing leaves this process

    def withdraw_order(self, fax: str, packet: str) -> None:
        self.withdrawn.append((fax, packet))

    def fax_for_contact(self, contact_no: int, view: CaseView, today: date) -> str | None:
        """The document (if any) the office sends after our nth conversation with them."""
        variant = _pick(personas()["pcp"][self.scenario.pcp]["sends"], contact_no - 1)
        if variant == "none":
            return None
        case = view.case
        facts = {
            "Patient name": case.patient.name,
            "Order date": today.isoformat(),
            "Item": case.equipment.description,
            "HCPCS code": case.equipment.hcpcs,
            "Quantity": str(case.equipment.quantity),
            "Prescriber": f"{case.pcp.name}, MD",
            "NPI": PCP_NPI,
            "Signature": f"/s/ {case.pcp.name}, MD",
            "Signature date": today.isoformat(),
        } | FAX_MISTAKES.get(variant, {})
        prompt = f"Practice: {case.pcp.practice}\nFACTS: {json.dumps(facts)}"
        return self.llm.complete(
            purpose="sim:fax",
            role="sim",
            system="You produce the plain text of a one-page faxed doctor's order. Include every "
            "fact exactly as given and no other clinical or identifying facts. '(blank)' means "
            "the line is printed but left empty.",
            messages=[{"role": "user", "content": prompt}],
        )
