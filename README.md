# AI Business Command Center

The base operating layer for a controlled AI workforce: the system prompt every agent runs under, the structured response contract, and the checks the application runs on agent output.

## Layout

| Path | Purpose |
|---|---|
| `prompts/core_operating_principles.md` | The 30-section operating principles. `{{AGENT_PROFILE}}` is where the application injects the agent profile (§1). |
| `schemas/structured_response.schema.json` | Strict JSON Schema for STRUCTURED MODE (§25), including recommendation (§7), task (§8), change-management (§21) and proposed-action (§26) shapes. |
| `profiles/chief_of_staff.example.json` | Example agent profile with all 17 required fields. |
| `src/command_center/prompt.py` | Builds the system prompt from a profile. It rejects profiles with missing or unknown fields, so no permission is ever implied. |
| `src/command_center/validate.py` | Governance checks the schema can't express (below). |

## What the validator enforces

- **Schema (strict):** no unknown fields; `confidence` is HIGH/MEDIUM/LOW; any AMBER/RED action must have `approval_required: true`.
- **§5 Tools:** every `proposed_action.tool` must be in the agent's `ALLOWED_TOOLS`.
- **§6 / §26 Approvals:** every action with `approval_required` needs a matching `approval_request`, and the two must have the same risk class.
- **§3 / §19 Traceability:** facts, FACT findings and memory candidates must cite source IDs that exist in `sources`.
- **§17 / §28 Focus:** warnings for more than 7 brief priorities or more than 5 recommendations.

A passing response is *admissible*. It is not an approval and not an execution (§30). Approval and execution belong to the application's workflow and audit layer.

## Usage

```bash
pip install -e '.[dev]'
build-agent-prompt profiles/chief_of_staff.example.json > /tmp/chief_prompt.md
pytest
```

```python
from command_center import build_system_prompt, validate_response

system_prompt = build_system_prompt(profile)
result = validate_response(agent_json, profile["ALLOWED_TOOLS"], expected_agent_id=profile["AGENT_ID"])
if not result.ok:
    ...  # reject / ask the agent to repair; never execute
```
