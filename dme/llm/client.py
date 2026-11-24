"""Thin wrapper around Claude. Everything that talks to a model goes through LLM."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, Protocol, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from dme import config
from dme.events import LLMCalled

T = TypeVar("T", bound=BaseModel)
Role = Literal["agent", "sim"]
Messages = list[dict[str, str]]


class LLMError(Exception):
    """The model could not be used (API failure, refusal, truncation, bad output)."""

    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal  # retrying cannot help (bad credentials, malformed request)


class LLM(Protocol):
    on_call: Callable[[LLMCalled], None]

    def complete(
        self, *, purpose: str, system: str, messages: Messages, role: Role = "agent"
    ) -> str: ...

    def parse(self, *, purpose: str, system: str, messages: Messages, schema: type[T]) -> T: ...


class ClaudeLLM:
    MAX_TOKENS = 16000

    def __init__(self, client: anthropic.Anthropic | None = None):
        self.client = client or anthropic.Anthropic(timeout=120.0)
        self.on_call: Callable[[LLMCalled], None] = lambda e: None

    def complete(self, *, purpose, system, messages, role: Role = "agent") -> str:
        model = config.AGENT_MODEL if role == "agent" else config.SIM_MODEL
        effort = config.AGENT_EFFORT if role == "agent" else config.SIM_EFFORT
        response = self._guard(
            purpose,
            lambda: self.client.messages.create(
                model=model,
                max_tokens=self.MAX_TOKENS,
                system=system,
                messages=messages,
                output_config={"effort": effort},
            ),
        )
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            raise LLMError(f"{purpose}: empty reply")
        return text

    def parse(self, *, purpose, system, messages, schema: type[T]) -> T:
        response = self._guard(
            purpose,
            lambda: self.client.messages.parse(
                model=config.AGENT_MODEL,
                max_tokens=self.MAX_TOKENS,
                system=system,
                messages=messages,
                output_config={"effort": config.AGENT_EFFORT},
                output_format=schema,
            ),
        )
        if response.parsed_output is None:
            raise LLMError(f"{purpose}: no structured output")
        return response.parsed_output

    def _guard(self, purpose: str, call):
        try:
            response = call()
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise LLMError(f"{purpose}: credentials rejected", fatal=True) from e
        except (anthropic.BadRequestError, anthropic.NotFoundError) as e:
            raise LLMError(f"{purpose}: bad request: {e}", fatal=True) from e
        except anthropic.APIError as e:  # rate limit, 5xx, network (SDK already retried)
            raise LLMError(f"{purpose}: {e}") from e
        except ValidationError as e:
            raise LLMError(f"{purpose}: output did not match schema") from e
        except TypeError as e:
            # The SDK raises TypeError when no credential source resolves at all.
            if "authentication" not in str(e):
                raise
            raise LLMError("no Claude credentials: set ANTHROPIC_API_KEY", fatal=True) from e
        self.on_call(
            LLMCalled(
                model=response.model,
                purpose=purpose,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
        )
        if response.stop_reason in ("refusal", "max_tokens"):
            raise LLMError(f"{purpose}: stopped early ({response.stop_reason})")
        return response
