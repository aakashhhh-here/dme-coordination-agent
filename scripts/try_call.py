"""Run one supplier screening call and print the transcript and extracted record.

Usage: uv run python scripts/try_call.py --persona supplier_out_of_stock [--offline]
"""

import argparse

from dme import config
from dme.app import make_llm
from dme.clock import Task
from dme.data_loader import load_case, load_suppliers
from dme.events import CaseOpened
from dme.models import Address
from dme.rules.screening import screen_supplier
from dme.sim.counterparty import DEFAULT_ADDRESS, Scenario, World, personas
from dme.view import CaseView
from dme.workflows.base import Phone
from dme.workflows.sourcing import SimpleSourcer


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--persona", choices=sorted(personas()["supplier"]), default="supplier_happy"
    )
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    llm = make_llm(args.offline)
    suppliers = load_suppliers()
    case = load_case(suppliers=suppliers)
    case.patient.address = Address.parse(DEFAULT_ADDRESS)
    view = CaseView()
    view.apply(CaseOpened(case=case))
    world = World(llm, Scenario(name="try", default_supplier=args.persona))
    sourcer = SimpleSourcer(Phone(llm, world, suppliers), suppliers)
    task = Task(kind="supplier_screen", supplier_id=suppliers[0].supplier_id)
    call = sourcer.handle(task, view, config.SIM_START).events[0]
    for t in call.transcript:
        print(f"{t.speaker:>12}: {t.text}")
    print(call.outcome.model_dump_json(indent=2))
    print("=>", *screen_supplier(call.outcome))


if __name__ == "__main__":
    main()
