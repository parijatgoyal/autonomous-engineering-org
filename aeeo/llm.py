"""Model provider abstraction.

`AnthropicProvider` calls Claude through the official SDK with structured
JSON output. `MockProvider` (see mock_provider.py) returns scripted outputs so
the whole workflow runs offline and deterministically, including injected
faults. Both return the same `LLMResponse`, so usage/latency/cost tracking is
identical regardless of provider.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from typing import Any, Protocol


class ProviderUnavailable(Exception):
    """Transient provider failure (5xx, 429, network). Safe to retry / pause."""


class ProviderRejected(Exception):
    """Non-retryable provider failure (4xx, refusal). Needs a human."""


@dataclass
class LLMResponse:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


class LLMProvider(Protocol):
    name: str
    model: str

    def complete(self, *, agent: str, system: str, user: str, schema: dict, ctx: dict) -> LLMResponse: ...


def strict_json_schema(schema: dict) -> dict:
    """Adapt a Pydantic JSON schema to the constraints of strict structured output:
    every object closed (`additionalProperties: false`), every property required,
    and validation keywords the API does not enforce removed (Pydantic still
    enforces them on our side)."""
    schema = copy.deepcopy(schema)
    drop = {"default", "minItems", "maxItems", "minLength", "maxLength", "title"}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k in list(node):
                if k in drop and not isinstance(node.get(k), dict):
                    node.pop(k)
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    return schema


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str = "claude-opus-5-5", effort: str = "medium"):
        import anthropic

        self._anthropic = anthropic
        # The orchestrator owns retry/backoff so every attempt is recorded.
        self.client = anthropic.Anthropic(max_retries=0, timeout=300)
        self.model = model
        self.effort = effort
        self._structured_ok = True

    def complete(self, *, agent: str, system: str, user: str, schema: dict, ctx: dict) -> LLMResponse:
        a = self._anthropic
        output_config: dict[str, Any] = {"effort": self.effort}
        if self._structured_ok:
            output_config["format"] = {"type": "json_schema", "schema": strict_json_schema(schema)}
        t0 = time.perf_counter()
        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config=output_config,
                # Route refusals to a fallback model server-side instead of failing the step.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except a.BadRequestError as e:
            if self._structured_ok and "schema" in str(e).lower():
                # Schema not accepted: fall back to prompt-only JSON; our own
                # Pydantic validation + repair loop still guards the output.
                self._structured_ok = False
                return self.complete(agent=agent, system=system, user=user, schema=schema, ctx=ctx)
            raise ProviderRejected(f"bad request: {e.message}") from e
        except (a.AuthenticationError, a.PermissionDeniedError, a.NotFoundError) as e:
            raise ProviderRejected(f"{type(e).__name__}: {e.message}") from e
        except a.RateLimitError as e:
            raise ProviderUnavailable(f"rate limited: {e.message}") from e
        except a.APIStatusError as e:
            if e.status_code >= 500:
                raise ProviderUnavailable(f"server error {e.status_code}") from e
            raise ProviderRejected(f"API error {e.status_code}: {e.message}") from e
        except a.APIConnectionError as e:
            raise ProviderUnavailable(f"connection error: {e}") from e

        latency = int((time.perf_counter() - t0) * 1000)
        if resp.stop_reason == "refusal":
            raise ProviderRejected("model declined the request (refusal)")
        text = "".join(getattr(b, "text", "") for b in resp.content if b.type == "text")
        if resp.stop_reason == "max_tokens":
            text = text  # truncated JSON will fail validation and trigger a repair attempt
        return LLMResponse(
            text=text,
            model=resp.model,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            latency_ms=latency,
        )


def make_provider(name: str, *, model: str, effort: str, faults: list[str]) -> LLMProvider:
    if name == "anthropic":
        return AnthropicProvider(model=model, effort=effort)
    from .mock_provider import MockProvider

    return MockProvider(faults=faults)
