"""Optional backend: real Claude through the logged-in `claude` CLI, when there is no API key.

Opt in with DME_LLM_BACKEND=claude-cli. Slower than the SDK. ClaudeLLM is the default.
"""

from __future__ import annotations

import json
import subprocess
import tempfile

from anthropic import transform_schema
from pydantic import ValidationError

from dme import config
from dme.events import LLMCalled
from dme.llm.client import LLMError


class ClaudeCLILLM:
    def __init__(self):
        self.on_call = lambda e: None

    def complete(self, *, purpose, system, messages, role="agent") -> str:
        model = config.AGENT_MODEL if role == "agent" else config.SIM_MODEL
        effort = config.AGENT_EFFORT if role == "agent" else config.SIM_EFFORT
        prompt = messages[0]["content"]
        if len(messages) > 1:
            lines = "\n".join(
                f"{'YOU' if m['role'] == 'assistant' else 'THEM'}: {m['content']}" for m in messages
            )
            prompt = f"The conversation so far:\n{lines}\n\nReply with only your next turn, without a label."
        text = self._run(purpose, model, effort, system, prompt).get("result", "").strip()
        if not text:
            raise LLMError(f"{purpose}: empty reply")
        return text

    def parse(self, *, purpose, system, messages, schema):
        extra = ["--json-schema", json.dumps(transform_schema(schema))]
        out = self._run(
            purpose, config.AGENT_MODEL, config.AGENT_EFFORT, system, messages[0]["content"], extra
        )
        try:
            return schema.model_validate(out["structured_output"])
        except (KeyError, ValidationError) as e:
            raise LLMError(f"{purpose}: output did not match schema") from e

    def _run(self, purpose, model, effort, system, prompt, extra=()) -> dict:
        cmd = [
            "claude", "-p", "--model", model, "--effort", effort, "--tools", "",
            "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence",
            "--system-prompt", system, "--output-format", "json", *extra,
        ]  # fmt: skip
        try:
            # Run from an empty directory so no project files reach the model.
            with tempfile.TemporaryDirectory() as cwd:
                done = subprocess.run(
                    cmd, input=prompt, capture_output=True, text=True, timeout=180, cwd=cwd
                )
            out = json.loads(done.stdout)
        except FileNotFoundError as e:
            raise LLMError("claude CLI is not installed", fatal=True) from e
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            raise LLMError(f"{purpose}: claude CLI failed: {type(e).__name__}") from e
        if out.get("is_error"):
            raise LLMError(f"{purpose}: {out.get('result')}")
        usage = out.get("usage", {})
        self.on_call(
            LLMCalled(
                model=model,
                purpose=purpose,
                input_tokens=sum(
                    usage.get(k, 0)
                    for k in (
                        "input_tokens",
                        "cache_creation_input_tokens",
                        "cache_read_input_tokens",
                    )
                ),
                output_tokens=usage.get("output_tokens", 0),
            )
        )
        return out
