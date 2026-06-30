"""Database connection + tiny dialect shim for the SQL store.

PostgreSQL is the product target; SQLite (stdlib) is the local/dev/test adapter
so the SQL persistence layer can be exercised without a Postgres server. Both
drivers speak DB-API 2.0, so the store is written once against ``?`` placeholders
and the dialect translates for Postgres.
"""

import sqlite3
from typing import Tuple


class Dialect:
    def __init__(self, paramstyle: str):
        self.paramstyle = paramstyle

    def sql(self, query: str) -> str:
        """Author SQL with ``?``; translate to ``%s`` for Postgres."""
        if self.paramstyle == "qmark":
            return query
        return query.replace("?", "%s")


def connect(url: str) -> Tuple[object, Dialect]:
    """Open a connection for the given URL and return (conn, dialect).

    - ``postgres://…`` / ``postgresql://…`` → psycopg (autocommit)
    - ``sqlite:///path`` / ``sqlite://`` / a raw path / ``:memory:`` → sqlite3
    """
    if url.startswith(("postgres://", "postgresql://")):
        import psycopg  # imported lazily; only needed for the Postgres target
        from psycopg.rows import dict_row

        conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
        return conn, Dialect("pyformat")

    if url.startswith("sqlite:///"):
        path = url[len("sqlite:///"):]
    elif url.startswith("sqlite://"):
        path = url[len("sqlite://"):] or ":memory:"
    else:
        path = url  # raw filesystem path or ":memory:"

    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # autocommit; transaction() manages explicit txns
    return conn, Dialect("qmark")
