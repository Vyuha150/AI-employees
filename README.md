# AI Business Command Center

A governed AI workforce layer. Twelve AI employees run under shared operating principles
and an application-enforced control layer:

- application-owned risk classification;
- payload-bound approvals;
- append-only, hash-chained audit;
- explicit state machines;
- human-gated recommendations, change requests and memory.

## Layout

| Path | Purpose |
|---|---|
| `src/command_center/resources/prompts/` | 30-section operating principles; `{{AGENT_PROFILE}}` is where the profile is injected (§1) |
| `src/command_center/resources/config/agents/*.yaml` | The 12 agent profiles (mission, responsibilities, KPI definitions, tools, autonomy, schedules, cost limits, model profile) |
| `src/command_center/resources/config/organization_policy.example.yaml` | Risk thresholds, approvers, TTLs, budgets, scheduler, escalations, privacy gates (placeholders marked) |
| `src/command_center/resources/config/models.yaml` | Model profiles, pricing, fallback policy (no secrets) |
| `src/command_center/resources/schemas/` | Structured response, daily brief, weekly review contracts |
| `src/command_center/resources/migrations/` | PostgreSQL schema (`0001_core.sql`) and Supabase RLS (`supabase/0101_rls.sql`) |
| `src/command_center/` | Registry, risk, approvals, audit, runtime, scheduler, workflows, API, PostgreSQL adapters |
| `profiles/chief_of_staff.example.json` | Phase 1 example profile (kept; still valid prompt-builder input) |
| `docs/architecture.md` | Control flow, guarantees, design decisions, known gaps |
| `docs/supabase_rls.md`, `docs/workforce_data_privacy.md` | RLS model; people-data privacy gate |
| `docs/connectors.md` | Connector view contracts, read-tool states, diagnostics |

## Agents

| Agent | Reports to | Owns |
|---|---|---|
| `chief_of_staff` | Founder / human management | Priorities, coordination, decision queue, briefs, synthesis; the only agent that may delegate |
| `sales_director` | Chief of Staff | Profitable closed revenue, pipeline, forecast |
| `lead_generation` | Sales Director | Qualified prospects, enrichment, lead scoring |
| `marketing_director` | Chief of Staff | Profitable demand, CAC, attribution |
| `cfo` | Chief of Staff | Liquidity, margin, forecasts, financial alerts (no payment tools) |
| `operations` | Chief of Staff | On-time, on-quality delivery, SLA, capacity |
| `procurement` | Operations | Total landed cost, supplier risk, purchase orders (approval-gated) |
| `data_analyst` | Chief of Staff | Trusted KPIs, data quality; never redefines a KPI |
| `customer_success` | Chief of Staff | Retention, renewals, health, expansion |
| `product_analyst` | Chief of Staff | Product-market fit, requirements, product economics |
| `legal_risk` | Chief of Staff | Contract and compliance risk (not final legal authority) |
| `people_performance` | Chief of Staff | Roles, capacity, hiring workflow (no employment decisions) |

## Usage

```bash
pip install -e '.[dev]'                       # or: pip install ai-command-center[postgres,claude]
ai-command-center config validate --env development
CC_ENVIRONMENT=production CC_CONFIG_DIR=/etc/command-center ai-command-center config validate
ai-command-center readiness --env production        # READY | DEGRADED | NOT_READY (exit 0 / 1 / 2)
CC_DATABASE_URL=... ai-command-center connectors status --org <slug>
pytest                                        # add CC_TEST_DATABASE_URL=<admin dsn> for PostgreSQL tests
```

Production refuses to start while any security-critical or financial value is still listed under
`placeholders`, or while the example policy is in use. Copy the packaged config to your own directory, review
every value, remove the placeholder markers, and point `CC_CONFIG_DIR` at it. Secrets (`ANTHROPIC_API_KEY`,
`CC_DATABASE_URL`) are read from the environment only.

```python
from command_center.config import load_config
from command_center.model_providers import ProviderRegistry
from command_center.system import CommandCenter
from command_center.agents import RunContext

cfg = load_config()                              # CC_ENVIRONMENT + CC_CONFIG_DIR; fails closed
cc = CommandCenter.from_config(cfg, providers=ProviderRegistry.from_config(cfg.models))
# Agent runtime: cc.runtime.execute(run_id, RunContext(...)) drives the model loop through the gateway.
# PostgreSQL: CommandCenter.build(..., repositories=PgRepositories(db, org_uuid, org_slug))
cc = CommandCenter.build()                       # in-memory, packaged defaults (development and tests)
run = cc.runs.create_run("procurement", principal, "Restock SKU A")
cc.runs.start(run.run_id)
prompt = cc.runs.build_system_prompt(run.run_id, RunContext(org_ctx, memories, knowledge, task, principal))

out = cc.outputs.record(cc.runs.get(run.run_id), llm_json)   # validated and stored
report = cc.ingest_output(out.output_id)          # tasks / recommendations / actions / memory, all governed
cc.approve(report.pending_approvals[0], approver) # human decision, role-checked
cc.execute_approved(approval_id, tool_name=..., action_type=..., payload=exact_payload)
```

The validator, the gateway and the approval engine produce *admissible*, *approved* and
*executed* as three separate facts (§30). A success is recorded only when an executor
actually returns one. External tools are mocked in this phase.
