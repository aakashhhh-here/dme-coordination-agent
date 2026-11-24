"""Order validation, ranking, screening. Pure functions."""

from datetime import date

import pytest

from dme import config
from dme.data_loader import load_case, load_suppliers
from dme.models import (
    DateFact,
    IdentityOutcome,
    IntFact,
    Patient,
    SupplierCallOutcome,
    SupplierStatus,
)
from dme.rules.geo import locate, miles_between
from dme.rules.identity import verified_dob
from dme.rules.order_check import check, same_person, valid_npi
from dme.rules.ranking import rank_suppliers
from dme.rules.screening import CRITICAL, screen_supplier
from tests.helpers import HALSTED, LAKESHORE, LINCOLN, WINDY, good_order, no, yes

TODAY = date(2026, 11, 24)


def test_good_order_passes():
    assert check(good_order(), load_case(), TODAY) == []


@pytest.mark.parametrize(
    "changes, field",
    [
        ({"hcpcs_code": "K0003"}, "hcpcs_code"),
        ({"hcpcs_code": None}, "hcpcs_code"),
        ({"patient_name": "Elena Martinez"}, "patient_name"),
        ({"patient_name": "Martinez"}, "patient_name"),
        ({"patient_name": None}, "patient_name"),
        ({"prescriber_name": "Dr. Alan Wu"}, "prescriber_name"),
        ({"prescriber_name": None}, "prescriber_name"),
        ({"prescriber_npi": None}, "prescriber_npi"),
        ({"prescriber_npi": "1234567890"}, "prescriber_npi"),
        ({"signature_present": False}, "signature"),
        ({"signature_date": None}, "signature_date"),
        ({"signature_date": date(2027, 1, 1)}, "signature_date"),
        ({"order_date": None}, "order_date"),
        ({"order_date": date(2027, 1, 1)}, "order_date"),
        ({"item_description": None}, "item_description"),
        ({"quantity": None}, "quantity"),
        ({"quantity": 2}, "quantity"),
    ],
)
def test_each_defect_is_caught_alone(changes, field):
    assert [d.field for d in check(good_order(**changes), load_case(), TODAY)] == [field]


def test_all_defects_reported_together():
    bad = good_order(hcpcs_code="K0003", signature_present=False, prescriber_npi=None)
    fields = {d.field for d in check(bad, load_case(), TODAY)}
    assert fields == {"hcpcs_code", "signature", "prescriber_npi"}


def test_medicare_vs_house_policy_is_labelled():
    d = check(good_order(signature_present=False), load_case(), TODAY)[0]
    assert d.basis == "medicare"
    d = check(good_order(prescriber_npi=None), load_case(), TODAY)[0]
    assert d.basis == "policy"  # Medicare accepts name OR NPI


@pytest.mark.parametrize(
    "a, b, same",
    [
        ("Dr. Sarah Chen", "Sarah Chen, MD", True),
        ("SARAH CHEN M.D.", "Dr. Sarah Chen", True),
        ("Martinez, Eleanor", "Eleanor Martinez", True),
        ("Eleanor R. Martinez", "Eleanor Martinez", True),
        ("Eleanor Martin", "Eleanor Martinez", False),
        ("", "Eleanor Martinez", False),
    ],
)
def test_same_person(a, b, same):
    assert same_person(a, b) is same


def test_npi_check_digit():
    assert valid_npi("1234567893")
    assert not valid_npi("1234567890")
    assert not valid_npi("123456789")


def test_ranking_is_nearest_first_with_stable_ties():
    ids = [s.supplier_id for s, _ in rank_suppliers(load_suppliers(), "60614")]
    assert ids[:4] == [LAKESHORE, LINCOLN, HALSTED, WINDY]
    assert len(ids) == 12
    miles = [m for _, m in rank_suppliers(load_suppliers(), "60614")]
    assert miles == sorted(miles)


def test_ranking_drops_suppliers_beyond_the_radius():
    near = rank_suppliers(load_suppliers(), "60614", max_miles=5)
    assert 0 < len(near) < 12 and all(m <= 5 for _, m in near)
    assert rank_suppliers(load_suppliers(), "62701") == []
    assert rank_suppliers(load_suppliers(), "99999") == []


def test_every_supplier_zip_is_on_the_map():
    assert all(locate(s.address.zip) for s in load_suppliers())


def test_distance_is_sane():
    assert miles_between("60614", "60614") == 0
    assert 8 < miles_between("60614", "60201") < 11  # Lincoln Park to Evanston
    assert miles_between("60614", "62701") == pytest.approx(176, abs=10)


def outcome(**kw):
    facts = {f: yes() for f, _ in CRITICAL} | kw
    return SupplierCallOutcome(connected=True, **facts)


def test_all_yes_qualifies():
    assert screen_supplier(outcome()) == (SupplierStatus.QUALIFIED, None)


@pytest.mark.parametrize("field, reason", CRITICAL)
def test_each_clear_no_declines(field, reason):
    assert screen_supplier(outcome(**{field: no()})) == (SupplierStatus.DECLINED, reason)


@pytest.mark.parametrize("field, _", CRITICAL)
@pytest.mark.parametrize("fact", [yes(confidence="low"), no(confidence="low")])
def test_low_confidence_is_neither_yes_nor_no(field, _, fact):
    got = screen_supplier(outcome(**{field: fact}))
    assert got == (SupplierStatus.NO_ANSWER, "unclear_answers")


def test_too_far_out_declines_and_no_answer():
    far = IntFact(value=config.MAX_DELIVERY_DAYS_OUT + 1, confidence="high", evidence_quote="x")
    assert screen_supplier(outcome(earliest_delivery_days=far))[0] is SupplierStatus.DECLINED
    assert screen_supplier(SupplierCallOutcome(connected=False))[0] is SupplierStatus.NO_ANSWER


@pytest.mark.parametrize("dob,on_file,ok", [
    (date(1954, 3, 14), None, True),
    (date(1954, 11, 24), None, False),  # still 71 today
    (date(1953, 11, 24), None, True),  # turns 73 tomorrow
    (date(1953, 11, 23), None, False),  # 73 today
    (date(1954, 3, 15), date(1954, 3, 14), False),  # right age, not the same date as before
    (date(1954, 3, 14), date(1954, 3, 14), True),
])  # fmt: skip
def test_identity_needs_the_person_and_the_date(dob, on_file, ok):
    patient = Patient(name="Eleanor Martinez", age=72, date_of_birth=on_file)
    when = DateFact(value=dob, confidence="high", evidence_quote="x")
    today = date(2026, 11, 23)
    out = IdentityOutcome(connected=True, is_patient=yes(), date_of_birth=when)
    assert verified_dob(out, patient, today) == (dob if ok else None)
    out.is_patient = no()  # a relative who knows the date is still not the patient
    assert verified_dob(out, patient, today) is None
