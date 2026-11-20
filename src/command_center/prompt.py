"""Render the core operating principles with an injected agent profile (principles §1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .resources import resource

TEMPLATE_PATH = resource("prompts", "core_operating_principles.md")  # packaged resource
PLACEHOLDER = "{{AGENT_PROFILE}}"

REQUIRED_PROFILE_FIELDS: tuple[str, ...] = (
    "AGENT_ID",
    "AGENT_NAME",
    "AGENT_ROLE",
    "DEPARTMENT",
    "MANAGER",
    "MISSION",
    "OBJECTIVES",
    "KPIS",
    "ALLOWED_TOOLS",
    "AUTONOMY_LEVEL",
    "RISK_POLICY",
    "ORGANIZATION_CONTEXT",
    "RELEVANT_MEMORY",
    "RELEVANT_KNOWLEDGE",
    "CURRENT_TASK",
    "CURRENT_USER",
    "CURRENT_USER_PERMISSIONS",
)

# Descriptive fields an agent profile may add. They never grant anything; permissions come
# only from ALLOWED_TOOLS and CURRENT_USER_PERMISSIONS. Unknown fields are still rejected.
OPTIONAL_PROFILE_FIELDS: tuple[str, ...] = (
    "RESPONSIBILITIES",
    "ESCALATION_RULES",
    "PROHIBITED_ACTIONS",
    "BOUNDARIES",
    "DELEGATION",
    "OUTPUT_PREFERENCES",
)


def _render_value(value: Any) -> str:
    if isinstance(value, str):
        return value if value.strip() else "Not provided."
    if isinstance(value, (list, tuple)):
        if not value:
            return "None."
        return "\n" + "\n".join(f"  - {_render_value(v).strip()}" for v in value)
    if isinstance(value, Mapping):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


def render_profile(profile: Mapping[str, Any]) -> str:
    """Render the profile block. Missing fields are a hard error, never silently defaulted,
    because the agent must not assume permissions that were not provided (§1, §5)."""
    missing = [f for f in REQUIRED_PROFILE_FIELDS if f not in profile]
    if missing:
        raise ValueError(f"Agent profile is missing required fields: {', '.join(missing)}")
    unknown = sorted(set(profile) - set(REQUIRED_PROFILE_FIELDS) - set(OPTIONAL_PROFILE_FIELDS))
    if unknown:
        raise ValueError(f"Agent profile has unknown fields: {', '.join(unknown)}")

    # Fence the profile so that text inside memory/knowledge/task fields is clearly data (§20).
    lines = ["<agent_profile>"]
    lines += [f"{field}: {_render_value(profile[field])}" for field in REQUIRED_PROFILE_FIELDS]
    lines += [f"{field}: {_render_value(profile[field])}" for field in OPTIONAL_PROFILE_FIELDS if field in profile]
    lines.append("</agent_profile>")
    return "\n".join(lines)


def build_system_prompt(profile: Mapping[str, Any], template_path: Any = TEMPLATE_PATH) -> str:
    template = template_path.read_text(encoding="utf-8")
    if template.count(PLACEHOLDER) != 1:
        raise ValueError(f"Template must contain exactly one {PLACEHOLDER} placeholder")
    return template.replace(PLACEHOLDER, render_profile(profile))


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Render a Command Center system prompt for an agent profile.")
    parser.add_argument("profile", type=Path, help="Path to an agent profile JSON file")
    args = parser.parse_args(argv)
    print(build_system_prompt(json.loads(args.profile.read_text(encoding="utf-8"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
