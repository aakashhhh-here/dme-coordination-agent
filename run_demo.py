"""Run one scenario end to end on the simulated clock and print what happened.

uv run python run_demo.py --scenario happy              # real Claude calls
uv run python run_demo.py --scenario happy --offline    # free, no model
"""

from __future__ import annotations

import argparse
import sys

from dme import config
from dme.app import build, make_llm
from dme.events import Event
from dme.sim.counterparty import Scenario
from dme.store import EventStore
from dme.view import CaseView


def describe(e: Event) -> str | None:
    match e.type:
        case "state_changed":
            return f"STATE    {e.old.value} -> {e.new.value}  ({e.reason})"
        case "call_completed":
            return f"CALL     {e.call_type} {e.party_id}: " + (
                e.outcome.summary if e.connected else "no answer"
            )
        case "supplier_updated":
            extra = e.reason or (e.committed_delivery_date or "")
            return f"SUPPLIER {e.supplier_id}: {e.status.value.upper()} {extra}".rstrip()
        case "patient_address_set":
            return f"ADDRESS  {e.address.one_line()}"
        case "order_received":
            return "ORDER    received: " + ("; ".join(d.problem for d in e.defects) or "valid")
        case "order_sent":
            return f"ORDER    order and delivery details faxed to {e.supplier_id} {e.fax}"
        case "order_withdrawn":
            return f"ORDER    withdrawn from {e.supplier_id}"
        case "message_sent":
            return f"MESSAGE  [{e.milestone}, {e.source}] {e.text}"
        case "task_failed":
            return f"FAILED   {e.task.kind}: {e.error}"
        case "escalated":
            return f"ESCALATE {e.reason}"
    return None


def print_summary(view: CaseView, events: list[Event]) -> None:
    print(f"\nRESULT: {view.state.value.upper()}")
    for esc in view.escalations:
        print(f"  Reason:   {esc.reason} {esc.detail}")
        print(f"  Decision: {esc.decision_needed}")
        print(f"  Order:    {esc.snapshot['order_status']} {esc.snapshot['order_defects'] or ''}")
        print("  Tried:")
        for line in esc.tried:
            print(f"    - {line}")
    used = [e for e in events if e.type == "llm_called"]
    tokens = sum(e.input_tokens + e.output_tokens for e in used)
    print(f"  Model calls: {len(used)} ({tokens} tokens)")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    names = sorted(p.stem for p in config.SCENARIOS_DIR.glob("*.yaml"))
    parser.add_argument("--scenario", choices=names, default="happy")
    parser.add_argument(
        "--offline", action="store_true", help="rule-based stand-in, no Claude calls"
    )
    parser.add_argument("--transcripts", action="store_true", help="print every call word for word")
    args = parser.parse_args()

    llm = make_llm(args.offline)
    scenario = Scenario.load(args.scenario)
    print(f"{scenario.name}: {scenario.description}\n")

    def show(e: Event) -> None:
        if line := describe(e):
            print(f"{e.at:%a %b %d %H:%M}  {line}", flush=True)
        if args.transcripts and e.type == "call_completed":
            for t in e.transcript:
                print(f"{'':18}{t.speaker:>12}: {t.text}")

    store = EventStore()
    store.path(f"eleanor-martinez-k0001--{scenario.name}").unlink(missing_ok=True)  # fresh run
    orch = build(scenario, llm, store, on_event=show)
    view = orch.run()
    print_summary(view, store.events(orch.case_id))
    reason = view.escalations[-1].reason if view.escalations else None
    if (view.state.value, reason) != (scenario.expect_state, scenario.expect_reason):
        print(
            f"UNEXPECTED: wanted {scenario.expect_state} / {scenario.expect_reason}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
