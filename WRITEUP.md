# Writeup

## Sequencing

**What I built:** the whole loop, from intake to the patient confirming the chair arrived. The delays
in this job happen between the steps, so automating one step well would have left most of the problem.

**How I decided the order:** at each point, build what the next piece can't work without.

1. **First, the part that keeps a case moving** (states, timers, log). A case runs for days and mostly
   waits, so something has to remember what's owed and when. I tested it with fake calls.
2. **Then one real call, before any workflow.** Every step in this job is a phone call. If I couldn't
   get facts I trust out of one, there was no point building the rest.
3. **Then the steps, in the order a case needs them.** The address comes first because suppliers can't
   be ranked without it. The doctor's order needs nothing from the patient, so it runs in parallel.
   Delivery is last because it needs both a supplier and a valid order.
4. **Then your failure list, one by one.** Recovery only means something once there's a working path
   to recover to.

The plan came to about 4¼ hours, over your 3-hour box. Hardening took more time after that.

## Technology & architecture

```
timer fires -> orchestrator -> workflow -> phone call -> extract facts -> check quotes
                    ^                                                          |
                    +------- rules decide, state moves, log is written <-------+
```

**Claude talks and reads. Code decides.** Claude holds the calls, turns them into facts and words the
texts. It never picks a supplier, approves an order, changes state or escalates. Every fact needs a
quote the other person actually said. No quote means "unknown", and unknown is never a yes.

- **Python, no agent framework.** The steps are known. I'd rather read the flow in one file than
  debug a model choosing what to do next.
- **Claude Opus 5.5** for the calls and extraction. A cheaper model may be enough; I haven't
  measured it.
- **Pydantic**, so the model output, the log and the rules share one set of types.
- **JSONL log.** You said a file is fine. Replaying it gives me crash recovery and an audit trail.
- **Simulated clock**, so a week of follow-ups runs in minutes.

Three calls you may disagree with:

- **I ask the doctor for the order before reaching the patient.** It saves days when she's slow to
  pick up. The office may do paperwork for someone who then says no.
- **No order goes to a supplier until they say which code they'll bill.** It's still only their word.
- **A crash can repeat a call.** I'd rather call twice than lose track of a case.

## The cut list

- **Real phones and fax.** Lots of integration, and it wouldn't have tested the decisions.
- **Parallel supplier calls.** Saves minutes. Lost orders and silent suppliers cost days.
- **Backup supplier.** I didn't want to keep a second supplier waiting without a clear rule for it.
- **Supplier history.** With one case there's nothing to learn from.
- **Continuing after a human steps in.** This is the biggest hole. Escalating ends the case, and it
  also stops work that was going fine, like the order chase when suppliers run out.
- **More delivery check-ins.** There's one, the business day before. That's late if delivery is weeks out.
- **More than one reschedule.** Once is fine. Twice and I replace them.
- **Billing detail** (deductible status, rental rules, proof of delivery). She's told about 20%, not a
  dollar amount.
- **Medical necessity.** I check the order, not the visit notes behind it.

**What I can't claim:** no real person has been called. The simulated people are far tidier than real
ones. The quote check catches made-up answers, not a real quote that was misunderstood.

## What's next

**One more day**

1. Let a case continue after a human answers, and keep the rest of it moving meanwhile. They should
   answer one question, not take over the case.
2. Check in with suppliers earlier: a day after they commit, then halfway.

**Two weeks**

1. Real transcripts, with consent, graded field by field. Until I know how often extraction is wrong,
   I wouldn't put this on a live call.
2. A backup supplier, so one going silent costs hours instead of another round of calls.
3. Real phones, supplier screening first, because those calls share no patient details beyond a ZIP.

The order is: make sure every case can finish, prove I can trust what it hears, then let it call people.
