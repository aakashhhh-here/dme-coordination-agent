# Writeup

## Sequencing

The build was sized at about 4¼ hours at planning time (an estimate, against the suggested 3).
Hardening and review of live transcripts came on top of that.

1. **Event log, clock, state machine with stub handlers.** Everything plugs into these, and they
   prove the control flow before any model is involved.
2. **One call: talk, extract, ground.** The riskiest part. If a conversation can't become facts code
   can trust, nothing else matters, so it came before any workflow.
3. **Workflows in dependency order:** patient, then suppliers and the doctor's order side by side,
   then delivery.
4. **Failure modes as whole-case scenarios.** Lost requests, wrong codes and ghosting are the job,
   not the edges. Reading live transcripts then found gaps that tests had not: the signed order was
   never actually sent, and anyone answering the phone was believed.

I went for the whole loop rather than one deep piece because the delays live in the handoffs.
What I protected from cuts: extraction, order validation, and escalation.

## Technology & architecture

```
 timer due -> Orchestrator.step -> workflow handler -> Result(events, timers, escalate?)
                    |                     |
                    |                 Phone -> call_agent <-> person -> extractor -> grounding
                    v
            apply events, follow guards, append the batch to the log
```

| Choice | Why |
|---|---|
| Plain Python state machine, no agent framework | The flow is known in advance, and a reviewer can read every transition in one file. A model choosing the next step would be harder to test and audit |
| Claude (Opus 5.5 by default) | Calls need judgment and extraction needs care. The agent and simulator models are one line each in `config.py` |
| Pydantic | One set of types for structured output, events and rules |
| JSONL event log | The brief allows a file; replay gives crash recovery and an audit trail |
| Simulated clock | Days of follow-ups run in minutes, and tests are repeatable |

- **The LLM talks and reads; code decides.** Each extracted fact needs a quote the other party
  really said, or it counts as unknown. Unknown is never yes or no: it earns a retry, then a human.
- **Handlers propose, the orchestrator decides.** A handler returns a `Result`; only the
  orchestrator writes the log or changes state.
- **A step is one write.** A crash redoes that step. The cost: a call in progress may be placed twice.
- **The order doesn't wait for the patient.** It needs nothing from them, so it starts 15 minutes
  in. The cost: the office may do paperwork for someone who then declines.
- **The code is checked at both ends:** on the doctor's order, and on what the supplier says it will
  bill. A supplier gets the signed order only after naming the right code.
- **Know who answered.** Case facts are withheld until code has checked the date of birth. Suppliers
  hear only a ZIP code until they qualify and the order is valid.

## The cut list

| Cut | Why |
|---|---|
| Real telephony and fax | Adds integration work without testing the decisions; it sits behind small interfaces |
| Parallel supplier calls | Saves simulated minutes. Order follow-ups and ghosting cost days |
| Backup supplier kept warm | Needs a policy on holding two suppliers without misleading either |
| Supplier memory across cases | One case has no history to learn from |
| Resuming an escalated case | Needs a human-facing step. Today `ESCALATED` is final and also stops unaffected work, the largest gap against the north star |
| More than one delivery check-in | One, the business day before, catches silence, but late if the date is far out |
| A second reschedule | One slip is cheaper than starting over; a second is a pattern |
| Capped rental, deductible status, proof of delivery | Billing detail beyond the ~20% message. The cost message stays general |
| Medical necessity | Rests on the doctor's visit notes; we check the order document only |

**Limits of the evidence.** Offline tests prove control flow and safety rules, not model quality. Live
runs use simulated people, who are tidier than real ones. No real calls were made. Grounding catches
invented answers, not a real quote that was misunderstood. The billing check is the supplier's word;
we never see the claim.

## What's next

**One more day**
1. **Resume after a human decides**, and keep unaffected work running meanwhile. A human should
   answer one question, not inherit the case.
2. **Earlier delivery check-ins:** confirm within a day of committing, then midway. Ghosting is
   found only by chasing, so chase sooner.

**Two weeks**
1. **A graded set of real, consented transcripts**, with extraction accuracy measured per field.
   It decides whether any live call is safe, so it comes first.
2. **A backup supplier**, so a ghosting costs hours instead of another round of calls.
3. **A phone adapter**, with call-consent and recording rules by state, starting with supplier calls:
   no patient facts are shared while screening.

Why this order: first finish the loop, so a case can always end; then prove we can trust what we
hear; then put it on real phones.
