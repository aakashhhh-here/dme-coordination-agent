"""Run one "phone call": agent LLM <-> counterparty. Returns a transcript, decides nothing."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel

from dme import config
from dme.llm.client import LLM
from dme.models import Turn

END = "[END_CALL]"
HANG_UP = "[HANG_UP]"


class CallBrief(BaseModel):
    """What the agent is told before dialing. Only facts the other side needs (minimum necessary)."""

    call_type: str
    party_id: str
    party_name: str
    goal: str
    facts: list[str]
    questions: list[str]
    now: datetime


class Counterparty(Protocol):
    def answers(self) -> bool: ...

    def reply(self, turns: list[Turn]) -> str: ...


def _system(brief: CallBrief) -> str:
    facts = "\n".join(f"- {f}" for f in brief.facts)
    questions = "\n".join(f"{i}. {q}" for i, q in enumerate(brief.questions, 1))
    return f"""You are an automated care-coordination assistant on a phone call. You help a patient get \
medical equipment that Medicare covers. You are calling {brief.party_name}.
Current date and time: {brief.now:%A, %B %d, %Y, %I:%M %p}.

GOAL: {brief.goal}

FACTS YOU MAY USE (do not state anything about the patient, Medicare or costs beyond these):
{facts}

YOU NEED A CLEAR ANSWER TO EACH OF THESE:
{questions}

How to talk:
- Say early on that you are an automated assistant calling on the patient's behalf.
- Speak as on a phone: one to three short sentences per turn, no lists, no markdown.
- Ask only the questions above. A clear answer is final: don't test it or ask for extras.
- Ask one or two questions at a time, and wait for the answers. If an answer is vague, ask once \
more. If you need a calendar date and they give a relative one, read the date back.
- Use people's names exactly as given above: keep a title like Dr., add none, assume no pronouns.
- Do not promise, agree to, or decide anything on the patient's behalf.
- Give no medical advice. If someone asks for something not in your facts, say you can't share it.
- When you have the answers, or it is clear you won't get them, say goodbye and end that turn \
with {END}."""


def _identity_system(brief: CallBrief) -> str:
    """All the agent is told until we know who answered: it can't reveal what it doesn't have."""
    return f"""You are an automated assistant making a phone call to {brief.party_name}. You must \
verify who answered before saying what it is about.
- Say you are an automated assistant. Ask whether you are speaking with {brief.party_name}, then \
ask for their date of birth, to protect their privacy.
- If they ask why you are calling, say you can only say once you have verified who they are.
- A date of birth only counts from {brief.party_name} in person.
- If they gave one and you are reading this, it did not match: ask once more, without saying why.
- If {brief.party_name} is not there, won't say, or it fails twice: say you will call another \
time, and end that turn with {END}.
- Use their name exactly as given, with no title.
- One or two short spoken sentences per turn, no markdown. Agree to and promise nothing else."""


def run_call(
    llm: LLM,
    brief: CallBrief,
    counterparty: Counterparty,
    max_turns: int = config.MAX_CALL_TURNS,
    verified: Callable[[list[Turn]], bool] = lambda turns: True,
) -> list[Turn]:
    """Empty transcript = nobody picked up (no LLM call is made).

    The brief is withheld from the agent until `verified` says who answered.
    """
    if not counterparty.answers():
        return []
    unlocked = False
    turns: list[Turn] = []
    for _ in range(max_turns):
        said = counterparty.reply(turns)
        hung_up = HANG_UP in said
        turns.append(Turn(speaker="counterparty", text=said.replace(HANG_UP, "").strip()))
        if hung_up:
            break
        unlocked = unlocked or verified(turns)
        system = _system(brief) if unlocked else _identity_system(brief)
        messages = [
            {
                "role": "user" if t.speaker == "counterparty" else "assistant",
                "content": t.text or "...",
            }
            for t in turns
        ]
        said = llm.complete(purpose=f"agent:{brief.call_type}", system=system, messages=messages)
        ended = END in said
        turns.append(Turn(speaker="agent", text=said.replace(END, "").strip()))
        if ended:
            break
    return turns
