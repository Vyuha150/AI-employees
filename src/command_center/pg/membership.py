"""Server-side membership directory and shared rate limiter backed by PostgreSQL."""

from __future__ import annotations

from datetime import datetime, timedelta

from ..auth import Membership
from ..common import Clock, utc_now
from ..ratelimit import Limit, RateLimitedError
from .database import Database


class PgMembershipDirectory:
    """organization_members + member_roles + roles + permissions. The only source of roles and
    permissions for API principals; JWT claims are never consulted."""

    def __init__(self, db: Database):
        self.db = db

    def memberships(self, user_id: str) -> list[Membership]:
        with self.db.cursor() as cur:
            cur.execute(
                "SELECT o.slug, m.display_name, coalesce(array_agg(DISTINCT r.name) FILTER (WHERE r.name IS NOT NULL), '{}'), "
                "coalesce(array_agg(DISTINCT p.permission) FILTER (WHERE p.permission IS NOT NULL), '{}') "
                "FROM organization_members m JOIN organizations o ON o.id = m.organization_id "
                "LEFT JOIN member_roles mr ON mr.organization_id = m.organization_id AND mr.member_id = m.id "
                "LEFT JOIN roles r ON r.organization_id = mr.organization_id AND r.id = mr.role_id "
                "LEFT JOIN permissions p ON p.organization_id = r.organization_id AND p.role_id = r.id "
                "WHERE m.user_id = %s AND m.status = 'ACTIVE' GROUP BY o.slug, m.display_name", (user_id,))
            rows = cur.fetchall()
        return [Membership(slug, user_id, frozenset(roles), frozenset(perms), name) for slug, name, roles, perms in rows]


class PgRateLimiter:
    def __init__(self, db: Database, clock: Clock = utc_now):
        self.db, self.clock = db, clock

    def hit(self, key: str, limit: Limit) -> None:
        now = self.clock()
        start = datetime.fromtimestamp(int(now.timestamp()) // limit.window_seconds * limit.window_seconds, tz=now.tzinfo)
        with self.db.cursor() as cur:
            cur.execute("INSERT INTO rate_limit_counters (key, window_start, count) VALUES (%s, %s, 1) "
                        "ON CONFLICT (key, window_start) DO UPDATE SET count = rate_limit_counters.count + 1 "
                        "RETURNING count", (key, start))
            n = cur.fetchone()[0]
        if n > limit.max_requests:
            retry = start + timedelta(seconds=limit.window_seconds) - now
            raise RateLimitedError(f"rate limit {limit.name} exceeded ({limit.max_requests}/{limit.window_seconds}s)",
                                   max(1, int(retry.total_seconds())))

    def purge(self, older_than: timedelta = timedelta(days=1)) -> None:
        with self.db.cursor() as cur:
            cur.execute("DELETE FROM rate_limit_counters WHERE window_start < %s", (self.clock() - older_than,))


def add_member(db: Database, org_uuid: str, user_id: str, roles_permissions: dict[str, list[str]],
               display_name: str | None = None, granted_by: str = "bootstrap") -> None:
    """Privileged backend helper (provisioning/tests): create a member with roles and permissions."""
    with db.transaction() as cur:
        cur.execute("INSERT INTO organization_members (organization_id, user_id, display_name) VALUES (%s, %s, %s) "
                    "ON CONFLICT (organization_id, user_id) DO UPDATE SET display_name = EXCLUDED.display_name "
                    "RETURNING id", (org_uuid, user_id, display_name))
        member_id = cur.fetchone()[0]
        for role, perms in roles_permissions.items():
            cur.execute("INSERT INTO roles (organization_id, name) VALUES (%s, %s) ON CONFLICT (organization_id, name) "
                        "DO UPDATE SET name = EXCLUDED.name RETURNING id", (org_uuid, role))
            role_id = cur.fetchone()[0]
            for perm in perms:
                cur.execute("INSERT INTO permissions (organization_id, role_id, permission) VALUES (%s, %s, %s) "
                            "ON CONFLICT DO NOTHING", (org_uuid, role_id, perm))
            cur.execute("INSERT INTO member_roles (organization_id, member_id, role_id, granted_by) VALUES (%s,%s,%s,%s) "
                        "ON CONFLICT DO NOTHING", (org_uuid, member_id, role_id, granted_by))
