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
| `prompts/core_operating_principles.md` | 30-section operating principles; `{{AGENT_PROFILE}}` is where the profile is injected (§1) |
| `config/agents/*.yaml` | The 12 agent profiles (mission, responsibilities, KPI definitions, tools, autonomy, escalation, scopes, limits) |
| `config/organization_policy.example.yaml` | Risk thresholds, approver roles, approval TTLs, orchestration limits (placeholders) |
| `schemas/structured_response.schema.json` | Strict STRUCTURED MODE contract (§25) |
| `schemas/daily_brief.schema.json`, `schemas/weekly_review.schema.json` | Brief and review contracts |
| `src/command_center/` | Registry, tools, risk, approvals, audit, state machines, lifecycle services, gateway |
| `profiles/chief_of_staff.example.json` | Phase 1 example profile (kept; still valid prompt-builder input) |
| `docs/architecture.md` | Control flow, guarantees, design decisions, known gaps, PostgreSQL mapping |

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
pip install -e '.[dev]'
pytest
```

```python
from command_center.system import CommandCenter
from command_center.agents import RunContext

cc = CommandCenter.build()                       # loads config/, fails closed on any invalid profile
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
