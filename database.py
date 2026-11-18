"""Database access. SQLite by default; PostgreSQL when DATABASE_URL is set.

Free hosts such as Render erase the local disk on every restart, which wipes a SQLite file
(accounts, sessions, everything). Point DATABASE_URL at a free hosted PostgreSQL database
(for example Neon or Supabase) and the data survives restarts and redeploys."""
import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
URL = os.environ.get("DATABASE_URL", "").strip()
PG = URL.startswith(("postgres://", "postgresql://"))
SQLITE_PATH = os.environ.get("DB_PATH", str(Path(__file__).parent / "campus.db"))

try:
    import psycopg
    from psycopg import errors as pgerrors
    from psycopg.rows import dict_row
except ImportError:  # only needed when DATABASE_URL is set
    psycopg = None
if PG and psycopg is None:
    raise RuntimeError("DATABASE_URL is set but psycopg is not installed. Run: pip install -r requirements.txt")

IntegrityError = (sqlite3.IntegrityError,) + ((pgerrors.IntegrityError,) if psycopg else ())
# False when the data sits on a disk that the host is known to erase (Render without a database or disk).
DURABLE = PG or not (os.environ.get("RENDER") and "DB_PATH" not in os.environ)


def ddl(sql):
    """Fill in the few type names that differ between SQLite and PostgreSQL."""
    return sql.replace("{PK}", "SERIAL PRIMARY KEY" if PG else "INTEGER PRIMARY KEY AUTOINCREMENT")


class Conn:
    """Thin wrapper so the rest of the code can use one style of SQL for both databases."""

    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, params=()):
        if PG:
            sql = sql.replace("?", "%s")  # the queries contain no literal question marks or percent signs
        return self.raw.execute(sql, params)

    def script(self, text):
        for stmt in ddl(text).split(";"):
            if stmt.strip():
                self.execute(stmt)

    def add_column(self, table, col, definition):
        if PG:
            self.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {definition}")
        else:
            try:
                self.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
            except sqlite3.OperationalError:
                pass  # column already exists

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


def connect(path=None):
    if PG:
        # prepare_threshold=None keeps this working behind connection poolers such as pgbouncer
        return Conn(psycopg.connect(URL, row_factory=dict_row, prepare_threshold=None, connect_timeout=10))
    raw = sqlite3.connect(path or SQLITE_PATH)
    raw.row_factory = sqlite3.Row
    return Conn(raw)
