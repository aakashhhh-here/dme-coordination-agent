"""The cost explanation. Computed here, never by the LLM."""

from dme.models import Case

MEDICARE_SHARE = 80


def cost_line(case: Case) -> str:
    """Computed here and never by the LLM.

    Source: https://www.medicare.gov/coverage/wheelchairs-scooters (20% of the
    Medicare-approved amount after the Part B deductible, if the supplier accepts assignment).
    """
    patient_share = 100 - MEDICARE_SHARE
    line = (
        f"Medicare Part B pays about {MEDICARE_SHARE}% of the Medicare-approved amount and your "
        f"share is about {patient_share}%, after your yearly Part B deductible."
    )
    if case.patient.insurance.supplemental is None:
        line += (
            f" You have no Medigap plan on file, so that {patient_share}% would be yours to pay."
        )
    return line
