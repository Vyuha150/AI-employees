"""Single configuration-loading layer (Parts 22-23).

    load_config(config_dir=None, environment=None) -> AppConfig       (raises on any error)
    validate_config(config_dir=None, environment=None) -> ConfigReport (never raises)

Directory layout (an organization's directory, or the packaged defaults):
    agents/*.yaml
    organization_policy.yaml        (or organization_policy.example.yaml)
    models.yaml

Rules
- The environment mode is explicit: `environment=` or CC_ENVIRONMENT, one of
  development | test | production. It is never inferred (not from the hostname, not from paths).
- Production refuses to start when: any security-critical/financial value is still listed as a
  placeholder, the example policy is in use, a required section is missing, a provider
  credential is missing, or any reference is unresolved.
- Only the overrides in SUPPORTED_ENV_OVERRIDES are read from the environment. Other CC_*
  variables are reported as ignored.
- Config files must not contain secrets. Keys that look like secrets (api_key, token,
  password, secret, private_key) must be empty or end in `_env` (naming the variable to read).
- Reports never contain secret values; credentials are reported only as set / not set.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from .agents import AgentRegistry, load_raw_profiles
from .common import StrEnum
from .errors import ConfigurationError
from .policy import OrganizationPolicy, resolve_dotted, _MISSING
from .resources import Resource, as_resource, default_config_dir
from .tools import DEFAULT_TOOL_REGISTRY, ToolRegistry
from .workflow_catalog import KNOWN_WORKFLOWS


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


SUPPORTED_ENV_OVERRIDES = {
    "CC_ENVIRONMENT": "environment mode (development | test | production)",
    "CC_CONFIG_DIR": "directory containing agents/, organization_policy.yaml, models.yaml",
    "CC_DATABASE_URL": "PostgreSQL connection string (secret; never printed)",
    "CC_DEFAULT_MODEL_PROFILE": "overrides models.yaml default_profile (must name an existing profile)",
}

# Placeholder paths under these prefixes are security-critical or financial.
CRITICAL_PLACEHOLDER_PREFIXES = ("risk.", "approvals.", "budgets.", "orchestration.", "escalations.",
                                 "cost_limits.", "amber_auto_execute_tools", "privacy_gates.")

REQUIRED_POLICY_SECTIONS = ("organization_id", "name", "currency", "timezone", "risk", "approvals", "budgets",
                            "scheduler", "context_limits", "retries", "escalations", "privacy_gates",
                            "attachments", "orchestration", "external_adapters", "briefing")

_SECRET_KEY = re.compile(r"(api[_-]?key|secret|password|passwd|token|private[_-]?key|credential)", re.I)
_SECRET_VALUE = re.compile(r"(sk-ant-[A-Za-z0-9_-]{8,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|postgres(ql)?://[^\s:@]+:[^\s@]+@)")


# ---------------------------------------------------------------------------------------
# model configuration
# ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ProviderConfig:
    name: str
    api_key_env: str | None = None
    timeout_seconds: float = 600.0
    sdk_max_retries: int = 0


@dataclass(frozen=True)
class ModelProfile:
    name: str
    provider: str
    model: str
    max_tokens: int
    effort: str | None


@dataclass(frozen=True)
class ModelPricing:
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float | None
    cache_write_per_mtok: float | None
    currency: str
    as_of: str


@dataclass(frozen=True)
class FallbackPolicy:
    allowed: bool
    chains: Mapping[str, tuple[tuple[str, str], ...]]   # profile -> ((provider, model), ...)
    server_side_refusal_fallback: bool


@dataclass(frozen=True)
class ModelsConfig:
    default_profile: str
    providers: Mapping[str, ProviderConfig]
    profiles: Mapping[str, ModelProfile]
    fallback: FallbackPolicy
    pricing: Mapping[tuple[str, str], ModelPricing]
    native_json_schema: bool

    def profile(self, name: str) -> ModelProfile:
        if name not in self.profiles:
            raise ConfigurationError(f"unknown model profile {name!r}")
        return self.profiles[name]

    def price(self, provider: str, model: str) -> ModelPricing | None:
        return self.pricing.get((provider, model))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ModelsConfig":
        try:
            providers = {n: ProviderConfig(n, v.get("api_key_env"), float(v.get("timeout_seconds", 600)),
                                           int(v.get("sdk_max_retries", 0)))
                         for n, v in raw["providers"].items()}
            profiles = {}
            for n, v in raw["model_profiles"].items():
                if set(v) - {"provider", "model", "max_tokens", "effort"}:
                    raise ConfigurationError(f"model profile {n!r} has unknown keys")
                if v["provider"] not in providers:
                    raise ConfigurationError(f"model profile {n!r} uses unknown provider {v['provider']!r}")
                profiles[n] = ModelProfile(n, v["provider"], v["model"], int(v["max_tokens"]), v.get("effort"))
            fb = raw["fallback"]
            chains = {p: tuple((c["provider"], c["model"]) for c in chain) for p, chain in (fb.get("chains") or {}).items()}
            for p, chain in chains.items():
                if p not in profiles:
                    raise ConfigurationError(f"fallback chain for unknown profile {p!r}")
                for prov, _ in chain:
                    if prov not in providers:
                        raise ConfigurationError(f"fallback chain uses unknown provider {prov!r}")
            pricing = {}
            for prov, models in (raw.get("pricing") or {}).items():
                for model, pr in models.items():
                    pricing[(prov, model)] = ModelPricing(float(pr["input_per_mtok"]), float(pr["output_per_mtok"]),
                                                          pr.get("cache_read_per_mtok"), pr.get("cache_write_per_mtok"),
                                                          pr["currency"], str(pr["as_of"]))
            default = raw["default_profile"]
            if default not in profiles:
                raise ConfigurationError(f"default_profile {default!r} is not a model profile")
            return cls(default, providers, profiles,
                       FallbackPolicy(bool(fb["allowed"]), chains, bool(fb.get("server_side_refusal_fallback", False))),
                       pricing, bool((raw.get("structured_output") or {}).get("native_json_schema", False)))
        except KeyError as exc:
            raise ConfigurationError(f"models.yaml missing required key {exc}") from None


# ---------------------------------------------------------------------------------------
# app configuration
# ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class AppConfig:
    environment: Environment
    source: str
    policy: OrganizationPolicy
    registry: AgentRegistry
    tools: ToolRegistry
    models: ModelsConfig
    using_example_policy: bool
    database_url: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:  # never expose the database URL
        return (f"AppConfig(environment={self.environment.value!r}, source={self.source!r}, "
                f"organization={self.policy.organization_id!r}, database_url={'set' if self.database_url else 'unset'})")


@dataclass
class ConfigReport:
    environment: str | None
    source: str
    errors: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    placeholders: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    credentials: dict[str, str] = field(default_factory=dict)   # name -> "set" | "not set"

    @property
    def valid(self) -> bool:
        return not (self.errors or self.missing or self.unresolved)

    def render(self) -> str:
        lines = [f"configuration: {'VALID' if self.valid else 'INVALID'}",
                 f"environment:   {self.environment or '(not set)'}", f"source:        {self.source}"]
        for title, items in (("errors", self.errors), ("missing required values", self.missing),
                             ("unresolved references", self.unresolved),
                             ("placeholder values (unsafe for production)", self.placeholders),
                             ("warnings", self.warnings)):
            if items:
                lines.append(f"{title}:")
                lines += [f"  - {i}" for i in items]
        if self.credentials:
            lines.append("credentials:")
            lines += [f"  - {k}: {v}" for k, v in sorted(self.credentials.items())]
        return "\n".join(lines)


def _scan_secrets(node: Any, path: str, out: list[str]) -> None:
    if isinstance(node, Mapping):
        for k, v in node.items():
            p = f"{path}.{k}" if path else str(k)
            # secrets are strings; counts such as max_tokens are not
            if isinstance(k, str) and _SECRET_KEY.search(k) and not k.endswith("_env") and isinstance(v, str) and v:
                out.append(f"{p}: looks like a secret value in a config file (use an *_env variable name)")
            _scan_secrets(v, p, out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _scan_secrets(v, f"{path}[{i}]", out)
    elif isinstance(node, str) and _SECRET_VALUE.search(node):
        out.append(f"{path}: contains what looks like a credential")


def _resolve_environment(environment: str | Environment | None, env: Mapping[str, str]) -> Environment:
    value = environment if environment is not None else env.get("CC_ENVIRONMENT")
    if value is None or value == "":
        raise ConfigurationError("environment mode not set: pass environment= or set CC_ENVIRONMENT "
                                 "to development, test or production")
    try:
        return Environment(str(value.value if isinstance(value, Environment) else value).lower())
    except ValueError:
        raise ConfigurationError(f"invalid environment {value!r}; expected development, test or production") from None


def _read_yaml(node: Resource) -> Any:
    return yaml.safe_load(node.read_text(encoding="utf-8"))


def _collect(config_dir: "str | Path | Resource | None", environment: "str | Environment | None",
             env: Mapping[str, str]) -> tuple[ConfigReport, AppConfig | None]:
    if config_dir is None and env.get("CC_CONFIG_DIR"):
        config_dir = env["CC_CONFIG_DIR"]
    root = as_resource(config_dir) if config_dir is not None else default_config_dir()
    report = ConfigReport(environment=None, source=str(root) if config_dir is not None else "packaged defaults")

    try:
        mode = _resolve_environment(environment, env)
        report.environment = mode.value
    except ConfigurationError as exc:
        report.errors.append(str(exc))
        mode = None

    for k in sorted(env):
        if k.startswith("CC_") and k not in SUPPORTED_ENV_OVERRIDES:
            report.warnings.append(f"environment variable {k} is not a supported override and was ignored")

    if not root.is_dir():
        report.errors.append(f"config directory not found: {root}")
        return report, None

    # --- organization policy --------------------------------------------------------------
    policy_node, using_example = root.joinpath("organization_policy.yaml"), False
    if not policy_node.is_file():
        policy_node, using_example = root.joinpath("organization_policy.example.yaml"), True
    policy = raw_policy = None
    if not policy_node.is_file():
        report.missing.append("organization_policy.yaml")
    else:
        try:
            raw_policy = _read_yaml(policy_node) or {}
            _scan_secrets(raw_policy, "organization_policy", report.errors)
            for sec in REQUIRED_POLICY_SECTIONS:
                if sec not in raw_policy:
                    report.missing.append(f"organization_policy.{sec}")
            policy = OrganizationPolicy.from_mapping(raw_policy)
        except (ConfigurationError, yaml.YAMLError, TypeError, ValueError) as exc:
            report.errors.append(f"organization policy: {exc}")
    if using_example:
        msg = "using organization_policy.example.yaml (example thresholds)"
        (report.errors if mode is Environment.PRODUCTION else report.warnings).append(msg)

    # --- models ---------------------------------------------------------------------------
    models = raw_models = None
    models_node = root.joinpath("models.yaml")
    if not models_node.is_file():
        report.missing.append("models.yaml")
    else:
        try:
            raw_models = _read_yaml(models_node) or {}
            _scan_secrets(raw_models, "models", report.errors)
            override = env.get("CC_DEFAULT_MODEL_PROFILE")
            if override:
                raw_models = {**raw_models, "default_profile": override}
            models = ModelsConfig.from_mapping(raw_models)
        except (ConfigurationError, yaml.YAMLError, TypeError, ValueError) as exc:
            report.errors.append(f"models: {exc}")

    # --- agents ---------------------------------------------------------------------------
    registry = None
    tools = DEFAULT_TOOL_REGISTRY
    agents_node = root.joinpath("agents")
    raw_profiles: dict[str, Any] = {}
    if not agents_node.is_dir():
        report.missing.append("agents/")
    else:
        try:
            raw_profiles = load_raw_profiles(agents_node)
            for aid, raw in raw_profiles.items():
                _scan_secrets(raw, f"agents.{aid}", report.errors)
            registry = AgentRegistry.from_mappings(raw_profiles, tools)
            for aid, errs in sorted(registry.validation_report().items()):
                for e in errs:
                    (report.unresolved if ("not a registered agent" in e or "missing" in e) else report.errors).append(
                        f"agent {aid}: {e}")
        except ConfigurationError as exc:
            report.errors.append(f"agents: {exc}")

    # --- cross references -----------------------------------------------------------------
    agent_ids = set(raw_profiles)
    if registry is not None:
        for p in registry.list_agents(include_disabled=True):
            if models is not None and p.model_profile not in models.profiles:
                report.unresolved.append(f"agent {p.agent_id}: model_profile {p.model_profile!r} is not defined")
            for job in p.schedule_jobs:
                if job.workflow not in KNOWN_WORKFLOWS:
                    report.unresolved.append(f"agent {p.agent_id}: job {job.job_id} uses unknown workflow {job.workflow!r}")
            for ph in p.placeholders:
                report.placeholders.append(f"agents/{p.agent_id}: {ph}")
    if policy is not None:
        for rule in policy.escalations:
            target = rule.escalate_to
            if not (target.startswith("human:") or target in agent_ids):
                report.unresolved.append(f"escalation {rule.rule_id}: escalate_to {target!r} is not an agent or human:<ROLE>")
        for ph in policy.placeholders:
            report.placeholders.append(f"organization_policy: {ph}")
        for aid in sorted(policy.briefing.all_agents() - agent_ids):
            report.unresolved.append(f"briefing: participant {aid!r} is not a registered agent")
        if policy.scheduler.escalation_cron:
            from .cron import CronError, CronExpression
            try:
                CronExpression.parse(policy.scheduler.escalation_cron)
            except CronError as exc:
                report.errors.append(f"scheduler.escalation_cron: {exc}")
        enabled = [t for t, st in policy.external_adapters.items() if st.enabled]
        if enabled:
            report.warnings.append(f"external writes enabled for: {', '.join(sorted(enabled))}")
        for name, gate in policy.privacy_gates.items():
            if gate.approved and not (gate.reviewed_by and gate.reviewed_at):
                report.errors.append(f"privacy gate {name}: approved without reviewed_by and reviewed_at")
    if models is not None:
        for prof in models.profiles.values():
            if prof.provider != "mock" and models.price(prof.provider, prof.model) is None:
                report.warnings.append(f"no pricing for {prof.provider}/{prof.model}: costs will be stored as NULL")
            if prof.provider == "mock" and mode is Environment.PRODUCTION and prof.name == models.default_profile:
                report.errors.append("production default model profile uses the mock provider")
        for prov in models.providers.values():
            if prov.api_key_env:
                report.credentials[prov.api_key_env] = "set" if env.get(prov.api_key_env) else "not set"
                used = any(p.provider == prov.name for p in models.profiles.values())
                if mode is Environment.PRODUCTION and used and not env.get(prov.api_key_env):
                    report.errors.append(f"provider {prov.name}: credential {prov.api_key_env} is not set")
    report.credentials["CC_DATABASE_URL"] = "set" if env.get("CC_DATABASE_URL") else "not set"

    # --- production placeholder gate ------------------------------------------------------
    if mode is Environment.PRODUCTION:
        critical = [p for p in report.placeholders
                    if any(p.split(": ", 1)[1].startswith(pref) for pref in CRITICAL_PLACEHOLDER_PREFIXES)]
        for p in critical:
            report.errors.append(f"production refuses placeholder value: {p}")

    if not report.valid or mode is None or policy is None or models is None or registry is None:
        return report, None
    return report, AppConfig(mode, report.source, policy, registry, tools, models, using_example,
                             env.get("CC_DATABASE_URL"))


def validate_config(config_dir: "str | Path | Resource | None" = None,
                    environment: "str | Environment | None" = None,
                    env: Mapping[str, str] | None = None) -> ConfigReport:
    return _collect(config_dir, environment, os.environ if env is None else env)[0]


def load_config(config_dir: "str | Path | Resource | None" = None,
                environment: "str | Environment | None" = None,
                env: Mapping[str, str] | None = None) -> AppConfig:
    """Load and validate everything; fail closed with every problem listed."""
    report, cfg = _collect(config_dir, environment, os.environ if env is None else env)
    if cfg is None:
        raise ConfigurationError("configuration is invalid:\n" + report.render())
    return cfg


def placeholder_value(raw: Mapping[str, Any], path: str) -> Any:
    v = resolve_dotted(raw, path)
    return None if v is _MISSING else v


__all__: Iterable[str] = ("AppConfig", "ConfigReport", "Environment", "ModelsConfig", "load_config",
                          "validate_config", "SUPPORTED_ENV_OVERRIDES")
