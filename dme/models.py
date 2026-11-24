"""Domain types.

These are the shared vocabulary of the system. The orchestrator, rules and
workflows only ever exchange these typed objects — free text from calls is
converted into them by the extractor before anything acts on it.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --- People and places


class Address(BaseModel):
    street: str
    city: str
    state: str
    zip: str

    @classmethod
    def parse(cls, raw: str) -> Address:
        """Parse 'street, city, ST 12345' (the supplier-directory format)."""
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) != 3:
            raise ValueError(f"Unexpected address format: {raw!r}")
        street, city, state_zip = parts
        state, zip_code = state_zip.split()
        return cls(street=street, city=city, state=state, zip=zip_code)

    def one_line(self) -> str:
        return f"{self.street}, {self.city}, {self.state} {self.zip}"


class Insurance(BaseModel):
    plan: Literal["original_medicare_part_b"] = "original_medicare_part_b"
    supplemental: str | None = None  # Medigap plan name, if any


class Patient(BaseModel):
    name: str
    age: int
    phone: str | None = None
    date_of_birth: date | None = None  # learned when we verify who answered
    texts_ok: bool = False
    # Intake did NOT capture an address. Supplier range can't be judged
    # without it, so the system must obtain it.
    address: Address | None = None
    insurance: Insurance = Insurance()


class PCP(BaseModel):
    name: str
    practice: str
    city: str
    state: str
    phone: str
    npi: str | None = None  # not in intake; must come from the written order


class Supplier(BaseModel):
    """Exactly what the Medicare directory gives us — nothing more, on purpose."""

    supplier_id: str
    name: str
    phone: str
    address: Address


# --- Equipment and orders


class EquipmentRequest(BaseModel):
    description: str
    hcpcs: str  # billing code the claim must carry, e.g. K0001
    quantity: int = 1

    def line(self) -> str:
        return f"{self.description}, billing code {self.hcpcs}, quantity {self.quantity}"


class OrderStatus(StrEnum):
    VERBAL_ONLY = "verbal_only"  # noted in chart, not a valid written order
    REQUESTED = "requested"  # we asked the PCP office for it
    DEFECTIVE = "defective"  # failed rules/order_check — correction needed
    VALID = "valid"  # passed rules/order_check


class WrittenOrder(BaseModel):
    """What we read off a written order. rules/order_check.py decides if it is good."""

    model_config = ConfigDict(json_schema_extra=lambda s: _all_required(s))

    patient_name: str | None = None
    order_date: date | None = None
    item_description: str | None = None
    hcpcs_code: str | None = None
    quantity: int | None = None
    prescriber_name: str | None = None
    prescriber_npi: str | None = None
    signature_present: bool = False
    signature_date: date | None = None


# --- Suppliers as the case sees them


class SupplierStatus(StrEnum):
    UNCONTACTED = "uncontacted"
    NO_ANSWER = "no_answer"
    DECLINED = "declined"  # can't serve her: not taking new pts, no stock, out of area...
    QUALIFIED = "qualified"  # said yes to every screening question
    COMMITTED = "committed"  # agreed to a delivery date against a valid order
    GHOSTED = "ghosted"  # committed, then went silent / never delivered
    DELIVERED = "delivered"


class SupplierRecord(BaseModel):
    supplier_id: str
    status: SupplierStatus = SupplierStatus.UNCONTACTED
    reason: str | None = None
    committed_delivery_date: date | None = None


# --- Call outcomes (produced by the extractor from transcripts)

Confidence = Literal["high", "medium", "low"]


def _all_required(schema: dict) -> None:
    """Make every field mandatory in the JSON schema the LLM must fill.

    Python-side defaults stay (an unfilled fact means "unknown"), but the model
    has to state every field explicitly, including null. It also keeps us under the
    structured-output limit on optional parameters.
    """
    props = schema.get("properties", {})
    schema["required"] = list(props)
    for prop in props.values():
        prop.pop("default", None)


class LLMFilled(BaseModel):
    """Base for anything the extractor LLM fills in."""

    model_config = ConfigDict(json_schema_extra=_all_required)


class FactBase(LLMFilled):
    """One extracted answer, with the transcript quote that supports it.

    The quote lets a human audit the extraction, and low confidence on a
    decision-critical fact is a signal to re-ask or escalate.
    """

    value: Any = None  # narrowed by each subclass
    confidence: Confidence = "low"
    evidence_quote: str = ""

    @property
    def known(self) -> bool:
        return self.value is not None and self.confidence != "low"


class Fact(FactBase):
    value: bool | None = None  # None = not asked / not answered

    @property
    def yes(self) -> bool:
        return self.known and self.value is True

    @property
    def no(self) -> bool:
        return self.known and self.value is False


class IntFact(FactBase):
    value: int | None = None


class DateFact(FactBase):
    value: date | None = None


class TextFact(FactBase):
    value: str | None = None


class AddressFact(FactBase):
    value: Address | None = None


class CallOutcomeBase(LLMFilled):
    connected: bool  # did a human pick up and engage?
    summary: str = ""


class SupplierCallOutcome(CallOutcomeBase):
    call_type: Literal["supplier_screen"] = "supplier_screen"
    taking_new_medicare_patients: Fact = Fact()
    has_k0001_in_stock: Fact = Fact()
    accepts_medicare_assignment: Fact = Fact()
    serves_patient_area: Fact = Fact()
    earliest_delivery_days: IntFact = IntFact()


class SupplierScheduleOutcome(CallOutcomeBase):
    call_type: Literal["supplier_schedule"] = "supplier_schedule"
    can_schedule: Fact = Fact()
    delivery_date: DateFact = DateFact()
    order_fax: TextFact = TextFact()


class SupplierCheckinOutcome(CallOutcomeBase):
    call_type: Literal["supplier_checkin"] = "supplier_checkin"
    on_track_for_committed_date: Fact = Fact()
    new_delivery_date: DateFact = DateFact()  # only if the committed date slipped


class PCPCallOutcome(CallOutcomeBase):
    call_type: Literal["pcp_order"] = "pcp_order"
    office_has_request: Fact = Fact()
    order_will_be_sent: Fact = Fact()


class PatientCallOutcome(CallOutcomeBase):
    call_type: Literal["patient_intake"] = "patient_intake"
    address: AddressFact = AddressFact()
    phone_for_delivery: TextFact = TextFact()
    texts_ok: Fact = Fact()
    address_confirmed: Fact = Fact()  # after it was read back
    wants_to_proceed: Fact = Fact()
    understands_cost_share: Fact = Fact()


class DeliveryConfirmationOutcome(CallOutcomeBase):
    call_type: Literal["patient_confirm"] = "patient_confirm"
    delivery_received: Fact = Fact()  # the right chair, in hand
    wrong_or_damaged_item_arrived: Fact = Fact()


class PatientUpdateOutcome(CallOutcomeBase):
    call_type: Literal["patient_update"] = "patient_update"
    understood: Fact = Fact()


class IdentityOutcome(CallOutcomeBase):
    call_type: Literal["identity"] = "identity"
    is_patient: Fact = Fact()  # the named patient, not a relative or caregiver
    date_of_birth: DateFact = DateFact()


CallOutcome = Annotated[
    SupplierCallOutcome | SupplierScheduleOutcome | SupplierCheckinOutcome | PCPCallOutcome
    | PatientCallOutcome | PatientUpdateOutcome | DeliveryConfirmationOutcome,
    Field(discriminator="call_type"),
]  # fmt: skip


CALL_SCHEMAS: dict[str, type[CallOutcomeBase]] = {
    "patient_intake": PatientCallOutcome,
    "patient_update": PatientUpdateOutcome,
    "patient_confirm": DeliveryConfirmationOutcome,
    "supplier_screen": SupplierCallOutcome,
    "supplier_schedule": SupplierScheduleOutcome,
    "supplier_checkin": SupplierCheckinOutcome,
    "pcp_request": PCPCallOutcome,
    "pcp_followup": PCPCallOutcome,
    "pcp_correction": PCPCallOutcome,
}


class Turn(BaseModel):
    speaker: Literal["agent", "counterparty"]
    text: str


# --- The case


class CaseState(StrEnum):
    NEW = "new"
    NEED_PATIENT_INFO = "need_patient_info"
    WORKING = "working"  # supplier sourcing ∥ order chase
    READY_TO_MATCH = "ready_to_match"  # qualified supplier AND valid order
    DELIVERY_SCHEDULED = "delivery_scheduled"
    AWAITING_DELIVERY_CONFIRMATION = "awaiting_delivery_confirmation"
    CLOSED = "closed"
    ESCALATED = "escalated"


class Case(BaseModel):
    case_id: str
    patient: Patient
    pcp: PCP
    equipment: EquipmentRequest
    pcp_visit_date: date
    order_status: OrderStatus
    written_order: WrittenOrder | None = None
    state: CaseState = CaseState.NEW
    suppliers: dict[str, SupplierRecord] = {}
