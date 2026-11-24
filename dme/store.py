"""Append-only JSONL persistence for events. One file per case."""

from __future__ import annotations

from pathlib import Path

from pydantic import TypeAdapter

from dme import config
from dme.events import Event
from dme.view import CaseView

_adapter: TypeAdapter[Event] = TypeAdapter(Event)


class EventStore:
    def __init__(self, root: Path = config.CASES_DIR):
        self.root = Path(root)

    def path(self, case_id: str) -> Path:
        return self.root / f"{case_id}.jsonl"

    def append(self, case_id: str, *events: Event) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.path(case_id).open("a") as f:
            f.write("".join(e.model_dump_json() + "\n" for e in events))

    def events(self, case_id: str) -> list[Event]:
        path = self.path(case_id)
        if not path.exists():
            return []
        text = path.read_text()
        if text and not text.endswith("\n"):
            # A crash cut the last line short. Drop it so later appends stay readable.
            text = text[: text.rfind("\n") + 1]
            path.write_text(text)
        return [_adapter.validate_json(line) for line in text.splitlines()]

    def load(self, case_id: str) -> CaseView:
        view = CaseView()
        for e in self.events(case_id):
            view.apply(e)
        return view
