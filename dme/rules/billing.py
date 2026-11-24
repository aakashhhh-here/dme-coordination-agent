"""Will the supplier's claim carry the code that was ordered? No LLM.

A claim whose code differs from the order is rejected after delivery, when it is hardest to fix.
"""

import re

from dme.models import EquipmentRequest, TextFact


def _codes(text: str) -> set[str]:
    return {re.sub(r"[\s-]", "", c).upper() for c in re.findall(r"\b[a-z][\s-]?\d{4}\b", text, re.I)}


def claim_code_ok(fact: TextFact, item: EquipmentRequest) -> bool | None:
    """True or False only on a clear answer. None: not said, unclear, or two codes named."""
    if not fact.known or not fact.value:
        return None
    named, value = _codes(fact.evidence_quote), _codes(fact.value)
    if len(value) != 1 or len(named) > 1 or (named and named != value):
        return None
    return value == {item.hcpcs}
