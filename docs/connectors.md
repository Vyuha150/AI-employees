# Connector read views

Organizations expose business data to the Command Center through **read-only views in the `connector`
schema**. The Command Center never writes these views. It never substitutes data when a view is absent:
the read tool returns `UNAVAILABLE`.

## Common columns (every view)

| Column | Type | Meaning |
|---|---|---|
| `organization_id` | uuid | Owning organization; rows are filtered on it |
| `source_system` | text | System of record (`hubspot`, `erp`, ...) |
| `source_record_id` | text | Record id in that system; becomes source id `db:<view>:<id>` |
| `source_updated_at` | timestamptz | Last change in the source; drives staleness |

Every record returned by a read tool also carries `retrieved_at`, the time the Command Center read it.

## Views

| View | Read tool | Business columns | Stale after |
|---|---|---|---|
| `connector.customers` | `read_customers` | `name, status, health_score` | 2 days |
| `connector.leads` | `read_leads` | `name, company, stage, score` | 2 days |
| `connector.opportunities` | `read_opportunities` | `name, customer_id, stage, amount, currency, last_activity_at` | 1 day |
| `connector.projects` | `read_projects` | `name, status, due_date, blocked_since` | 1 day |
| `connector.vendors` | `read_vendors` | `name, status, lead_time_days` | 7 days |
| `connector.financial_summary` | `read_financial_summary` | `period, metric, value, currency` | 2 days |

The machine-readable contracts are in `command_center.connectors.CONTRACTS`.
`contract_sql_example(view)` prints a skeleton view definition.

## Read-tool states

| State | When |
|---|---|
| `OK` | Rows returned, newest within the staleness window |
| `PARTIAL` | Result truncated at the row limit |
| `STALE` | Newest `source_updated_at` is older than the window |
| `UNAVAILABLE` | View not created, or no rows for the organization |
| `ERROR` | View exists but violates the contract (missing common columns), or the query failed |

Views with the Phase 3 shape (`record_id`, `updated_at`) are still read. Their results carry a warning to
migrate to the contract.

## Escalation signals

The scheduled escalation evaluation reads three views as signals, using only rows that have a source id:
- `opportunities`, for stalled high-value deals;
- `customers`, for critical customer-health deterioration;
- `projects`, for projects blocked too long.

## Diagnostics

```bash
CC_DATABASE_URL=... ai-command-center connectors status --org <organization-slug>
```

For each view it shows:
- configured or not;
- state (`OK`, `STALE`, `UNAVAILABLE`, `ERROR`, or `DEGRADED` when the latest read failed);
- contract conformance;
- row count;
- last successful refresh;
- latest error.

Refresh outcomes are recorded in `connector_status` on every read.
