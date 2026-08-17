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


def _pg_params(url):
    """Parse a Postgres URI into psycopg kwargs WITHOUT tripping on special
    characters in the password (Supabase passwords often contain @ / : ). We
    split on the LAST '@' (host has none) and the FIRST ':' of the userinfo, so
    the raw password can be pasted un-encoded."""
    rest = url.split("://", 1)[1]
    rest = rest.split("?", 1)[0]                 # drop any query string
    userinfo, hostpart = rest.rsplit("@", 1)
    user, _, password = userinfo.partition(":")
    hostport, _, dbname = hostpart.partition("/")
    host, _, port = hostport.partition(":")
    return {"host": host, "port": port or "5432", "user": user,
            "password": password, "dbname": dbname or "postgres",
            "sslmode": "require"}

def connect() -> _Conn:
    if IS_PG:
        import psycopg
        from psycopg.rows import dict_row
        return _Conn(psycopg.connect(row_factory=dict_row, **_pg_params(DATABASE_URL)), True)
    import sqlite3
    raw = sqlite3.connect(DB_PATH); raw.row_factory = sqlite3.Row
    return _Conn(raw, False)
