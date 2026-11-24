from collections.abc import Callable

from dme.events import LLMCalled


class FakeLLM:
    """Scripted outputs for unit tests, keyed by purpose. Values are consumed in order;
    an Exception instance is raised instead of returned."""

    def __init__(self, script: dict[str, list]):
        self.script = {k: list(v) for k, v in script.items()}
        self.requests: list[dict] = []
        self.on_call: Callable[[LLMCalled], None] = lambda e: None

    def complete(self, *, purpose, system, messages, role="agent") -> str:
        return self._next(purpose, system, messages)

    def parse(self, *, purpose, system, messages, schema):
        return self._next(purpose, system, messages)

    def _next(self, purpose, system, messages):
        self.requests.append({"purpose": purpose, "system": system, "messages": list(messages)})
        self.on_call(LLMCalled(model="fake", purpose=purpose))
        if not self.script.get(purpose):
            raise AssertionError(f"FakeLLM has nothing scripted for {purpose!r}")
        item = self.script[purpose].pop(0)
        if isinstance(item, Exception):
            raise item
        return item
