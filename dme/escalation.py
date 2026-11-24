"""Human queue. Every escalation carries the one decision a person has to make."""

from __future__ import annotations

from dme.events import Escalated
from dme.view import CaseView

DECISIONS = {
    "patient_unreachable": "Find another way to reach the patient (caregiver, new number, letter), or close the case?",
    "patient_info_incomplete": "Can you get the patient's home address and go-ahead directly?",
    "patient_declined": "Should this case be closed at the patient's request?",
    "zip_unknown": "Is this address correct, and which suppliers serve it?",
    "no_supplier": "Widen the search radius, accept a non-assignment supplier, or wait for stock?",
    "order_stalled": "Who can get the written order from Dr. Chen's office by another route?",
    "order_refused": "The office won't send the order as things stand. How should we proceed with the PCP?",
    "order_defective": "Can you resolve the remaining order defects with Dr. Chen directly?",
    "delivery_unconfirmed": "Did the wheelchair arrive? We could not confirm it with the patient.",
    "wrong_item_delivered": "The supplier delivered a wrong or damaged item. Should they exchange it, or take it back so we can use another supplier?",
    "system_error": "The automation could not continue. Can you take the next step by hand or fix the fault?",
}


def build_escalation(view: CaseView, reason: str, detail: str = "") -> Escalated:
    case = view.case
    tried = []
    for c in view.calls:
        name = c.party_id
        result = c.outcome.summary if c.connected else "no answer"
        tried.append(f"{c.at:%a %b %d %H:%M} {c.call_type} {name}: {result}")
    return Escalated(
        reason=reason,
        decision_needed=DECISIONS[reason],
        detail=detail,
        tried=tried,
        snapshot={
            "state": case.state.value,
            "patient": case.patient.name,
            "address": case.patient.address.one_line() if case.patient.address else None,
            "order_status": case.order_status.value,
            "order_defects": [d.problem for d in view.defects],
            "suppliers": {
                r.supplier_id: f"{r.status.value}" + (f" ({r.reason})" if r.reason else "")
                for r in case.suppliers.values()
            },
        },
    )
