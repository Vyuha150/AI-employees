# Architecture: AI Workforce Layer (Phase 2)

Framework-neutral Python. Every service takes a `Repository` (storage contract) and an
`AuditLogger`; in-memory and JSONL implementations ship for tests. A PostgreSQL adapter
implements the same contracts later (see the end of this document).

## Control flow

```
Human principal ──run_agent:<id>──▶ RunService.create_run ── registry.get_agent (fail closed)
                                         │
                                  system prompt = principles + profile (static) + RunContext (per run)
                                         │
                       LLM structured response
                                         │
                       OutputService.record ── validate_response (schema + governance)
                         (stored VALID or INVALID; INVALID is never ingested)
                                         │
                       CommandCenter.ingest_output
             ┌───────────────┬───────────┴──────────┬──────────────────────┐
        tasks → gateway   recommendations      proposed_actions → gateway   memory_candidates
        (create_task)     (PROPOSED)                                        (PROPOSED)
                                         │
                       ActionGateway.submit
       permissions (allowed_tools ∩ tool.allowed_roles − prohibited_actions)
       → risk.classify_action (application decides; agent declaration only raises risk)
       → risk.requires_approval
          ├─ no  → executor → tool.executed | tool.failed
          └─ yes → ApprovalEngine.request_approval (payload-hash bound, expiring)
                   run → APPROVAL_REQUIRED
                                         │
                       human approve / reject (role-checked)
                                         │
                       ActionGateway.execute_approved(exact payload)
       re-check agent + tool permission → begin_execution (hash match, single use)
       → executor → mark_executed | mark_failed → run resumes when nothing is pending
```

Every step writes an append-only, hash-chained audit event.

## Modules

| Module | Responsibility |
|---|---|
| `agents.py` | Profile model, validation, `AgentRegistry` (fail closed), `RunContext`, hierarchy |
| `tools.py` | Canonical tool registry with risk floor, reversibility, allowed roles, side-effect type |
| `policy.py` | Organization policy: monetary/recipient/sensitive-field thresholds, approver roles, TTLs, orchestration limits |
| `risk.py` | `classify_action`, `validate_risk_class`, `requires_approval` |
| `approvals.py` | Approval engine: payload binding, decisions, expiry, single-use execution claim |
| `canonical.py` | Deterministic canonical JSON and the action binding hash |
| `audit.py` | `AuditEvent`, `AuditStore` contract, in-memory + JSONL stores, chain verification |
| `state_machines.py` | Run, task, recommendation, change, approval, memory, delegation transition tables |
| `runs.py` / `tasks.py` | Run lifecycle with cost limits; §8 tasks with dependency and blocking rules |
| `recommendations.py` | Recommendation lifecycle, change requests, outcome measurement |
| `memory.py` | Memory governance (proposal → human decision → scoped context) |
| `sources.py` | Source/evidence model and catalog |
| `evaluation.py` / `performance.py` | Human output evaluation; per-agent metrics from the audit log |
| `orchestration.py` | Delegation contract and limits |
| `briefs.py` | Daily brief / weekly review validation (`schemas/daily_brief`, `schemas/weekly_review`) |
| `outputs.py` | Agent output persistence |
| `executors.py` | Internal executor, read data-provider hook, **mock** external executor |
| `gateway.py` | The single path from proposed action to execution |
| `system.py` | `CommandCenter` composition root and output ingestion |

## Governance guarantees (each one is tested)

- **No implied permissions.** A tool is usable only when it is in the profile's
  `allowed_tools`, the tool's `allowed_roles` includes the agent's role, and it is not in
  `prohibited_actions`. These are checked when the profile loads and again at every
  submission and every execution.
  - Human permissions are explicit strings (`run_agent:<id>`, `auto_execute:GREEN`,
    `auto_execute:AMBER`), with no wildcards.
  - Delegated runs re-check the human's permission to run the target agent.
- **Application-owned risk.** Final risk = max(tool floor, payload escalation, agent
  declaration).
  - If the agent declares too low a risk, a `risk.overridden_by_policy` event is written.
  - If it declares too high a risk, the safer class is kept.
  - An amount the engine can't assess fails closed to RED: missing, non-numeric, negative,
    or in a foreign currency.
  - Amounts from the same agent and tool within 24h are aggregated, so one RED action can't
    be split into smaller ones (§6).
- **RED is never autonomous.** No autonomy level or policy flag auto-executes RED, and
  policy loading rejects RED tools on the AMBER auto-execute list. RED approvals expire
  within the policy cap (24h in the example).
- **Payload integrity.** Each approval is bound to SHA-256 over organization + agent + run +
  tool + action type + canonical payload. Every payload field counts as material, so any
  change needs a new approval. `10000` and `10000.0` hash differently on purpose.
- **Approval ≠ execution.** An approval can be claimed for exactly one execution attempt,
  and the result is recorded separately. A failed execution needs a new approval.
- **Approvers.** Only a human from the same organization who holds the policy-defined role
  can approve. Callers can't choose a different approver role, and the requester can never
  approve its own request.
- **Append-only audit.** The store has no update or delete. Events are frozen, and their
  payloads are returned as copies. A hash chain detects edits, deletions and reordering, and
  the JSONL store refuses to open a tampered file.
- **Terminal states are final.** A retry is a new run linked by `retry_of`.
- **Human gates.** Only a human can:
  - approve, reject or verify a recommendation;
  - approve, close or roll back a change request;
  - accept, reject or invalidate a memory;
  - record an output evaluation or a measured outcome.
- **No invented numbers.**
  - Actual outcomes need source IDs and can only be recorded after the measurement window
    ends.
  - Performance metrics with no inputs are `None`.
  - Read tools fail ("data unavailable") when no data adapter is configured.
  - External tools are mocked and say so (`{"mock": true}`).
- **§27.** A rejected recommendation can't be proposed again unless it brings new evidence or
  new source IDs.
- **Orchestration.** Only the Chief of Staff can delegate, and only to its configured
  targets. Depth is capped at 1, so a delegated run can't delegate again. Each orchestration
  is capped on number of agents, cost, tokens (including the parent run's usage) and
  runtime.

## Design decisions to review

1. **Runtime prompt fields.** Each profile holds the 11 static prompt fields. The other six
   (organization context, memory, knowledge, task, user, user permissions) come from
   `RunContext`, which is mandatory and has no defaults. Keeping a user's permissions in
   static config would be an implied permission.
2. **Extended prompt fields.** `prompt.py` accepts an optional, strictly checked set:
   `RESPONSIBILITIES`, `ESCALATION_RULES`, `PROHIBITED_ACTIONS`, `BOUNDARIES`,
   `DELEGATION` and `OUTPUT_PREFERENCES`. Unknown fields are still rejected, and the
   required 17 are unchanged.
3. **Schema changes are additive only.**
   - New source types, plus optional `title` and `authority_level` on sources.
   - Optional `evidence_source_ids` on recommendations (validated; a warning when absent).
   - Optional `scope`, `scope_ref`, `confidence`, `review_at` and `expires_at` on memory
     candidates.
   - The legacy source types stay as aliases. Two types were added beyond the requested
     seven: `ACCEPTED_MEMORY` and `AGENT_OUTPUT`. Both can be cited, but neither counts as
     primary evidence.
4. **Memory scope default.** A candidate with no scope gets `AGENT` scope, the narrowest.
5. **GREEN actions are not always automatic.** They auto-execute only for MODERATE or
   ELEVATED agents, when the triggering human holds `auto_execute:GREEN`, and when the tool
   isn't in the agent's `always_require_approval` list. Otherwise they go to approval.
   - The CFO's `record_kpi_observation` always requires approval.
6. **High-risk tools.** `initiate_payment` may only ever be granted to the CFO role, and no
   shipped profile grants it. The other high-risk tools can't be granted to any agent.
7. **Reporting lines.** Customer Success, Product, Legal/Risk and People report to the Chief
   of Staff for coordination. SDR reports to Sales; Procurement reports to Operations.
8. **Placeholder numbers.** Thresholds, TTLs and cost limits in `config/` are placeholders
   for the organization to set.

## Known gaps after Phase 2 (status after Phase 3)

- **Scheduler:** resolved. Schedules are enforced by `scheduler.py`. Typed escalation rules are enforced by
  `escalations.py`.
- **Read adapters:** resolved. They exist for Command Center data and PostgreSQL connector views. External
  tools are still mocked.
- **People data:** gated. `read_people_summary` exists as a privacy-gated proposed tool
  (`docs/workforce_data_privacy.md`) and stays disabled until a review approves it.
- **Tools without a gateway path:** unchanged by design. `create_output`, `create_recommendation`,
  `request_approval` and `propose_change_request` are served by their dedicated services, and the gateway
  refuses them with a clear message.
- **Weekly review:** resolved. The full Part 20 contract ships as the v2 schema and the `WeeklyReview` model.
- **Packaging:** resolved. Resources are package data loaded through `importlib.resources`.

## PostgreSQL mapping (implemented in Phase 3: `pg/`, `migrations/`)

- **Records.** One table per record type. Primary key = the `*_id` field, plus a `version`
  column. `Repository.update` becomes
  `UPDATE ... SET ..., version = version + 1 WHERE id = $1 AND version = $2`, raising
  `ConcurrencyError` when zero rows match.
- **Audit log.** An `audit_events` table:
  - `sequence BIGSERIAL`, `event_id UUID UNIQUE`, the event columns, `payload JSONB`,
    `prev_hash`, `event_hash`;
  - `REVOKE UPDATE, DELETE` from the application role, plus a trigger that raises on
    UPDATE or DELETE;
  - appends serialized per organization with an advisory lock, so the chain stays linear.
- **Approvals.** `begin_execution` becomes `SELECT ... FOR UPDATE` on the approval row, then
  sets `execution_status` in the same transaction, which gives single use across processes.
- **Payload hashing.** `payload_hash` is computed in the application, never in SQL, so
  canonicalization stays in one place.

---

# Phase 3: Operational layer

## Additional modules

| Module | Responsibility |
|---|---|
| `resources.py` | Packaged prompts, schemas, default config and SQL migrations, read via `importlib.resources` |
| `config.py`, `cli.py` | Single config loader (typed, fail-closed, explicit environment mode, placeholder gate), plus `ai-command-center config validate` |
| `cron.py`, `scheduler.py` | Timezone-aware cron; jobs from agent profiles; SKIP / RUN_ONCE / CATCH_UP; FORBID concurrency; idempotent triggers |
| `idempotency.py` | Idempotency keys (`organization, kind, discriminators`) and stores |
| `model_providers.py` | `ModelProvider` contract; `MockModelProvider`; `ClaudeModelProvider` (Anthropic SDK) |
| `runtime.py` | Agent runtime loop: context, prompt, model, gateway-only tool use, validation, ingestion, usage |
| `context.py` | Per-agent context assembly with limits and a `SourceLedger` (provenance) |
| `read_tools.py`, `pg/read_connectors.py` | Read-tool contract (`OK / PARTIAL / UNAVAILABLE / STALE / ERROR`); domain and PostgreSQL connectors; people-data privacy gate |
| `knowledge.py`, `attachments.py` | Knowledge ingestion and permission-filtered retrieval; attachment gating |
| `alerts.py`, `escalations.py`, `kpis.py` | Alerts and notifications; typed escalation rules; sourced KPI snapshots |
| `usage.py` | Usage accounting from configured pricing; per-run / agent-day / agent-month / organization budgets; exhaustion policy and audited overrides |
| `retries.py` | `MODEL_RETRY`, `READ_TOOL_RETRY`, `WRITE_TOOL_RETRY` rules |
| `workflows.py`, `workflow_catalog.py` | Workflow definitions and execution state; the gateway always decides risk |
| `briefing.py` | Daily brief and weekly review generation; scheduler dispatcher |
| `briefs.py` | Daily-brief and weekly-review contracts (`WeeklyReview` typed model; v1 draft kept as legacy) |
| `realtime.py`, `commands.py`, `api.py`, `tenancy.py` | Realtime event contract; command submission; framework-neutral API; tenant registry |
| `codec.py`, `pg/` | Dataclass codec; PostgreSQL repositories, audit store, idempotency, agents, evaluations, sources, tool calls |

## Additional guarantees (each one is tested)

- **No working-directory dependence.** The wheel carries every resource. A test builds the wheel, installs
  it into a fresh virtualenv and runs the CLI from an unrelated directory.
- **Explicit environment.**
  - `CC_ENVIRONMENT` (or `environment=`) must be set; it is never inferred.
  - Production refuses to start when any of these is true:
    - a security-critical or financial value is still listed under `placeholders`;
    - the example policy is in use;
    - a provider credential is missing;
    - a reference is unresolved.
  - Config files containing secrets are rejected. Reports show credentials only as set / not set.
- **The model never bypasses the gateway.**
  - Read-tool calls from the model are `gateway.submit` calls.
  - Write tools cannot be called directly by the model; they must be proposed and are governed at ingestion.
  - Simulated tool results are labelled for the model (`mock: true` plus a notice).
- **Provenance.**
  - Every context item and tool result carries a source ID in a per-run ledger.
  - A response citing a source the runtime did not provide (or with an altered reference) is stored INVALID
    and fails the run.
- **No silent model switches.**
  - Model retries stay on the same model.
  - Fallback happens only when `models.yaml` enables it. It is audited (`model.fallback_used`) and recorded
    on the run (`fallback_used`, `fallback_from`).
  - Server-side refusal fallback is off by default and, when enabled, reported the same way.
- **Costs are never invented.** Missing pricing stores `estimated_cost = NULL` with an explanation, and the
  run is marked `cost_unknown`. Budget checks report unpriced usage instead of treating it as zero.
- **Execution ambiguity.**
  - An exception or timeout from an external or high-risk tool is `EXECUTION_UNKNOWN`, never `FAILED`.
  - The approval moves to `EXECUTION_UNKNOWN` and cannot be reused. Nothing retries it.
  - A CRITICAL alert and a human task are created.
  - Only a human with the approval's role can reconcile it, with an evidence reference.
- **Idempotency.**
  - Scheduler triggers, daily briefs, weekly reviews and escalations claim keys, so duplicate triggers do
    nothing.
  - Approved executions pass `execution_id = approval_id` to adapters.
- **Workflows cannot weaken policy.**
  - Approval and risk-override keys are rejected in definitions; the database also has a CHECK for this.
  - TOOL and KPI_UPDATE steps go through `gateway.submit`, so `initiate_payment` in a workflow still waits
    for a RED approval.
- **Multi-tenancy.**
  - One `CommandCenter` per organization, resolved from the principal.
  - Cross-organization reads return NotFound.
  - PostgreSQL uses composite `(organization_id, id)` foreign keys plus RLS.
  - Agent IDs are organization-scoped: `(organization_id, agent_key)`.
- **Database-level integrity.**
  - Audit and event tables are append-only (trigger), and the audit hash chain is per organization.
  - Approval binding fields are immutable; constraints forbid self-approval and approvals that never expire.
  - Output content is immutable.
  - KPI snapshots and memories must carry sources.

## Design decisions to review (Phase 3)

1. **Packaging.** Prompts, schemas and config moved into `src/command_center/resources/`. The Phase 1 example
   profile stays at `profiles/`.
2. **Weekly review v2.** The Phase 2 draft (`week_start`…) is kept as `weekly_review_v1.schema.json`, and
   `validate_weekly_review` dispatches on shape. New documents use the Part 20 contract.
3. **Agent profiles changed.** Each profile now has:
   - required `model_profile`;
   - daily and monthly cost limits;
   - an enforceable `schedule_policy` (`triggers`, `jobs`);
   - an optional `placeholders` list.

   Specialist daily reviews run at 06:30, data validation at 06:00, the brief at 07:30 and reconciliation at
   18:00, on weekdays in the organization's timezone.
4. **Briefing fan-out.** The daily brief runs six agents, but model-initiated delegation is capped at four
   per orchestration. Brief specialists therefore run as workflow-owned runs, bounded by the workflow's fixed
   agent list. The delegation cap stays unchanged for the Chief of Staff's own delegations.
5. **Data quality is computed by the application**, from failed runs and UNAVAILABLE / STALE tool results.
   The Chief of Staff's document may report it as more severe, never less.
6. **IDs.** `new_id()` now returns plain UUIDs (database primary keys).
7. **Structured output.** The structured-response schema has free-form `parameters` objects, which native
   JSON-schema output does not support. So `models.yaml` sets `native_json_schema: false`, and the runtime
   validates the JSON in code.
8. **Escalation rules.** Agent-profile `escalation_rules` stay as prompt guidance. The enforced rules are the
   typed `escalations` in organization policy.

## Known gaps (Phase 3)

- **Escalation engine trigger.** The engine runs when `evaluate()` is called. Wire it to a periodic job,
  e.g. a scheduler tick or a cron in the hosting platform.
- **Durable job queue.** `ThreadExecutor` is in-process. Production should use a durable queue; runs are
  already persisted as QUEUED.
- **Conversations.** Conversation messages from the command service are kept in memory. There is no table
  yet.
- **PostgreSQL list queries.** `PgRepository.list` filters in Python after an organization-scoped query.
  Add indexed query methods for high-volume tables before large deployments.
- **No real external adapters.** No adapters exist for email, purchase orders, payments and the like; they
  are mocked. Connector views (`connector.*`) must be created per organization.
- **Spec cut off.** The Part 55 text was cut off after "for cases where an external tool times out". Its
  implementation covers timeouts and exceptions in external and high-risk tools (see above).
