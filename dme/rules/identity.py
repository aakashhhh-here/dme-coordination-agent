"""Are we talking to the patient? No LLM."""

from datetime import date

from dme.models import IdentityOutcome, Patient


def verified_dob(out: IdentityOutcome, patient: Patient, today: date) -> date | None:
    """The date of birth, if the patient says who they are and it matches our records.

    Intake has only an age, so the first call checks that; later calls need the same date.
    """
    if not (out.is_patient.yes and out.date_of_birth.known):
        return None
    dob = out.date_of_birth.value
    if patient.date_of_birth:
        return dob if dob == patient.date_of_birth else None
    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    return dob if age == patient.age else None
