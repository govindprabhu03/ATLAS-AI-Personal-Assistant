"""Storage layer for ATLAS — one small abstraction over SQLite (local dev) and
Postgres (Supabase, durable cloud data).

Set DATABASE_URL to a Postgres/Supabase connection string to go durable; leave it
unset to use a local SQLite file. The rest of the app keeps using `?`-style SQL
and `conn.execute(...)` / `conn.insert(...)` — this layer translates for Postgres.
"""
import os
from pathlib import Path

DB_PATH = os.getenv("ATLAS_DB", str(Path(__file__).parent / "atlas.db"))
DATABASE_URL = os.getenv("DATABASE_URL", "")
IS_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))

# Primary-key column definition differs by dialect.
PK = "SERIAL PRIMARY KEY" if IS_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"


def is_postgres() -> bool:
    return IS_PG


class _Conn:
    """Thin wrapper giving both backends the same tiny interface."""
    def __init__(self, raw, pg):
        self.raw = raw; self.pg = pg

    def _t(self, sql):
        return sql.replace("?", "%s") if self.pg else sql

    def execute(self, sql, params=()):
        cur = self.raw.cursor()
        cur.execute(self._t(sql), params)
        return cur

    def insert(self, sql, params=()):
        """Run an INSERT and return the new row id (portable across backends)."""
        cur = self.raw.cursor()
        if self.pg:
            cur.execute(self._t(sql) + " RETURNING id", params)
            return cur.fetchone()["id"]
        cur.execute(sql, params)
        return cur.lastrowid

    def executescript(self, script):
        if self.pg:
            cur = self.raw.cursor()
            for stmt in (s.strip() for s in script.split(";") if s.strip()):
                cur.execute(stmt)
        else:
            self.raw.executescript(script)

    def commit(self): self.raw.commit()
    def close(self): self.raw.close()


def connect() -> _Conn:
    if IS_PG:
        import psycopg
        from psycopg.rows import dict_row
        return _Conn(psycopg.connect(DATABASE_URL, row_factory=dict_row), True)
    import sqlite3
    raw = sqlite3.connect(DB_PATH); raw.row_factory = sqlite3.Row
    return _Conn(raw, False)
