"""Database diagnostics: connector view status and migration state (no secrets are reported)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..connectors import COMMON_COLUMNS, CONTRACTS, LEGACY_COLUMNS
from .database import Database, migration_files
from .read_connectors import view_columns


def connector_status(db: Database, org_uuid: str, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    out = []
    with db.cursor() as cur:
        for view, contract in CONTRACTS.items():
            row = {"view": f"connector.{view}", "tool": contract.tool, "configured": False, "state": "UNAVAILABLE",
                   "contract": "n/a", "rows": 0, "freshest_record": None, "last_successful_refresh": None,
                   "latest_error": None}
            cur.execute("SELECT last_success_at, last_error_at, last_error FROM connector_status "
                        "WHERE organization_id = %s AND view_name = %s", (org_uuid, view))
            st = cur.fetchone()
            if st:
                row["last_successful_refresh"] = st[0].isoformat() if st[0] else None
                if st[2] and (st[0] is None or (st[1] and st[1] > st[0])):
                    row["latest_error"] = st[2]
            cur.execute("SELECT to_regclass(%s)", (f"connector.{view}",))
            if cur.fetchone()[0] is None:
                out.append(row)
                continue
            row["configured"] = True
            cols = view_columns(cur, view)
            if set(COMMON_COLUMNS) <= cols:
                missing = [c for c in contract.business_columns if c not in cols]
                row["contract"] = "OK" if not missing else f"MISSING {missing}"
                ts = "source_updated_at"
            elif set(LEGACY_COLUMNS) <= cols:
                row["contract"], ts = "LEGACY", "updated_at"
            else:
                row["contract"], row["state"] = f"INVALID (missing {[c for c in COMMON_COLUMNS if c not in cols]})", "ERROR"
                out.append(row)
                continue
            cur.execute(f"SELECT count(*), max({ts}) FROM connector.{view} WHERE organization_id = %s", (org_uuid,))
            n, newest = cur.fetchone()
            row["rows"], row["freshest_record"] = n, newest.isoformat() if newest else None
            if n == 0:
                row["state"] = "UNAVAILABLE"
            elif newest is None or now - newest > contract.max_age:
                row["state"] = "STALE"
            else:
                row["state"] = "OK"
            if row["latest_error"] and row["state"] == "OK":
                row["state"] = "DEGRADED"
            out.append(row)
    return out


def migrations_state(db: Database) -> dict[str, Any]:
    expected = [n for n, _ in migration_files()]
    with db.cursor() as cur:
        cur.execute("SELECT to_regclass('schema_migrations')")
        if cur.fetchone()[0] is None:
            return {"expected": expected, "applied": [], "pending": expected, "current": False}
        cur.execute("SELECT name FROM schema_migrations")
        applied = sorted(r[0] for r in cur.fetchall())
    pending = [n for n in expected if n not in applied]
    return {"expected": expected, "applied": applied, "pending": pending, "current": not pending}


def table_exists(db: Database, name: str) -> bool:
    with db.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (name,))
        return cur.fetchone()[0] is not None


def organization_uuid(db: Database, slug: str) -> str | None:
    with db.cursor() as cur:
        cur.execute("SELECT id FROM organizations WHERE slug = %s", (slug,))
        row = cur.fetchone()
    return None if row is None else str(row[0])
