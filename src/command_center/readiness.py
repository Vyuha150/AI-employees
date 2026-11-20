"""Production readiness report (Part 11 of the Phase 4 brief).

    readiness(config_dir=None, environment=None) -> ReadinessReport  (READY | DEGRADED | NOT_READY)

Any FAIL -> NOT_READY; otherwise any WARN -> DEGRADED; otherwise READY. In production, remaining
security-critical placeholders, a missing database/queue, pending migrations or a missing model
credential are FAILs. Secrets are never printed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping

from .common import StrEnum


class CheckStatus(StrEnum):
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"


class Readiness(StrEnum):
    READY = "READY"
    DEGRADED = "DEGRADED"
    NOT_READY = "NOT_READY"


@dataclass(frozen=True)
class Check:
    name: str
    status: CheckStatus
    detail: str


@dataclass
class ReadinessReport:
    environment: str | None
    checks: list[Check] = field(default_factory=list)

    @property
    def status(self) -> Readiness:
        if any(c.status is CheckStatus.FAIL for c in self.checks):
            return Readiness.NOT_READY
        if any(c.status is CheckStatus.WARN for c in self.checks):
            return Readiness.DEGRADED
        return Readiness.READY

    def add(self, name: str, status: CheckStatus, detail: str) -> None:
        self.checks.append(Check(name, status, detail))

    def render(self) -> str:
        lines = [f"readiness: {self.status.value}", f"environment: {self.environment or '(not set)'}"]
        width = max(len(c.name) for c in self.checks) if self.checks else 10
        lines += [f"  [{c.status.value:4}] {c.name.ljust(width)}  {c.detail}" for c in self.checks]
        return "\n".join(lines)


def readiness(config_dir: Any = None, environment: str | None = None, env: Mapping[str, str] | None = None,
              *, database_factory: Any = None) -> ReadinessReport:
    from .config import CRITICAL_PLACEHOLDER_PREFIXES, Environment, _collect
    from .tools import DEFAULT_TOOL_REGISTRY, PROPOSED_TOOLS, ToolCategory

    env = os.environ if env is None else env
    report_cfg, cfg = _collect(config_dir, environment, env)
    rep = ReadinessReport(report_cfg.environment)
    prod = report_cfg.environment == Environment.PRODUCTION.value
    fail_or_warn = CheckStatus.FAIL if prod else CheckStatus.WARN

    # 1-2 configuration and placeholders --------------------------------------------------
    problems = report_cfg.errors + report_cfg.missing + report_cfg.unresolved
    rep.add("configuration", CheckStatus.OK if report_cfg.valid else CheckStatus.FAIL,
            "valid" if report_cfg.valid else f"{len(problems)} problem(s): " + "; ".join(problems[:3]))
    critical = [p for p in report_cfg.placeholders
                if any(p.split(": ", 1)[1].startswith(x) for x in CRITICAL_PLACEHOLDER_PREFIXES)]
    if report_cfg.placeholders:
        rep.add("placeholders", fail_or_warn if critical else CheckStatus.WARN,
                f"{len(report_cfg.placeholders)} remaining ({len(critical)} security-critical/financial)")
    else:
        rep.add("placeholders", CheckStatus.OK, "none")

    policy = cfg.policy if cfg else None
    if policy is None:                     # best effort for the remaining checks
        try:
            from .policy import OrganizationPolicy
            from .resources import as_resource, default_config_dir
            root = as_resource(config_dir) if config_dir else default_config_dir()
            node = root.joinpath("organization_policy.yaml")
            policy = OrganizationPolicy.load(node if node.is_file() else root.joinpath("organization_policy.example.yaml"))
        except Exception:
            policy = None

    # 3-5 database, migrations, queue -------------------------------------------------------
    dsn = env.get("CC_DATABASE_URL")
    db = None
    if not dsn:
        rep.add("database", fail_or_warn, "CC_DATABASE_URL not set (in-memory storage)")
        rep.add("migrations", fail_or_warn, "no database")
        rep.add("queue", fail_or_warn, "in-memory queue only (not durable)")
    else:
        try:
            if database_factory is None:
                from .pg.database import Database as database_factory
            db = database_factory(dsn)
            rep.add("database", CheckStatus.OK, "reachable")
        except Exception as exc:
            rep.add("database", CheckStatus.FAIL, f"unreachable ({type(exc).__name__})")
        if db is not None:
            from .pg.diagnostics import migrations_state, table_exists
            m = migrations_state(db)
            rep.add("migrations", CheckStatus.OK if m["current"] else CheckStatus.FAIL,
                    "current" if m["current"] else f"pending: {', '.join(m['pending'])}")
            rep.add("queue", CheckStatus.OK if table_exists(db, "jobs") else CheckStatus.FAIL,
                    "durable PostgreSQL queue" if table_exists(db, "jobs") else "jobs table missing")
        else:
            rep.add("migrations", CheckStatus.FAIL, "database unreachable")
            rep.add("queue", CheckStatus.FAIL, "database unreachable")

    # 6 scheduler ---------------------------------------------------------------------------
    if policy is None:
        rep.add("scheduler", CheckStatus.FAIL, "policy unavailable")
    elif not policy.scheduler.enabled:
        rep.add("scheduler", CheckStatus.WARN, "disabled in policy")
    else:
        jobs = sum(len(a.schedule_jobs) for a in cfg.registry.list_agents()) if cfg else 0
        esc = policy.scheduler.escalation_cron
        rep.add("scheduler", CheckStatus.OK if esc else CheckStatus.WARN,
                f"{jobs} agent job(s); escalation evaluation " + (f"'{esc}'" if esc else "not scheduled"))

    # 7 model provider ----------------------------------------------------------------------
    if cfg is None:
        rep.add("model provider", CheckStatus.FAIL, "configuration invalid")
    else:
        prof = cfg.models.profile(cfg.models.default_profile)
        prov = cfg.models.providers[prof.provider]
        if prof.provider == "mock":
            rep.add("model provider", fail_or_warn, "default profile uses the mock provider")
        elif prov.api_key_env and not env.get(prov.api_key_env):
            rep.add("model provider", fail_or_warn, f"{prov.api_key_env} not set")
        else:
            fb = "fallback OFF" if not cfg.models.fallback.allowed else "fallback ON (audited)"
            rep.add("model provider", CheckStatus.OK, f"{prof.provider}/{prof.model}; credential set; {fb}")

    # 8 connectors ----------------------------------------------------------------------------
    if db is None or policy is None:
        rep.add("connectors", CheckStatus.WARN, "no database: connector views unavailable (reads return UNAVAILABLE)")
    else:
        from .pg.diagnostics import connector_status, organization_uuid
        org = organization_uuid(db, policy.organization_id)
        if org is None:
            rep.add("connectors", CheckStatus.WARN, f"organization {policy.organization_id!r} not in database")
        else:
            rows = connector_status(db, org)
            ok = [r["view"] for r in rows if r["state"] == "OK"]
            bad = [f"{r['view']}={r['state']}" for r in rows if r["state"] != "OK"]
            rep.add("connectors", CheckStatus.OK if not bad else CheckStatus.WARN,
                    f"{len(ok)}/{len(rows)} OK" + (f"; {', '.join(bad)}" if bad else ""))

    # 9-11 tools, adapters, people data -------------------------------------------------------
    high = sorted(t.name for t in DEFAULT_TOOL_REGISTRY.all() if t.category is ToolCategory.HIGH_RISK)
    rep.add("disabled tools", CheckStatus.OK, f"high-risk (no adapter): {', '.join(high)}; proposed (gated): "
            f"{', '.join(sorted(PROPOSED_TOOLS))}")
    if policy is not None:
        enabled = sorted(t for t, s in policy.external_adapters.items() if s.enabled)
        rep.add("external-write adapters", CheckStatus.OK if not enabled else CheckStatus.WARN,
                "all disabled (writes mocked/unavailable)" if not enabled else
                f"ENABLED: {', '.join(enabled)} (verify adapters are registered and reviewed)")
        gate = policy.privacy_gates.get("read_people_summary")
        if policy.privacy_gate_open("read_people_summary"):
            rep.add("people data", CheckStatus.OK, f"read_people_summary enabled by review ({gate.reviewed_by}, "
                    f"{gate.reviewed_at}); fields: {', '.join(gate.allowed_fields)}")
        else:
            rep.add("people data", CheckStatus.OK, "read_people_summary disabled (no recorded privacy review)")

    # 12 unresolved security issues --------------------------------------------------------
    security = [e for e in report_cfg.errors if any(k in e for k in ("secret", "credential", "privacy gate"))]
    rep.add("security issues", CheckStatus.FAIL if security else CheckStatus.OK,
            "; ".join(security[:3]) if security else "none detected")
    if db is not None:
        db.close()
    return rep
