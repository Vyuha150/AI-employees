"""`ai-command-center` command-line interface. Works from any working directory."""

from __future__ import annotations

import argparse
import sys

from .config import SUPPORTED_ENV_OVERRIDES, validate_config


def _config_validate(args: argparse.Namespace) -> int:
    report = validate_config(args.config_dir, args.env)
    print(report.render())
    return 0 if report.valid else 1


def _prompt_build(args: argparse.Namespace) -> int:
    from .agents import AgentRegistry, RunContext, build_prompt_profile
    from .common import ActorType, Principal
    from .prompt import build_system_prompt
    from .resources import default_agent_dir

    registry = AgentRegistry.load_dir(args.config_dir + "/agents" if args.config_dir else default_agent_dir())
    agent = registry.get_agent(args.agent_id)
    preview_user = Principal("preview", "preview", ActorType.HUMAN)  # no permissions: preview only
    ctx = RunContext("Preview: no organization context loaded.", (), (), args.task, preview_user)
    print(build_system_prompt(build_prompt_profile(agent, ctx)))
    return 0


def _readiness(args: argparse.Namespace) -> int:
    from .readiness import Readiness, readiness
    rep = readiness(args.config_dir, args.env)
    print(rep.render())
    return {Readiness.READY: 0, Readiness.DEGRADED: 1, Readiness.NOT_READY: 2}[rep.status]


def _connectors_status(args: argparse.Namespace) -> int:
    import os
    dsn = os.environ.get("CC_DATABASE_URL")
    if not dsn:
        print("connectors: UNAVAILABLE (CC_DATABASE_URL not set; no connector views can be read)")
        return 1
    from .pg.database import Database
    from .pg.diagnostics import connector_status, organization_uuid
    db = Database(dsn)
    try:
        org = organization_uuid(db, args.org)
        if org is None:
            print(f"organization {args.org!r} not found")
            return 1
        rows = connector_status(db, org)
    finally:
        db.close()
    print(f"connectors for {args.org}:")
    print(f"  {'view':32} {'state':11} {'contract':10} {'rows':>5}  last successful refresh / latest error")
    for r in rows:
        extra = r["last_successful_refresh"] or "never"
        if r["latest_error"]:
            extra += f" / ERROR: {r['latest_error']}"
        print(f"  {r['view']:32} {r['state']:11} {str(r['contract'])[:10]:10} {r['rows']:>5}  {extra}")
    return 0 if all(r["state"] == "OK" for r in rows) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai-command-center")
    sub = parser.add_subparsers(dest="command", required=True)

    cfg = sub.add_parser("config", help="configuration commands")
    cfg_sub = cfg.add_subparsers(dest="config_command", required=True)
    v = cfg_sub.add_parser("validate", help="validate configuration (never prints secrets)",
                           epilog="Supported environment overrides: " + ", ".join(SUPPORTED_ENV_OVERRIDES))
    v.add_argument("--config-dir", default=None, help="organization config directory (default: CC_CONFIG_DIR or packaged defaults)")
    v.add_argument("--env", default=None, help="development | test | production (default: CC_ENVIRONMENT)")
    v.set_defaults(func=_config_validate)

    rd = sub.add_parser("readiness", help="production readiness: READY | DEGRADED | NOT_READY")
    rd.add_argument("--config-dir", default=None)
    rd.add_argument("--env", default=None)
    rd.set_defaults(func=_readiness)

    con = sub.add_parser("connectors", help="connector view diagnostics")
    con_sub = con.add_subparsers(dest="connectors_command", required=True)
    cs = con_sub.add_parser("status", help="configured / unavailable / stale / last refresh / latest error")
    cs.add_argument("--org", required=True, help="organization slug")
    cs.set_defaults(func=_connectors_status)

    pr = sub.add_parser("prompt", help="render an agent system prompt preview")
    pr.add_argument("agent_id")
    pr.add_argument("--task", default="Preview task")
    pr.add_argument("--config-dir", default=None)
    pr.set_defaults(func=_prompt_build)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # report, never dump state
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
