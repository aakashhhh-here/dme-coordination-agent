# DME back-end coordination

Runs Eleanor Martinez's K0001 wheelchair case from intake to confirmed delivery with no care
advocate, and hands a human one specific decision when it truly can't go on.

## Setup

```
uv sync
export ANTHROPIC_API_KEY=...            # or: export DME_LLM_BACKEND=claude-cli  (logged-in Claude Code)
uv run python scripts/smoke_llm.py      # checks credentials
```

## Run

```
uv run python run_demo.py --scenario happy
uv run python run_demo.py --scenario messy_but_recovers --transcripts
uv run python run_demo.py --scenario must_escalate
uv run python run_demo.py --scenario patient_unreachable
uv run python scripts/try_call.py --persona supplier_out_of_stock
```

Add `--offline` to run free with a rule-based stand-in instead of Claude. `run_demo.py` exits 1 if a
scenario doesn't end as its YAML says. Each run's full event log is in `data/cases/`.

## Test

```
uv run pytest                                             # offline, about 5 s
uv run pytest -m llm                                      # real Claude, costs money
DME_LLM_BACKEND=claude-cli uv run pytest -m llm           # same, without an API key
```

`tests/test_invariants.py` is the main safety evidence: 300 random worlds (half with the model
failing at random) must never match without a valid order, call out of hours, rely on two suppliers
at once, text without consent, believe an unverified voice, or get stuck. `tests/test_ten_cases.py`
runs twelve whole cases, best to worst, offline and on real Claude; run the live ones a few at a time. Restarting from the log after every step must end in the identical state.

## Real and mocked

| Real | Mocked |
|---|---|
| Claude calls: conversation, extraction, message wording | Phone system (`PhoneLine` in `workflows/base.py`) |
| Rules, orchestrator, retries, escalation | The people called: LLM personas in `sim/personas.yaml` |
| Event log, resume after a crash | Fax/portal (`FaxInbox`), message sending |
| | Sending the order to the supplier (`OrderOutbox`) |
| | Geocoding (ZIP-centroid table), time (`SimClock`) |

## Where AI is, and isn't

- **LLM:** holds calls, turns transcripts and faxes into typed records, words patient messages.
- **No LLM:** state changes, order validation, supplier ranking and qualification, timing,
  escalation, cost figures.
- **Between them:** every extracted fact needs a quote the other party really said, or it drops to
  low confidence and counts as unknown. Codes and NPIs must appear in the document. A drafted
  message with a date, number or money word outside its facts is replaced by a template.

## Assumptions

- Intake has no patient phone number. We assume the intake channel can reach the patient; we ask
  for a delivery phone on the first call.
- Intake has an age but no date of birth. The first call can only check that the one given fits the
  age; later calls must match it exactly. Until then the agent isn't given any case facts.
- Our own fax number is made up (`config.OUR_FAX`).
- Medicare requires prescriber name *or* NPI, and no HCPCS code or signature date. We require all
  of them as house policy; defects are tagged `medicare` or `policy` (`rules/order_check.py`).
- We check the order document, not medical necessity (LCD L33788), which rests on the visit notes.
- The NPI is a standard documentation example, so it can't be checked against a registry.
