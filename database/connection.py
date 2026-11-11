"""SQLite connection helpers (built-in sqlite3 only)."""
import sqlite3
import uuid
from contextlib import contextmanager

from flask import current_app, g


def connect(path: str) -> sqlite3.Connection:
    """Open a connection in autocommit mode; transactions are explicit via transaction()."""
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Atomic block built on SAVEPOINTs, so it can be nested safely.

    The outermost block commits on exit; any exception rolls the block back.
    """
    name = "sp_" + uuid.uuid4().hex
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield conn
    except BaseException:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    else:
        conn.execute(f"RELEASE {name}")


def get_db() -> sqlite3.Connection:
    """Per-request connection stored on flask.g."""
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()
