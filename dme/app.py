"""Wire the real pieces together for one case + scenario."""

import os

from dme.data_loader import load_case, load_suppliers
from dme.llm.claude_cli import ClaudeCLILLM
from dme.llm.client import LLM, ClaudeLLM
from dme.orchestrator import Orchestrator
from dme.sim.counterparty import Scenario, World
from dme.sim.offline import OfflineLLM
from dme.store import EventStore
from dme.workflows.base import Phone
from dme.workflows.delivery import Scheduler, SimpleMonitor
from dme.workflows.messages import Messenger
from dme.workflows.order import OrderWorkflow
from dme.workflows.patient import PatientUpdates, PatientWorkflow
from dme.workflows.sourcing import SimpleSourcer


def make_llm(offline: bool = False) -> LLM:
    if offline:
        return OfflineLLM()
    if os.environ.get("DME_LLM_BACKEND") == "claude-cli":
        return ClaudeCLILLM()
    return ClaudeLLM()


def build(scenario: Scenario, llm: LLM, store: EventStore, on_event=None) -> Orchestrator:
    suppliers = load_suppliers()
    case = load_case(suppliers=suppliers)
    case.case_id = f"{case.case_id}--{scenario.name}"  # one log per demo scenario
    world = World(llm, scenario)
    phone = Phone(llm, world, suppliers)
    monitor = SimpleMonitor(phone, world)
    orch = Orchestrator(
        case=case,
        store=store,
        sourcer=SimpleSourcer(phone, suppliers),
        patient=PatientWorkflow(phone),
        order=OrderWorkflow(phone, world, llm),
        scheduler=Scheduler(phone, world, monitor),
        monitor=monitor,
        updates=PatientUpdates(phone, suppliers),
        messenger=Messenger(llm, suppliers),
        on_event=on_event,
    )
    llm.on_call = orch.record_llm_call
    return orch
