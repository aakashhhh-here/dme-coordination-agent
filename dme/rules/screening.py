"""Turn a supplier's answers into a status. No LLM.

"Unknown" is never read as yes or no: it returns NO_ANSWER(unclear_answers), which
earns a callback like any other unfinished call.
"""

from dme import config
from dme.models import SupplierCallOutcome, SupplierStatus

# (field, decline reason if the answer is a clear "no")
CRITICAL = [
    ("taking_new_medicare_patients", "not_taking_new_medicare_patients"),
    ("has_k0001_in_stock", "out_of_stock"),
    ("accepts_medicare_assignment", "does_not_accept_assignment"),
    ("serves_patient_area", "out_of_service_area"),
]


def screen_supplier(outcome: SupplierCallOutcome) -> tuple[SupplierStatus, str | None]:
    if not outcome.connected:
        return SupplierStatus.NO_ANSWER, "no_answer"
    for field, reason in CRITICAL:
        if getattr(outcome, field).no:
            return SupplierStatus.DECLINED, reason
    days = outcome.earliest_delivery_days
    if days.known and days.value > config.MAX_DELIVERY_DAYS_OUT:
        return SupplierStatus.DECLINED, "delivery_too_far_out"
    if all(getattr(outcome, field).yes for field, _ in CRITICAL):
        return SupplierStatus.QUALIFIED, None
    return SupplierStatus.NO_ANSWER, "unclear_answers"
