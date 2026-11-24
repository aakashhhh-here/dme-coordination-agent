"""Step 10: the cost figure is code's; the LLM may not add facts."""

import pytest

from dme.data_loader import load_case
from dme.llm.client import LLMError
from dme.rules.cost import cost_line
from dme.workflows.messages import TEMPLATES, Messenger, unsupported_claims
from tests.fake_llm import FakeLLM
from tests.helpers import run

FACTS = {
    "item": "standard manual wheelchair", "doctor": "Dr. Sarah Chen", "supplier": "Acme",
    "supplier_phone": "(312) 555-0142", "delivery_date": "Wednesday, November 25, 2026",
}  # fmt: skip
GOOD = "Acme will bring your wheelchair on Wednesday, November 25, 2026."


def send(draft, milestone="delivery_scheduled"):
    return Messenger(FakeLLM({f"message:{milestone}": [draft]}), []).send(
        milestone, FACTS, load_case()
    )


def test_cost_line_is_computed():
    line = cost_line(load_case())
    assert "80%" in line and "20%" in line and "no Medigap" in line
    case = load_case()
    case.patient.insurance.supplemental = "Plan G"
    assert "Medigap" not in cost_line(case)


def test_good_draft_is_used_with_code_cost_line():
    m = send(GOOD)
    assert m.source == "llm" and m.text == f"{GOOD} {cost_line(load_case())}"


@pytest.mark.parametrize(
    "draft",
    [
        GOOD + " It costs $50.",
        GOOD + " It's free.",
        GOOD + " Medicare pays most of it.",
        GOOD + " They come at 9.",
        GOOD + " Call them with any questions.",
        "Acme will come on Thursday, not Wednesday, November 25, 2026.",
        "Acme will bring your wheelchair tomorrow.",
        "Acme will bring your wheelchair soon.",  # drops the date
        LLMError("down"),
    ],
)
def test_bad_draft_falls_back_to_template(draft):
    m = send(draft)
    assert m.source == "template"
    assert m.text == TEMPLATES["delivery_scheduled"].format_map(FACTS) + " " + cost_line(
        load_case()
    )


def test_templates_pass_their_own_check():
    for t in TEMPLATES.values():
        assert unsupported_claims(t.format_map(FACTS), FACTS) == []


def test_all_milestones_sent_once_on_happy_path(tmp_path):
    view, _ = run(tmp_path)
    assert sorted(m.milestone for m in view.messages) == [
        "delivered", "delivery_scheduled", "order_confirmed", "supplier_found",
    ]  # fmt: skip
    with_cost = [m.milestone for m in view.messages if m.text.endswith(cost_line(view.case))]
    assert with_cost == ["delivery_scheduled"]
    assert not any("%" in m.text for m in view.messages if m.milestone not in with_cost)


def test_patient_is_told_when_a_supplier_falls_through(tmp_path):
    for i, p in enumerate(["supplier_yes_then_ghost", "supplier_refuses_to_schedule"]):
        view, _ = run(tmp_path, name=f"t{i}", suppliers={"lakeshore-home-medical-equipment": p})
        assert "delivery_problem" in [m.milestone for m in view.messages]
