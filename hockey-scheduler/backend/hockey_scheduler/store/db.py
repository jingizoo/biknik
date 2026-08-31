"""Database connection + tiny dialect shim for the SQL store.

PostgreSQL is the product target; SQLite (stdlib) is the local/dev/test adapter
so the SQL persistence layer can be exercised without a Postgres server. Both
drivers speak DB-API 2.0, so the store is written once against ``?`` placeholders
and the dialect translates for Postgres.
"""

import sqlite3
from typing import Optional, Tuple


class StoreConnectionError(RuntimeError):
    """The configured database could not be reached while connecting.

    This phase-specific wrapper is intentionally narrower than a driver's
    ``OperationalError``: the same broad driver type can also be raised later
    by migrations or queries, where degrading as ``store_unreachable`` would
    hide a real boot/programming failure.
    """


def _postgres_is_unreachable(exc) -> bool:
    """Whether a psycopg connect failure positively identifies availability.

    Psycopg deliberately maps both network failures *and* invalid libpq
    options (for example ``sslmode=bogus`` or a non-numeric port) to the same
    broad ``OperationalError`` with no SQLSTATE.  Degrading on that type alone
    would hide configuration errors indefinitely.  SQLSTATE class 08 is the
    server/driver connection-exception class; for failures that happen before
    a server can supply SQLSTATE, accept only libpq's concrete DNS/socket/
    timeout diagnostics.  Unknown shapes fail closed at boot.
    """
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate:
        return sqlstate.startswith("08") or sqlstate == "57P03"

    message = str(exc).casefold()
    return any(marker in message for marker in (
        "failed to resolve host ",
        "could not translate host name ",
        "could not connect to server",
        "connection refused",
        "connection reset by peer",
        "connection timed out",
        "connection timeout expired",
        "timeout expired",
        "network is unreachable",
        "no route to host",
        "server closed the connection unexpectedly",
        "could not receive data from server",
        "could not send data to server",
    ))


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

        # client_encoding is DECLARED, never inherited (#405). On a SQL_ASCII
        # database the server performs no encoding conversion, so without this
        # psycopg cannot decode TEXT and returns `bytes` for every text column.
        # The first symptom is that the app CANNOT RESTART: migrate() compares
        # a str version against a set holding b'001_initial', concludes nothing
        # is applied, re-runs every migration and dies on
        # schema_migrations_pkey — while the ledger is perfectly intact. The
        # first deploy succeeds and every restart afterwards fails, which reads
        # as database corruption rather than an encoding mismatch.
        #
        # Fixing it at the connection, not in migrate(), is deliberate: ids,
        # names and audit actions are TEXT too, so decoding just the version
        # set would move the failures somewhere less obvious instead of
        # removing them. SQL_ASCII is what `initdb` produces under LC_ALL=C,
        # a very common way to script a cluster.
        try:
            conn = psycopg.connect(url, autocommit=True, row_factory=dict_row,
                                   client_encoding="UTF8")
        except psycopg.OperationalError as exc:
            # OperationalError is not itself an availability verdict: libpq
            # uses it for malformed ports, invalid SSL modes, authentication,
            # serialization failures, and genuine network outages alike.
            if not _postgres_is_unreachable(exc):
                raise
            raise StoreConnectionError(str(exc)) from exc
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
