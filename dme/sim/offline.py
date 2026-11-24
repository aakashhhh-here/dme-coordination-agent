"""OfflineLLM: a rule-based stand-in so whole scenarios run free and deterministically.

It speaks the persona's facts as "key: value" sentences and reads them back with regexes.
It exercises the orchestrator, rules, grounding, timers and event log for real. It says
nothing about how well Claude converses or extracts; `pytest -m llm` covers that.
"""

from __future__ import annotations

import json
import re

from dme.events import LLMCalled
from dme.llm.call_agent import END
from dme.models import Address, AddressFact, Fact, WrittenOrder

_KEYS = {
    "taking_new_medicare_patients": "taking new Medicare patients",
    "has_k0001_in_stock": "K0001 in stock",
    "accepts_medicare_assignment": "accepts Medicare assignment",
    "serves_patient_area": "serves the patient ZIP",
    "earliest_delivery_days": "earliest delivery days",
    "can_schedule": "can schedule delivery",
    "delivery_date": "delivery date",
    "on_track_for_committed_date": "on track for committed date",
    "office_has_request": "has our earlier request",
    "order_will_be_sent": "will send the order",
    "is_patient": "is the patient in person",
    "date_of_birth": "date of birth",
    "texts_ok": "texts are fine",
    "new_delivery_date": "new delivery date",
    "address": "home address",
    "order_fax": "order fax number",
    "claim_billing_code": "billing code on the claim",
    "phone_for_delivery": "phone for the delivery driver",
    "address_confirmed": "address read back correctly",
    "wants_to_proceed": "wants to proceed",
    "understands_cost_share": "understands cost share",
    "understood": "understands the news",
    "wrong_or_damaged_item_arrived": "wrong or damaged item arrived",
    "delivery_received": "wheelchair was received",
}


def _facts(text: str) -> dict[str, str]:
    return json.loads(re.search(r"FACTS: (\{.*\})", text).group(1))


class OfflineLLM:
    def __init__(self):
        self.on_call = lambda e: None

    def complete(self, *, purpose, system, messages, role="agent") -> str:
        self.on_call(LLMCalled(model="offline", purpose=purpose))
        if purpose == "sim:counterparty":
            return "Hello. " + " ".join(f"{k}: {v}." for k, v in _facts(system).items())
        if purpose == "sim:fax":
            return "\n".join(f"{k}: {v}" for k, v in _facts(messages[0]["content"]).items())
        if purpose.startswith("message:"):
            return "Here is an update. " + " ".join(_facts(messages[0]["content"]).values())
        return f"This is an automated assistant. Thank you, goodbye. {END}"

    def parse(self, *, purpose, system, messages, schema):
        self.on_call(LLMCalled(model="offline", purpose=purpose))
        text = messages[0]["content"]
        return self._order(text) if schema is WrittenOrder else self._call(text, schema)

    def _call(self, text, schema):
        out = {}
        for name, field in schema.model_fields.items():
            m = re.search(re.escape(_KEYS.get(name, "\0")) + r": (.*?)\.(?=\s|$)", text)
            if not m or m.group(1) == "unsure":
                continue
            kind, raw = field.annotation, m.group(1)
            if raw.startswith("none"):
                continue
            value = raw  # pydantic parses ints and dates
            if kind is Fact:
                value = raw == "yes"
            elif kind is AddressFact:
                value = Address.parse(raw)
            out[name] = kind(value=value, confidence="high", evidence_quote=m.group(0))
        return schema(connected=True, summary="offline extraction", **out)

    def _order(self, text):
        f = dict(re.findall(r"^([\w ]+): (.*)$", text, re.M))
        f = {k: None if v == "(blank)" else v for k, v in f.items()}
        return WrittenOrder(
            patient_name=f["Patient name"],
            order_date=f["Order date"],
            item_description=f["Item"],
            hcpcs_code=f["HCPCS code"],
            quantity=f["Quantity"],
            prescriber_name=f["Prescriber"],
            prescriber_npi=f["NPI"],
            signature_present=bool(f["Signature"]),
            signature_date=f["Signature date"],
        )
