"""Agent runtime: one place that enforces the contract every agent must meet.

For each call it:
  * replays the stored output if this step already completed (idempotency),
  * retries transient provider failures with backoff, then raises ProviderUnavailable
    so the orchestrator can pause the run at a checkpoint,
  * validates the output against the agent's Pydantic schema plus semantic
    checks, re-prompting with the validation error (bounded) when it is malformed,
  * records every attempt with latency, tokens and cost.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from ..config import Settings, cost_usd
from ..llm import LLMProvider, ProviderRejected, ProviderUnavailable
from ..store import Store


class AgentOutputError(Exception):
    """Agent kept returning malformed/unsupported output after bounded repairs."""


@dataclass
class Runtime:
    run_id: str
    store: Store
    provider: LLMProvider
    settings: Settings
    stage: str = ""


def extract_json(text: str) -> Any:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith(("{", "[")):
        start = text.find("{")
        if start == -1:
            raise ValueError("no JSON object found in output")
        text = text[start:]
    return json.loads(text)


class Agent:
    name: ClassVar[str]
    role: ClassVar[str]
    responsibilities: ClassVar[str]
    output_model: ClassVar[type[BaseModel]]

    def system_prompt(self) -> str:
        return (
            f"You are the {self.role} in an autonomous software delivery team.\n"
            f"Responsibilities: {self.responsibilities}\n"
            "Rules: stay strictly within your responsibilities; never invent files that the input does not "
            "mention unless your output schema asks for new files; prefer the smallest correct change; "
            "respond with a single JSON object that matches the schema and nothing else."
        )

    def instructions(self, ctx: dict) -> str:
        return "Complete your task for the input below."

    def semantic_check(self, out: BaseModel, ctx: dict) -> list[str]:
        """Return a list of problems beyond schema validity (empty = ok)."""
        return []

    def _prompt(self, ctx: dict, repair_notes: list[str]) -> str:
        visible = {k: v for k, v in ctx.items() if not k.startswith("_")}
        parts = [
            self.instructions(ctx),
            "INPUT:\n" + json.dumps(visible, indent=2, default=str),
            "OUTPUT JSON SCHEMA:\n" + json.dumps(self.output_model.model_json_schema()),
        ]
        if repair_notes:
            parts.append(
                "Your previous response was rejected:\n- " + "\n- ".join(repair_notes)
                + "\nReturn a corrected JSON object only."
            )
        return "\n\n".join(parts)

    def run(self, rt: Runtime, step_key: str, ctx: dict) -> BaseModel:
        cached = rt.store.completed_output(rt.run_id, step_key)
        if cached is not None:
            rt.store.event(rt.run_id, rt.stage, "idempotent_replay",
                           f"{self.name}: step {step_key} already completed; reusing stored output")
            return self.output_model.model_validate(cached)

        repair_notes: list[str] = []
        attempt = 0
        for _ in range(rt.settings.max_output_repairs + 1):
            attempt += 1
            user = self._prompt(ctx, repair_notes)
            resp = self._call_with_retries(rt, step_key, attempt, user, ctx)
            cost = cost_usd(rt.provider.model, resp.input_tokens, resp.output_tokens)
            try:
                out = self.output_model.model_validate(extract_json(resp.text))
                problems = self.semantic_check(out, ctx)
                if problems:
                    raise ValueError("; ".join(problems))
            except (ValueError, ValidationError) as e:
                err = _short_error(e)
                rt.store.record_execution(
                    rt.run_id, step_key, self.name, attempt, "malformed", model=resp.model,
                    input_tokens=resp.input_tokens, output_tokens=resp.output_tokens, cost=cost,
                    latency_ms=resp.latency_ms, input_obj=_trim(ctx), output_obj=resp.text[:4000], error=err,
                )
                rt.store.event(rt.run_id, rt.stage, "malformed_output",
                               f"{self.name} returned invalid output (attempt {attempt}): {err}", level="warn")
                repair_notes = [err]
                continue
            rt.store.record_execution(
                rt.run_id, step_key, self.name, attempt, "ok", model=resp.model,
                input_tokens=resp.input_tokens, output_tokens=resp.output_tokens, cost=cost,
                latency_ms=resp.latency_ms, input_obj=_trim(ctx), output_obj=out.model_dump(mode="json"),
            )
            return out
        raise AgentOutputError(f"{self.name} produced invalid output {attempt} times: {repair_notes}")

    def _call_with_retries(self, rt: Runtime, step_key: str, attempt: int, user: str, ctx: dict):
        last: Exception | None = None
        for i in range(rt.settings.provider_retries):
            t0 = time.perf_counter()
            try:
                return rt.provider.complete(agent=self.name, system=self.system_prompt(), user=user,
                                            schema=self.output_model.model_json_schema(),
                                            ctx={**ctx, "attempt": attempt})
            except ProviderUnavailable as e:
                last = e
                rt.store.record_execution(rt.run_id, step_key, self.name, attempt, "provider_error",
                                          model=rt.provider.model, error=str(e),
                                          latency_ms=int((time.perf_counter() - t0) * 1000))
                delay = rt.settings.provider_backoff_s * (2 ** i)
                rt.store.event(rt.run_id, rt.stage, "provider_retry",
                               f"{self.name}: provider unavailable ({e}); retry {i + 1}/"
                               f"{rt.settings.provider_retries} in {delay:.1f}s", level="warn")
                time.sleep(delay)
            except ProviderRejected as e:
                rt.store.record_execution(rt.run_id, step_key, self.name, attempt, "provider_error",
                                          model=rt.provider.model, error=str(e))
                raise AgentOutputError(f"{self.name}: provider rejected request: {e}") from e
        raise ProviderUnavailable(str(last))


def _short_error(e: Exception) -> str:
    if isinstance(e, ValidationError):
        return "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:5])
    return str(e)[:500]


def _trim(ctx: dict, limit: int = 20000) -> Any:
    s = json.dumps(ctx, default=str)
    return ctx if len(s) <= limit else {"truncated": s[:limit]}
