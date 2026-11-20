"""Connection handling and migrations."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

import psycopg

from ..resources import resource


class Database:
    """One autocommit connection guarded by a lock. Operations needing atomicity use
    `transaction()`. A production deployment can swap in a pool with the same interface."""

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._conn = psycopg.connect(dsn, autocommit=True)
        self._lock = threading.RLock()

    def __repr__(self) -> str:
        return "Database(<dsn hidden>)"

    @contextmanager
    def cursor(self) -> Iterator[psycopg.Cursor]:
        with self._lock, self._conn.cursor() as cur:
            yield cur

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Cursor]:
        with self._lock, self._conn.transaction(), self._conn.cursor() as cur:
            yield cur

    def close(self) -> None:
        self._conn.close()


def migration_files() -> list[tuple[str, str]]:
    root = resource("migrations")
    files = sorted((n for n in root.iterdir() if n.name.endswith(".sql") and n.name[:4].isdigit()),
                   key=lambda n: n.name)
    return [(n.name, n.read_text(encoding="utf-8")) for n in files]


def apply_migrations(db: Database) -> list[str]:
    """Apply numbered core migrations once each (tracked in schema_migrations).
    Supabase-specific RLS lives in supabase/ and is applied separately (see docs/supabase_rls.md)."""
    applied = []
    with db.transaction() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('cc_migrations'))")
        cur.execute("SELECT name FROM schema_migrations")
        done = {r[0] for r in cur.fetchall()}
        for name, sql in migration_files():
            if name in done:
                continue
            cur.execute(sql)
            cur.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
            applied.append(name)
    return applied


def ensure_organization(db: Database, slug: str, name: str, timezone: str, currency: str) -> str:
    """Return the organization's UUID, creating the row if needed."""
    with db.transaction() as cur:
        cur.execute("INSERT INTO organizations (slug, name, timezone, currency) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (slug) DO NOTHING", (slug, name, timezone, currency))
        cur.execute("SELECT id FROM organizations WHERE slug = %s", (slug,))
        return str(cur.fetchone()[0])
