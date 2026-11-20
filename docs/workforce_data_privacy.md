# Workforce data privacy gate

The People & Performance agent needs some workforce data to do capacity and goal analysis. Workforce data is
sensitive, so access sits behind a **privacy review gate** and is **disabled by default**. Until the gate is
approved, the People agent reports workforce data as unavailable.

## Current state (default)

- `read_people_summary` is a **proposed** tool (`command_center.tools.PROPOSED_TOOLS`). It is not in the active
  tool registry, so:
  - no agent profile can list it (profile validation fails);
  - a request for it is denied with "privacy-gated and not enabled; data unavailable".
- `organization_policy.yaml` → `privacy_gates.read_people_summary.approved: false`.
- The weekly review only includes the People agent when the gate is open **and** the requesting human holds
  `view:people_data`. Otherwise the `people` domain must be reported as `UNAVAILABLE`, and the review cannot
  be authoritative.

## What the tool may ever expose

Only business-relevant fields, and only those the review approves:

`employee_id, role, department, workload, assigned_tasks, goal_completion, skills, availability`

## What it must never expose (enforced in code)

`PeopleSummaryConnector` strips these categories even if a review lists them
(`read_tools.PEOPLE_FORBIDDEN_FIELDS`):
- health or medical data, disability;
- religion, political beliefs, union membership;
- race or ethnicity, nationality, immigration status;
- sexual orientation, gender identity, marital status, pregnancy, family information;
- private communications or messages;
- biometrics or genetic data, criminal records;
- home address, date of birth or age;
- salary, compensation, bank accounts, national IDs.

The agent profile also says: "Do not infer protected or sensitive personal attributes." The agent evaluates
work outcomes, not people's characteristics.

## Enabling the tool (all steps required)

1. **Privacy review.** A data protection owner documents:
   - purpose (capacity planning, goal tracking);
   - legal basis;
   - the exact approved fields (a subset of the business list above);
   - retention;
   - who can view results;
   - the data source.
2. **Record the approval** in `organization_policy.yaml`:
   ```yaml
   privacy_gates:
     read_people_summary:
       approved: true
       reviewed_by: dpo@example.com
       reviewed_at: "2026-12-01"
       allowed_fields: [employee_id, role, department, workload, goal_completion]
   ```
   `config validate` rejects `approved: true` without `reviewed_by` and `reviewed_at`.
3. **Grant the tool** by adding `read_people_summary` to `people_performance.yaml` → `allowed_tools`. The
   registry adds the proposed tool only when the gate is open (`ToolRegistry.with_proposed`).
4. **Configure the data source.** Provide a connector returning rows from an HR system view that already
   excludes sensitive columns. The connector filters to the approved fields again.
5. **Grant viewing.** Give `view:people_data` only to the humans who may see workforce summaries in weekly
   reviews.

Removing any step closes the gate again. Every read is audited (`tool.executed` with the fields returned).

## Review checklist

- [ ] Purpose limited to workforce capacity, roles, goals and skills.
- [ ] Approved fields are the minimum needed. No free-text fields that could carry sensitive data.
- [ ] Source view excludes sensitive columns at the database level.
- [ ] Retention and access for outputs that contain workforce data are defined.
- [ ] Employees informed per local law. A DPIA was completed where required.
