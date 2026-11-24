# Writeup

## Sequencing

1. **Event log, clock, state machine with stubs.** Everything plugs into these, and they prove the
   control flow before any model is involved.
2. **Call, then extract, then ground.** This does the actual work, so it came before any workflow.
3. **Workflows by dependency:** address, then sourcing alongside the order chase, then delivery.
4. **Failure modes as scenarios.** Lost orders, wrong codes and ghosting are the job, not the edges.

## Architecture

```
 timer due -> Orchestrator.step -> workflow handler -> Result(events, timers, escalate?)
                    |                     |
                    |                 Phone -> call_agent <-> person -> extractor -> grounding
                    v
            apply events, follow guards, append the batch to the log
```

- **Handlers propose, the orchestrator decides.** A handler gets a view and returns a `Result`. It
  can't write the log or change state; the orchestrator rejects it if it tries.
- **A step is one write.** A crash leaves its timer open, so a restart redoes that step. The cost is
  that a call in progress at the moment of a crash may be placed twice.
- **"Unknown" is its own answer.** It never reads as yes or no. It earns a retry, then a human.
- **The order reaches the supplier.** A case can't move to `DELIVERY_SCHEDULED` until the signed
  order has been sent to the supplier that committed.
- **Know who answered.** Patient facts are withheld from the agent until code has checked the date
  of birth. No consent to texts means delivery news comes by phone instead.
- **Minimum necessary.** Suppliers hear only a ZIP code until they qualify and the order is valid.

How the code follows SOLID:

| | |
|---|---|
| Single responsibility | `events` (facts), `view` (replay), `store` (file), `rules/` (decisions), one module per workflow |
| Open/closed | Order checks and screening questions are tables: add a row. New task kinds register a handler |
| Liskov | `ClaudeLLM`, `ClaudeCLILLM`, `OfflineLLM` and the tests' `FakeLLM` are interchangeable |
| Interface segregation | Small protocols: `LLM`, `Handler`, `PhoneLine`, `FaxInbox`, `OrderOutbox`, `SupplierSourcer`, `DeliveryMonitor` |
| Dependency inversion | Workflows depend on those protocols; only `app.py` knows the concrete classes |

## Cut

- Parallel supplier calls, a backup supplier kept warm, supplier memory across cases.
- Resuming an escalated case after a human decides. Today `ESCALATED` is final, and it also stops
  the parallel work (for example, the order chase when suppliers run out).
- Earlier ghosting checks. One check-in the business day before delivery is late if the date is far out.
- More than one reschedule: a supplier may move its date once; a second slip gets it replaced.
- Capped rental, deductible status and proof-of-delivery paperwork. The cost message is general.

## Limits of the evidence

- Offline tests prove control flow and safety rules, not model quality. The live tests do that on
  simulated people, who are more articulate than real ones. No real calls were made.
- Quote grounding catches invented answers, not a real quote that was misunderstood.

## Next

1. Human-resolution events, so an escalated case continues.
2. B3 (more delivery checkpoints) and B5 (backup supplier): the largest cut in patient waiting time.
3. A graded set of real, consented transcripts to measure extraction before any live calls.
4. B6: a telephony adapter behind `PhoneLine`, with call-consent and recording rules by state.
