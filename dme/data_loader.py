"""Load the intake case and the Medicare supplier directory."""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from dme import config
from dme.models import (
    PCP,
    Address,
    Case,
    EquipmentRequest,
    OrderStatus,
    Patient,
    Supplier,
    SupplierRecord,
)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def load_suppliers(path: Path = config.DATA_DIR / "suppliers.csv") -> list[Supplier]:
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    suppliers = [
        Supplier(
            supplier_id=slugify(row["supplier_name"]),
            name=row["supplier_name"],
            phone=row["phone"],
            address=Address.parse(row["address"]),
        )
        for row in rows
    ]
    ids = [s.supplier_id for s in suppliers]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate supplier ids in directory")
    return suppliers


def load_case(
    path: Path = config.DATA_DIR / "case_eleanor.json",
    suppliers: list[Supplier] | None = None,
    as_of: datetime = config.SIM_START,
) -> Case:
    """Build the Case from intake JSON.

    Intake records the PCP visit relatively ("3 days ago"), so it is
    resolved against the simulation start time for reproducible runs.
    """
    raw = json.loads(path.read_text())
    suppliers = suppliers if suppliers is not None else load_suppliers()
    visit_date = (as_of - timedelta(days=raw["pcp_visit_days_before_start"])).date()
    return Case(
        case_id=raw["case_id"],
        patient=Patient.model_validate(raw["patient"]),
        pcp=PCP.model_validate(raw["pcp"]),
        equipment=EquipmentRequest.model_validate(raw["equipment"]),
        pcp_visit_date=visit_date,
        order_status=OrderStatus(raw["order_status"]),
        suppliers={s.supplier_id: SupplierRecord(supplier_id=s.supplier_id) for s in suppliers},
    )
