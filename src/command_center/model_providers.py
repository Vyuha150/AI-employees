"""Model-provider abstraction (Part 29) with a scripted mock and a Claude implementation.

The provider only talks to the model. It never executes tools: tool calls it returns are
handed by the runtime to the ActionGateway, and results come back through
`continue_with_tool_result`. Model names come from configuration (ModelProfile).

The Claude provider:
- reads the API key from the environment variable named in models.yaml (never logged,
  never in repr);
- runs adaptive thinking (default on current models) with `output_config.effort` from the profile;
- appends the full assistant content (including thinking blocks) to the conversation, as
  the API requires;
- does not use native JSON-schema output for the structured-response schema (free-form
  `parameters` objects are not supported there); the runtime validates the JSON in code;
- does not switch models: server-side refusal fallback is used only when models.yaml enables
  it, and any model switch is reported on the ModelTurn so the runtime can audit it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Protocol, Sequence

from .errors import CommandCenterError, ConfigurationError
from .usage import TokenUsage


class ModelProviderError(CommandCenterError):
    def __init__(self, message: str, *, retryable: bool, provider: str, model: str):
        super().__init__(message)
        self.retryable = retryable
        self.provider = provider
        self.model = model


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True)
class ToolCallRequest:
    call_id: str
    name: str
    input: Mapping[str, Any]


@dataclass(frozen=True)
class ModelTurn:
    text: str
    tool_calls: tuple[ToolCallRequest, ...]
    stop_reason: str
    usage: TokenUsage
    provider: str
    model: str                       # model actually serving this turn
    refused: bool = False
    fallback_used: bool = False


@dataclass
class ModelSession:
    """Provider-neutral conversation state; `messages` holds provider-native entries."""

    provider: str
    model: str
    max_tokens: int
    effort: str | None
    system: str
    tools: tuple[ToolDefinition, ...] = ()
    messages: list[Any] = field(default_factory=list)
    response_schema: Mapping[str, Any] | None = None


class ModelProvider(Protocol):
    name: str

    def generate(self, session: ModelSession, user_message: str) -> ModelTurn: ...
    def generate_structured(self, session: ModelSession, user_message: str, schema: Mapping[str, Any]) -> ModelTurn: ...
    def continue_with_tool_result(self, session: ModelSession, turn: ModelTurn,
                                  results: Sequence[Mapping[str, Any]]) -> ModelTurn: ...


# ---------------------------------------------------------------------------------------
# mock
# ---------------------------------------------------------------------------------------
Script = Callable[[ModelSession, str], ModelTurn]


@dataclass
class MockModelProvider:
    """Deterministic provider for tests and dry runs. `script` is a list of turns or callables
    (session, last_input) -> ModelTurn. Raises when the script is exhausted (never invents output)."""

    script: list[Any] = field(default_factory=list)
    name: str = "mock"
    responder: Any = None   # callable(session, last_input) used when the script is empty
    calls: list[dict[str, Any]] = field(default_factory=list)
    fail_with: list[ModelProviderError] = field(default_factory=list)

    def _next(self, session: ModelSession, last: str) -> ModelTurn:
        self.calls.append({"model": session.model, "input": last, "system_len": len(session.system),
                           "tools": [t.name for t in session.tools]})
        if self.fail_with:
            raise self.fail_with.pop(0)
        if not self.script and self.responder is None:
            raise ModelProviderError("mock script exhausted", retryable=False, provider=self.name, model=session.model)
        item = self.script.pop(0) if self.script else self.responder
        turn = item(session, last) if callable(item) else item
        turn = replace(turn, provider=self.name, model=session.model)   # report what actually served it
        session.messages.append({"role": "assistant", "text": turn.text,
                                 "tool_calls": [c.call_id for c in turn.tool_calls]})
        return turn

    def generate(self, session: ModelSession, user_message: str) -> ModelTurn:
        session.messages.append({"role": "user", "text": user_message})
        return self._next(session, user_message)

    def generate_structured(self, session: ModelSession, user_message: str, schema: Mapping[str, Any]) -> ModelTurn:
        session.response_schema = schema
        return self.generate(session, user_message)

    def continue_with_tool_result(self, session: ModelSession, turn: ModelTurn,
                                  results: Sequence[Mapping[str, Any]]) -> ModelTurn:
        payload = json.dumps(list(results), default=str)
        session.messages.append({"role": "tool_results", "results": list(results)})
        return self._next(session, payload)


def mock_turn(text: str = "", tool_calls: Sequence[ToolCallRequest] = (), *, model: str = "mock-model",
              input_tokens: int = 100, output_tokens: int = 50, refused: bool = False) -> ModelTurn:
    return ModelTurn(text, tuple(tool_calls), "refusal" if refused else ("tool_use" if tool_calls else "end_turn"),
                     TokenUsage(input_tokens, output_tokens, 0, 0), "mock", model, refused)


# ---------------------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------------------
class ClaudeModelProvider:
    name = "anthropic"

    def __init__(self, *, api_key_env: str = "ANTHROPIC_API_KEY", timeout_seconds: float = 600.0,
                 sdk_max_retries: int = 0, server_side_refusal_fallback: bool = False,
                 native_json_schema: bool = False, client: Any = None, env: Mapping[str, str] | None = None):
        self._fallback = server_side_refusal_fallback
        self._native_json = native_json_schema
        if client is not None:          # injected (tests); no credential handling needed
            self._client = client
            return
        env = os.environ if env is None else env
        key = env.get(api_key_env)
        if not key:
            raise ConfigurationError(f"Claude provider: credential {api_key_env} is not set")
        import anthropic
        self._client = anthropic.Anthropic(api_key=key, timeout=timeout_seconds, max_retries=sdk_max_retries)

    def __repr__(self) -> str:
        return f"ClaudeModelProvider(fallback={self._fallback})"   # never includes the key

    # -- request -----------------------------------------------------------------------
    def _call(self, session: ModelSession) -> ModelTurn:
        import anthropic

        kwargs: dict[str, Any] = {"model": session.model, "max_tokens": session.max_tokens,
                                  "system": session.system, "messages": session.messages}
        if session.effort:
            kwargs["output_config"] = {"effort": session.effort}
        if session.tools:
            kwargs["tools"] = [{"name": t.name, "description": t.description, "input_schema": dict(t.input_schema)}
                               for t in session.tools]
        if session.response_schema is not None and self._native_json:
            kwargs.setdefault("output_config", {})["format"] = {"type": "json_schema",
                                                                "schema": dict(session.response_schema)}
        try:
            if self._fallback:
                response = self._client.beta.messages.create(betas=["server-side-fallback-2026-07-01"],
                                                             fallbacks="default", **kwargs)
            else:
                response = self._client.messages.create(**kwargs)
        except anthropic.RateLimitError as exc:
            raise ModelProviderError(f"rate limited: {exc.message}", retryable=True, provider=self.name,
                                     model=session.model) from None
        except anthropic.APIStatusError as exc:
            raise ModelProviderError(f"API error {exc.status_code}: {exc.message}",
                                     retryable=exc.status_code >= 500, provider=self.name, model=session.model) from None
        except anthropic.APIConnectionError as exc:  # includes timeouts
            raise ModelProviderError(f"connection error: {type(exc).__name__}", retryable=True, provider=self.name,
                                     model=session.model) from None

        # Keep the full content (thinking blocks included) for the next request.
        session.messages.append({"role": "assistant", "content": response.content})
        text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
        calls = tuple(ToolCallRequest(b.id, b.name, dict(b.input))
                      for b in response.content if getattr(b, "type", None) == "tool_use")
        u = response.usage
        usage = TokenUsage(u.input_tokens, u.output_tokens, getattr(u, "cache_read_input_tokens", None),
                           getattr(u, "cache_creation_input_tokens", None))
        served = getattr(response, "model", session.model) or session.model
        iterations = getattr(u, "iterations", None) or []
        switched = served != session.model or any(getattr(i, "type", None) == "fallback_message" for i in iterations)
        return ModelTurn(text, calls, response.stop_reason or "", usage, self.name, served,
                         refused=response.stop_reason == "refusal", fallback_used=switched)

    def generate(self, session: ModelSession, user_message: str) -> ModelTurn:
        session.messages.append({"role": "user", "content": user_message})
        return self._call(session)

    def generate_structured(self, session: ModelSession, user_message: str, schema: Mapping[str, Any]) -> ModelTurn:
        session.response_schema = schema
        return self.generate(session, user_message)

    def continue_with_tool_result(self, session: ModelSession, turn: ModelTurn,
                                  results: Sequence[Mapping[str, Any]]) -> ModelTurn:
        # All results for one assistant turn go back in a single user message.
        blocks = [{"type": "tool_result", "tool_use_id": r["tool_call_id"],
                   "content": json.dumps(r, default=str), "is_error": r.get("status") not in ("OK", "PARTIAL", "STALE")}
                  for r in results]
        session.messages.append({"role": "user", "content": blocks})
        return self._call(session)


@dataclass
class ProviderRegistry:
    """Provider instances by name. A provider is created only when configured."""

    providers: dict[str, ModelProvider] = field(default_factory=dict)

    def get(self, name: str) -> ModelProvider:
        if name not in self.providers:
            raise ConfigurationError(f"model provider {name!r} is not configured")
        return self.providers[name]

    @classmethod
    def from_config(cls, models: Any, *, env: Mapping[str, str] | None = None,
                    mock: MockModelProvider | None = None) -> "ProviderRegistry":
        reg = cls()
        for name, prov in models.providers.items():
            if name == "mock":
                if mock is not None:
                    reg.providers["mock"] = mock
                continue
            if name == "anthropic":
                used = any(p.provider == "anthropic" for p in models.profiles.values())
                if used:
                    reg.providers["anthropic"] = ClaudeModelProvider(
                        api_key_env=prov.api_key_env or "ANTHROPIC_API_KEY", timeout_seconds=prov.timeout_seconds,
                        sdk_max_retries=prov.sdk_max_retries,
                        server_side_refusal_fallback=models.fallback.server_side_refusal_fallback,
                        native_json_schema=models.native_json_schema, env=env)
        return reg
