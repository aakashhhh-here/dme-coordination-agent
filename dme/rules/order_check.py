"""Is the written order good enough to act on? Pure function, no LLM.

Sources (checked Nov 2026):
- 42 CFR 410.38(d)(1)(i): a Standard Written Order needs beneficiary name or MBI, a general
  description of the item, quantity if applicable, order date, treating practitioner name OR
  NPI, and treating practitioner signature.
  https://www.law.cornell.edu/cfr/text/42/410.38
- LCD L33788 (Manual Wheelchair Bases): the order must reach the supplier before the claim is
  submitted, or the claim is denied.
  https://www.cms.gov/medicare-coverage-database/view/lcd.aspx?LCDId=33788

Some checks are deliberately STRICTER than Medicare and are tagged basis="policy":
- prescriber name AND NPI (Medicare accepts either; the supplier's claim needs the NPI)
- HCPCS code on the order (a description alone is legal; the code is what catches the
  K0001/K0003 mix-up before it becomes a denied claim)
- a signature date, so a stale signature is visible

This checks the order document. It does not decide medical necessity (LCD criteria A-G),
which rests on the visit notes.
"""

import re
from collections.abc import Callable
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel

from dme.models import Case, WrittenOrder


class Defect(BaseModel):
    field: str
    problem: str
    basis: Literal["medicare", "policy"]


def _words(name: str) -> list[str]:
    name = re.sub(r"[^a-z\s-]", " ", name.lower())
    skip = {"dr", "mr", "mrs", "ms", "md", "m", "d", "do"}
    return [w for w in name.split() if w not in skip]


def same_person(a: str, b: str) -> bool:
    """Same first and last name, ignoring titles, case and middle names ('Last, First' ok)."""
    if "," in a and not re.search(r",\s*(M\.?D|D\.?O|N\.?P|P\.?A)\b", a, re.I):
        last, _, first = a.partition(",")
        a = f"{first} {last}"
    x, y = _words(a), _words(b)
    return len(x) >= 2 and len(y) >= 2 and x[0] == y[0] and x[-1] == y[-1]


def valid_npi(npi: str) -> bool:
    """10 digits with a Luhn check over the '80840' prefix (CMS NPI check digit)."""
    if not re.fullmatch(r"\d{10}", npi):
        return False
    total = 0
    for i, d in enumerate(reversed("80840" + npi)):
        n = int(d) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


Basis = Literal["medicare", "policy"]
# (defect field, basis, label, WrittenOrder attribute, is the value acceptable?)
# Add a check by adding a row.
RULES: list[tuple[str, Basis, str, str, Callable[[Any, Case, date], bool]]] = [
    ("patient_name", "medicare", "patient name", "patient_name",
     lambda v, case, today: same_person(v, case.patient.name)),
    ("order_date", "medicare", "order date", "order_date", lambda v, case, today: v <= today),
    ("item_description", "medicare", "item description", "item_description",
     lambda v, case, today: True),
    ("hcpcs_code", "policy", "billing code", "hcpcs_code",
     lambda v, case, today: v.strip().upper() == case.equipment.hcpcs),
    ("quantity", "medicare", "quantity", "quantity",
     lambda v, case, today: v == case.equipment.quantity),
    ("prescriber_name", "policy", "prescriber name", "prescriber_name",
     lambda v, case, today: same_person(v, case.pcp.name)),
    ("prescriber_npi", "policy", "prescriber NPI", "prescriber_npi",
     lambda v, case, today: valid_npi(v)),
    ("signature", "medicare", "prescriber signature", "signature_present",
     lambda v, case, today: True),
    ("signature_date", "policy", "signature date", "signature_date",
     lambda v, case, today: v <= today),
]  # fmt: skip


def check(order: WrittenOrder, case: Case, today: date) -> list[Defect]:
    found = []
    for field, basis, label, attr, ok in RULES:
        if field == "signature_date" and not order.signature_present:
            continue  # already reported as a missing signature
        value = getattr(order, attr)
        if value in (None, "", False):
            found.append(Defect(field=field, basis=basis, problem=f"{label} is missing"))
        elif not ok(value, case, today):
            found.append(Defect(field=field, basis=basis, problem=f"{label} {value} is not right"))
    return found
