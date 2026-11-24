"""What every workflow shares. A handler reads the view and returns a Result; only the
orchestrator writes the log, touches the clock, or changes case state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol

from dme.clock import Task
from dme.events import CallCompleted, Event
from dme.llm.call_agent import CallBrief, Counterparty, run_call
from dme.llm.client import LLM
from dme.llm.extractor import extract_call
from dme.models import CALL_SCHEMAS, IdentityOutcome, Supplier, Turn
from dme.rules.identity import verified_dob
from dme.view import CaseView


@dataclass
class Result:
    events: list[Event] = field(default_factory=list)
    schedule: list[tuple[datetime, Task]] = field(default_factory=list)
    cancel: list[str] = field(default_factory=list)  # task kinds whose open timers are dropped
    escalate: tuple[str, str] | None = None  # (reason, detail)


class Handler(Protocol):
    def handle(self, task: Task, view: CaseView, now: datetime) -> Result: ...


class PhoneLine(Protocol):
    """Reaches people. Today the simulator; later a Twilio/Vapi-shaped adapter."""

    def counterparty(self, brief: CallBrief, view: CaseView) -> Counterparty: ...


class FaxInbox(Protocol):
    def fax_for_contact(self, contact_no: int, view: CaseView, today: date) -> str | None: ...


class OrderOutbox(Protocol):
    def send_order(self, fax: str, packet: str) -> None: ...

    def withdraw_order(self, fax: str, packet: str) -> None: ...


class Phone:
    """Places a call and returns the typed record of it."""

    def __init__(self, llm: LLM, line: PhoneLine, suppliers: list[Supplier]):
        self.llm, self.line = llm, line
        self.names = {s.supplier_id: s.name for s in suppliers}

    def call(
        self, call_type: str, party_id: str, view: CaseView, now: datetime,
        goal: str, facts: list[str], questions: list[str],
    ) -> CallCompleted:  # fmt: skip
        case = view.case
        names = {"patient": case.patient.name, "pcp": f"{case.pcp.name}'s office"} | self.names
        brief = CallBrief(
            call_type=call_type, party_id=party_id, party_name=names[party_id],
            goal=goal, facts=facts, questions=questions, now=now,
        )  # fmt: skip
        if party_id == "patient":
            brief.facts = [*facts, f"You have verified that you are speaking with {case.patient.name}."
                           " That is settled: say no more about it."]  # fmt: skip
        dob: date | None = None

        def verified(turns: list[Turn]) -> bool:
            nonlocal dob
            if party_id == "patient":
                said = extract_call(self.llm, turns, IdentityOutcome, now)
                dob = verified_dob(said, case.patient, now.date())
            return party_id != "patient" or dob is not None

        turns = run_call(self.llm, brief, self.line.counterparty(brief, view), verified=verified)
        schema = CALL_SCHEMAS[call_type]
        if turns and party_id == "patient" and dob is None:
            # Nothing a stranger says about the case counts.
            outcome = schema(connected=True, summary="Could not verify who answered; told nothing.")
        else:
            outcome = extract_call(self.llm, turns, schema, now)
        return CallCompleted(
            call_type=call_type, party_id=party_id, transcript=turns, outcome=outcome,
            verified_dob=dob,
        )  # fmt: skip
