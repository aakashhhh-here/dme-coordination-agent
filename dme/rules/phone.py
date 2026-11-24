"""Phone and fax numbers we will act on. No LLM."""

import re

from dme.models import TextFact


def usable_number(fact: TextFact) -> str | None:
    """A 10-digit number whose digits are in the quote, i.e. were really spoken."""
    digits = re.sub(r"\D", "", fact.value or "").removeprefix("1")
    spoken = re.sub(r"\D", "", fact.evidence_quote)
    return fact.value if fact.known and len(digits) == 10 and digits in spoken else None
