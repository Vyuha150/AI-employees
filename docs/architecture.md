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

## Known gaps (not built in this phase)

- **No scheduler.** `schedule_policy` and `escalation_rules` are configured and rendered
  into the prompt, but nothing enforces them yet.
- **No data adapters.** Read tools have no adapters, and external tools have only the mock
  executor.
- **People agent data.** There is no aggregated workforce-data read tool, so the People
  agent has no people data. Adding one needs a privacy review first.
- **Tools without a gateway path.** `create_output`, `create_recommendation`,
  `request_approval` and `propose_change_request` are served by their dedicated services.
  Submitted through the gateway, they fail with a clear message.
- **Weekly review spec.** The Part 20 brief was cut off after `decisions`, and the contract
  stops there.
- **Packaging.** Schemas and config load from repository-relative paths, so the code runs
  from a checkout. A packaged wheel would need package data.

## PostgreSQL mapping (next phase)

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
