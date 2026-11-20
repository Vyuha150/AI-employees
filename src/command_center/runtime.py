"""Agent runtime (Part 30).

    execute(run_id, RunContext) -> RuntimeResult

 1 receive validated run context      2 resolve agent           3 check enabled (registry fails closed)
 4 resolve prompt                     5 assemble permitted context (ContextAssembler + SourceLedger)
 6 invoke the model provider (MODEL_RETRY; configured, audited fallback only)
 7 validate the structured response   8 resolve tool calls
 9-12 permissions, risk, approvals and execution: ALWAYS via ActionGateway.submit
13 continue the model loop with ToolResult.for_model() (simulated results are labelled)
14-17 persist output, recommendations, tasks (ingest_output) and usage
18 audit events (every service writes its own)   19 finalize the run

The model can only *request* tools. It may call read tools directly (still through the
gateway); every state change must be proposed in `proposed_actions` and is governed by the
gateway at ingestion. Nothing the provider returns can skip the gateway.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Mapping

from .agents import AgentProfile, RunContext
from .audit import EventType
from .common import ActorType
from .context import AssembledContext, ContextAssembler, ContextItem, ExplicitContext
from .errors import BudgetExceededError, CommandCenterError, PermissionDeniedError
from .executors import ToolResult, ToolStatus
from .gateway import ActionStatus
from .model_providers import ModelProviderError, ModelSession, ModelTurn, ProviderRegistry, ToolDefinition
from .read_tools import READ_TOOLS
from .sources import SourceRecord, SourceType
from .state_machines import RUN_MACHINE, RunStatus
from .usage import UsageService
from .outputs import OutputKind
from .resources import load_json_schema
from .validate import STRUCTURED_RESPONSE_SCHEMA

_SCHEMAS = {OutputKind.STRUCTURED_RESPONSE: STRUCTURED_RESPONSE_SCHEMA,
            OutputKind.DAILY_BRIEF: load_json_schema("daily_brief.schema.json"),
            OutputKind.WEEKLY_REVIEW: load_json_schema("weekly_review.schema.json")}

READ_TOOL_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "read_kpis": {"type": "object", "properties": {"kpis": {"type": "array", "items": {"type": "string"}},
                                                   "limit": {"type": "integer"}}},
    "read_tasks": {"type": "object", "properties": {"owner": {"type": "string"}, "status": {"type": "string"},
                                                    "limit": {"type": "integer"}}},
    "read_knowledge": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                       "required": ["query"]},
    "search_company_data": {"type": "object", "properties": {"query": {"type": "string"},
                                                             "limit": {"type": "integer"}}, "required": ["query"]},
}
_GENERIC_READ = {"type": "object", "properties": {"filters": {"type": "object"}, "limit": {"type": "integer"}}}


@dataclass(frozen=True)
class RuntimeResult:
    run_id: str
    status: RunStatus
    output_id: str | None = None
    ingestion: Any = None
    error: str | None = None
    model_steps: int = 0
    tool_calls: int = 0
    provider: str | None = None
    model: str | None = None
    fallback_used: bool = False
    dropped_context: tuple[str, ...] = ()


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.S)


def parse_json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(_FENCE.sub("", text.strip()))
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


@dataclass
class AgentRuntime:
    cc: Any                                   # system.CommandCenter
    models: Any                               # config.ModelsConfig
    providers: ProviderRegistry
    assembler: ContextAssembler
    usage: UsageService
    max_model_steps: int = 8
    sleep: Callable[[float], None] = time.sleep

    # ---------------------------------------------------------------------------------
    def execute(self, run_id: str, context: RunContext, *, message: str | None = None,
                explicit: Iterable[ExplicitContext] = (), extra_items: Iterable[ContextItem] = (),
                customer_ids: Iterable[str] = (), project_ids: Iterable[str] = (),
                output_kind: OutputKind = OutputKind.STRUCTURED_RESPONSE,
                extra_checks: Callable[[Mapping[str, Any]], list[str]] | None = None) -> RuntimeResult:
        """extra_checks: caller-supplied validation (e.g. a workflow's data-quality floor). Its
        errors make the output INVALID; they can never make an invalid output valid."""
        cc = self.cc
        run = cc.runs.get(run_id)
        if context.current_user.principal_id != run.principal.principal_id \
                or context.organization_id != run.organization_id:
            raise PermissionDeniedError("run context does not belong to the run's principal and organization")
        try:
            agent = cc.registry.get_agent(run.agent_id)
        except CommandCenterError as exc:
            return self._fail(run_id, f"agent cannot execute: {exc}")
        if run.status is RunStatus.QUEUED:
            cc.runs.start(run_id)
        if cc.runs.get(run_id).status is not RunStatus.RUNNING:
            return RuntimeResult(run_id, cc.runs.get(run_id).status, error="run is not RUNNING")

        try:
            return self._run(run_id, agent, context, message, explicit, extra_items, customer_ids, project_ids,
                             output_kind, extra_checks)
        except BudgetExceededError as exc:
            return self._fail(run_id, f"budget_exceeded: {exc}")
        except ModelProviderError as exc:
            return self._fail(run_id, f"model_failure ({exc.provider}/{exc.model}): {exc}")
        except CommandCenterError as exc:
            return self._fail(run_id, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # never leave a run RUNNING after an unexpected error
            return self._fail(run_id, f"internal error: {type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------------------------
    def _run(self, run_id: str, agent: AgentProfile, context: RunContext, message: str | None,
             explicit, extra_items, customer_ids, project_ids, output_kind: OutputKind,
             extra_checks=None) -> RuntimeResult:
        cc = self.cc
        assembled = self.assembler.assemble(agent, context, run_id=run_id, explicit=explicit, extra_items=extra_items,
                                            customer_ids=customer_ids, project_ids=project_ids)
        prompt_ctx = replace(context, relevant_memory=assembled.lines("memory"),
                             relevant_knowledge=assembled.lines("knowledge"))
        system = cc.runs.build_system_prompt(run_id, prompt_ctx)
        profile = self.models.profile(agent.model_profile)
        read_tools = [t for t in agent.allowed_tools if t in READ_TOOLS and cc.tools.exists(t)]
        session = ModelSession(profile.provider, profile.model, profile.max_tokens, profile.effort, system,
                               tuple(ToolDefinition(t, cc.tools.get(t).description,
                                                    READ_TOOL_INPUT_SCHEMAS.get(t, _GENERIC_READ)) for t in read_tools))
        schema = _SCHEMAS[output_kind]
        user_message = self._render_request(agent, context, assembled, message, output_kind, schema)
        ledger = assembled.ledger

        steps = tool_calls = 0
        turn = self._invoke(run_id, agent, session,
                            lambda p: p.generate_structured(session, user_message, schema))
        steps += 1
        while True:
            if turn.refused:
                return self._fail(run_id, f"model refused (stop_reason={turn.stop_reason})", steps, tool_calls)
            if not turn.tool_calls:
                break
            if steps >= self.max_model_steps:
                return self._fail(run_id, f"model exceeded {self.max_model_steps} steps", steps, tool_calls)
            results = []
            for call in turn.tool_calls:
                tool_calls += 1
                results.append(self._tool_result(run_id, call, read_tools, ledger).for_model())
            pending = turn
            turn = self._invoke(run_id, agent, session,
                                lambda p, pending=pending, results=results: p.continue_with_tool_result(
                                    session, pending, results))
            steps += 1

        response = parse_json_object(turn.text)
        if response is None and steps < self.max_model_steps:
            turn = self._invoke(run_id, agent, session, lambda p: p.generate(
                session, "Your previous reply was not a single valid JSON object. Reply with only the JSON object "
                         "matching the structured response schema."))
            steps += 1
            response = None if turn.refused else parse_json_object(turn.text)
        run = cc.runs.get(run_id)
        if response is None:
            out = cc.outputs.record(run, {"unparseable_model_text": turn.text[:4000]},
                                    extra_errors=("model reply was not a JSON object",))
            return self._fail(run_id, "invalid structured response (not JSON)", steps, tool_calls, out.output_id)

        if getattr(cc, "sources_repo", None) is not None:   # persist provenance (PostgreSQL `sources`)
            for src in ledger.all():
                cc.sources_repo.add(src, run_id)
        errors = list(ledger.provenance_errors(response))
        if extra_checks is not None:
            errors += list(extra_checks(response))
        out = cc.outputs.record(run, response, extra_errors=tuple(errors), kind=output_kind)
        if out.validity.value != "VALID":
            return self._fail(run_id, "structured response failed validation: " + "; ".join(out.validation_errors[:5]),
                              steps, tool_calls, out.output_id)
        report = cc.ingest_output(out.output_id) if output_kind is OutputKind.STRUCTURED_RESPONSE else None
        run = cc.runs.get(run_id)
        if run.status is RunStatus.RUNNING:
            cc.runs.transition(run_id, RunStatus.SUCCEEDED, payload={"output_id": out.output_id})
        if run.delegation_id:
            d = cc.orchestration.repo.get(run.delegation_id)
            if d.status.value == "RUNNING":
                cc.orchestration.complete(run.delegation_id)
        final = cc.runs.get(run_id)
        return RuntimeResult(run_id, final.status, out.output_id, report, None, steps, tool_calls,
                             final.model_provider, final.model_name, final.fallback_used, tuple(assembled.dropped))

    # ---------------------------------------------------------------------------------
    def _render_request(self, agent: AgentProfile, ctx: RunContext, assembled: AssembledContext,
                        message: str | None, kind: OutputKind, schema: Mapping[str, Any]) -> str:
        other = [f"[{i.source.source_id}] ({i.kind}) {i.text}" for i in assembled.items
                 if i.kind not in ("memory", "knowledge")]
        parts = [
            f"TASK:\n{message or ctx.current_task}",
            "CONTEXT (cite by source id):\n" + ("\n".join(other) if other else "None."),
            "AVAILABLE SOURCES (the only sources you may cite; tool results add more):\n"
            + json.dumps(assembled.ledger.to_wire(), indent=1),
            f"OUTPUT: STRUCTURED MODE ({kind.value}). Reply with exactly one JSON object matching the schema "
            f"below. The agent id is {agent.agent_id!r}. Copy cited sources into `sources` exactly as listed. "
            "Read tools may be called directly; every state change must go in proposed_actions.",
            "SCHEMA:\n" + json.dumps(schema),
        ]
        if assembled.dropped:
            parts.insert(2, f"NOTE: {len(assembled.dropped)} context item(s) omitted by context limits.")
        return "\n\n".join(parts)

    def _tool_result(self, run_id: str, call, read_tools: list[str], ledger) -> ToolResult:
        if call.name not in read_tools:
            return ToolResult(False, error=f"{call.name!r} is not available as a direct tool. Propose state changes "
                                           "in proposed_actions; they are governed by approval policy.",
                              status=ToolStatus.ERROR, tool_call_id=call.call_id, tool_name=call.name)
        outcome = self.cc.gateway.submit(run_id, {"tool": call.name, "action_type": call.name,
                                                  "parameters": dict(call.input), "action_id": call.call_id,
                                                  "tool_call_id": call.call_id, "reason": "model read request"},
                                         pause_run=False)
        if outcome.status is ActionStatus.PENDING_APPROVAL:
            return ToolResult(False, error=f"requires approval {outcome.approval_id}; not executed",
                              status=ToolStatus.UNAVAILABLE, tool_call_id=call.call_id, tool_name=call.name)
        if outcome.status is ActionStatus.DENIED:
            return ToolResult(False, error=f"denied: {outcome.reason}", status=ToolStatus.ERROR,
                              tool_call_id=call.call_id, tool_name=call.name)
        result = outcome.result
        stamp = result.executed_at.isoformat() if result.executed_at else None
        for sid in result.source_ids:
            if sid not in ledger:   # ids are per record: a record already in context keeps its entry
                ledger.add(SourceRecord(sid, SourceType.TOOL_RESULT, f"{call.name} result",
                                        f"{call.name}:{result.result_ref}:{sid}", stamp))
        return replace(result, tool_call_id=call.call_id)

    def _invoke(self, run_id: str, agent: AgentProfile, session: ModelSession, fn) -> ModelTurn:
        cc, org = self.cc, self.cc.policy.organization_id
        setting = cc.policy.retries.model
        original = (session.provider, session.model)
        candidates = [original]
        first_turn = not any((m.get("role") if isinstance(m, dict) else None) == "assistant" for m in session.messages)
        if self.models.fallback.allowed and first_turn:
            # provider-native histories cannot move between providers mid-conversation
            profile = agent.model_profile
            candidates += list(self.models.fallback.chains.get(profile, ()))
        last: ModelProviderError | None = None
        for idx, (prov_name, model) in enumerate(candidates):
            provider = self.providers.get(prov_name)
            session.provider, session.model = prov_name, model
            for attempt in range(1, setting.max_attempts + 1):
                mark = len(session.messages)
                try:
                    turn = fn(provider)
                except ModelProviderError as exc:
                    del session.messages[mark:]
                    last = exc
                    cc.audit.record(EventType.MODEL_FAILED, organization_id=org, actor_type=ActorType.SYSTEM,
                                    actor_id="runtime", agent_id=agent.agent_id, run_id=run_id, object_type="agent_run",
                                    object_id=run_id, payload={"provider": prov_name, "model": model, "attempt": attempt,
                                                               "retryable": exc.retryable, "error": str(exc)[:300]})
                    if not exc.retryable or attempt == setting.max_attempts:
                        break
                    self.sleep(setting.backoff_seconds * (2 ** (attempt - 1)))
                    continue
                fallback_from = None
                if idx > 0 or turn.fallback_used:
                    fallback_from = f"{original[0]}/{original[1]}"
                    cc.audit.record(EventType.MODEL_FALLBACK_USED, organization_id=org, actor_type=ActorType.SYSTEM,
                                    actor_id="runtime", agent_id=agent.agent_id, run_id=run_id,
                                    object_type="agent_run", object_id=run_id,
                                    payload={"from": fallback_from, "to": f"{turn.provider}/{turn.model}",
                                             "server_side": turn.fallback_used and idx == 0})
                cc.runs.set_model(run_id, provider=turn.provider, model=turn.model, fallback_from=fallback_from)
                cc.audit.record(EventType.MODEL_CALLED, organization_id=org, actor_type=ActorType.SYSTEM,
                                actor_id="runtime", agent_id=agent.agent_id, run_id=run_id, object_type="agent_run",
                                object_id=run_id, payload={"provider": turn.provider, "model": turn.model,
                                                           "stop_reason": turn.stop_reason,
                                                           "tool_calls": len(turn.tool_calls)})
                self._record_usage(run_id, agent, turn, fallback_from is not None)
                return turn
        assert last is not None
        raise last

    def _record_usage(self, run_id: str, agent: AgentProfile, turn: ModelTurn, fallback: bool) -> None:
        rec = self.usage.record(run_id=run_id, agent_id=agent.agent_id, provider=turn.provider, model=turn.model,
                                usage=turn.usage, tool_calls=len(turn.tool_calls),
                                pricing=self.models.price(turn.provider, turn.model), fallback_used=fallback)
        if rec.estimated_cost is None:
            self.cc.runs.mark_cost_unknown(run_id)
        # per-run token/cost limits (raises BudgetExceededError and fails the run)
        self.cc.runs.record_usage(run_id, tokens=turn.usage.total(),
                                  cost=float(rec.estimated_cost) if rec.estimated_cost is not None else 0.0)

    def _fail(self, run_id: str, reason: str, steps: int = 0, tool_calls: int = 0,
              output_id: str | None = None) -> RuntimeResult:
        run = self.cc.runs.get(run_id)
        if not RUN_MACHINE.is_terminal(run.status):
            self.cc.runs.transition(run_id, RunStatus.FAILED, reason=reason[:500])
        if run.delegation_id:
            d = self.cc.orchestration.repo.get(run.delegation_id)
            if d.status.value in ("REQUESTED", "RUNNING"):
                (self.cc.orchestration.fail if d.status.value == "RUNNING" else self.cc.orchestration.cancel)(run.delegation_id)
        run = self.cc.runs.get(run_id)
        return RuntimeResult(run_id, run.status, output_id, None, reason, steps, tool_calls, run.model_provider,
                             run.model_name, run.fallback_used)
