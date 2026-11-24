from datetime import date, datetime

from dme import config
from dme.app import build
from dme.models import Fact, Turn, WrittenOrder
from dme.sim.counterparty import Scenario
from dme.sim.offline import OfflineLLM
from dme.store import EventStore

T0 = config.SIM_START
LAKESHORE = "lakeshore-home-medical-equipment"
LINCOLN = "lincoln-park-dme-services"
HALSTED = "halsted-home-health-supply"
WINDY = "windy-city-mobility-dme"


def run(tmp_path, llm=None, **scenario):
    scenario.setdefault("name", "test")
    store = EventStore(tmp_path)
    orch = build(Scenario(**scenario), llm or OfflineLLM(), store)
    view = orch.run()
    return view, store.events(orch.case_id)


def yes(quote="yes", confidence="high"):
    return Fact(value=True, confidence=confidence, evidence_quote=quote)


def no(quote="no", confidence="high"):
    return Fact(value=False, confidence=confidence, evidence_quote=quote)


def said(*lines):
    return [Turn(speaker="counterparty", text=t) for t in lines]


def good_order(**changes):
    fields = dict(
        patient_name="Eleanor Martinez",
        order_date=date(2026, 11, 23),
        item_description="Standard manual wheelchair",
        hcpcs_code="K0001",
        quantity=1,
        prescriber_name="Sarah Chen, MD",
        prescriber_npi="1234567893",
        signature_present=True,
        signature_date=date(2026, 11, 23),
    )
    return WrittenOrder(**(fields | changes))


def states(events):
    return [e.new.value for e in events if e.type == "state_changed"]


def calls(events, call_type):
    return [e for e in events if e.type == "call_completed" and e.call_type == call_type]


def at(d, h=9, m=0):
    return datetime(2026, 11, d, h, m)
