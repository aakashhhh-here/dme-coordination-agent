"""Intake case + supplier directory load correctly, and stay sparse."""

from datetime import date

import pytest

from dme.data_loader import load_case, load_suppliers
from dme.models import Address, Case, CaseState, OrderStatus, SupplierCallOutcome, SupplierStatus


@pytest.fixture(scope="module")
def suppliers():
    return load_suppliers()


@pytest.fixture(scope="module")
def case(suppliers):
    return load_case(suppliers=suppliers)


def test_loads_all_twelve_suppliers(suppliers):
    assert len(suppliers) == 12
    assert len({s.supplier_id for s in suppliers}) == 12


def test_supplier_addresses_parse(suppliers):
    by_id = {s.supplier_id: s for s in suppliers}
    lakeshore = by_id["lakeshore-home-medical-equipment"]
    assert lakeshore.phone == "(312) 555-0142"
    assert lakeshore.address == Address(
        street="1820 N Clark St", city="Chicago", state="IL", zip="60614"
    )
    assert by_id["evanston-home-medical"].address.city == "Evanston"
    assert all(len(s.address.zip) == 5 for s in suppliers)


def test_directory_stays_sparse(suppliers):
    # The brief: name, phone, address only. No invented stock/network fields.
    assert set(type(suppliers[0]).model_fields) == {"supplier_id", "name", "phone", "address"}


def test_eleanor_case_matches_intake(case):
    assert case.patient.name == "Eleanor Martinez"
    assert case.patient.age == 72
    assert case.patient.insurance.plan == "original_medicare_part_b"
    assert case.patient.insurance.supplemental is None
    assert case.pcp.name == "Dr. Sarah Chen"
    assert case.pcp.phone == "(312) 555-0198"
    assert case.equipment.hcpcs == "K0001"
    assert case.order_status is OrderStatus.VERBAL_ONLY
    assert case.written_order is None
    assert case.state is CaseState.NEW


def test_patient_address_is_missing(case):
    # Deliberate gap in intake: the system has to go get it.
    assert case.patient.address is None


def test_pcp_visit_resolved_against_sim_start(case):
    assert case.pcp_visit_date == date(2026, 11, 20)  # SIM_START (Nov 23) - 3 days


def test_every_supplier_starts_uncontacted(case, suppliers):
    assert set(case.suppliers) == {s.supplier_id for s in suppliers}
    assert all(r.status is SupplierStatus.UNCONTACTED for r in case.suppliers.values())


def test_case_round_trips_through_json(case):
    assert Case.model_validate_json(case.model_dump_json()) == case


def test_call_outcome_defaults_are_unknown_not_false():
    # An unanswered question must not read as "no".
    outcome = SupplierCallOutcome(connected=False)
    assert outcome.has_k0001_in_stock.value is None
    assert outcome.has_k0001_in_stock.confidence == "low"
