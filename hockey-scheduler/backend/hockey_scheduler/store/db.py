"""Database connection + tiny dialect shim for the SQL store.

PostgreSQL is the product target; SQLite (stdlib) is the local/dev/test adapter
so the SQL persistence layer can be exercised without a Postgres server. Both
drivers speak DB-API 2.0, so the store is written once against ``?`` placeholders
and the dialect translates for Postgres.
"""

import sqlite3
from typing import Optional, Tuple


class Dialect:
    def __init__(self, paramstyle: str, backend: str):
        self.paramstyle = paramstyle
        # "sqlite" or "postgres" — lets the migration loader pick a per-dialect
        # migration file when the DDL can't be written portably (e.g. adding a
        # foreign key needs an ALTER on Postgres but a table rebuild on SQLite).
        self.backend = backend

    def sql(self, query: str) -> str:
        """Author SQL with ``?``; translate to ``%s`` for Postgres."""
        if self.paramstyle == "qmark":
            return query
        return query.replace("?", "%s")


def connect(url: str) -> Tuple[object, Dialect, Optional[str]]:
    """Open a connection for the given URL and return (conn, dialect, path).

    - ``postgres://…`` / ``postgresql://…`` → psycopg (autocommit); ``path``
      is always ``None`` (readiness (#143) only cares whether SQLite landed
      on ``:memory:``, never a concern for Postgres).
    - ``sqlite:///path`` / ``sqlite://`` / a raw path / ``:memory:`` →
      sqlite3; ``path`` is the resolved filesystem path (or ``":memory:"``),
      so a caller can tell a genuinely durable file apart from a SQLite
      in-memory connection that's just as ephemeral as ``InMemoryStore``.
    """
    if url.startswith(("postgres://", "postgresql://")):
        import psycopg  # imported lazily; only needed for the Postgres target
        from psycopg.rows import dict_row

        conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
        return conn, Dialect("pyformat", "postgres"), None

    if url.startswith("sqlite:///"):
        path = url[len("sqlite:///"):]
    elif url.startswith("sqlite://"):
        path = url[len("sqlite://"):] or ":memory:"
    else:
        path = url  # raw filesystem path or ":memory:"

    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # autocommit; transaction() manages explicit txns
    # Enforce declared foreign keys (off by default in SQLite). PostgreSQL always
    # enforces them, so this gives both backends the same observable behavior
    # (#201). It is a per-connection setting and cannot be changed inside a
    # transaction, so it is set once here at open time.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn, Dialect("qmark", "sqlite"), path
