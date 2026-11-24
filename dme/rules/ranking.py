"""Rank suppliers by distance from the patient. No LLM."""

from dme import config
from dme.models import Supplier
from dme.rules.geo import miles_between


def rank_suppliers(
    suppliers: list[Supplier], patient_zip: str, max_miles: float = config.MAX_SUPPLIER_MILES
) -> list[tuple[Supplier, float]]:
    """Nearest first; drops anyone too far (or whose ZIP we can't place)."""
    ranked = []
    for s in suppliers:
        miles = miles_between(patient_zip, s.address.zip)
        if miles is not None and miles <= max_miles:
            ranked.append((s, miles))
    return sorted(ranked, key=lambda pair: (pair[1], pair[0].supplier_id))
