"""The app must declare its client encoding (#405).

`store/db.py`'s ``connect()`` opened the PostgreSQL connection without
declaring ``client_encoding``. On a **SQL_ASCII** database the server performs
no encoding conversion, so psycopg cannot decode ``TEXT`` and hands back
``bytes`` for every text column.

The visible symptom is a hard crash on the SECOND ``SqlStore`` against an
already-migrated database — i.e. **the app cannot restart**. ``migrate()``
compares a ``str`` version against a set holding ``b'001_initial'``, decides
nothing is applied, re-runs every migration, and dies:

    psycopg.errors.UniqueViolation: duplicate key value violates unique
    constraint "schema_migrations_pkey"
    DETAIL:  Key (version)=(001_initial) already exists.

The ledger is intact throughout; only the comparison is broken. The first
deploy therefore succeeds and every restart afterwards fails, which is the
worst possible shape — it looks like database corruption rather than an
encoding mismatch.

SQL_ASCII is a legitimate PostgreSQL configuration and is exactly what
``initdb`` produces under ``LC_ALL=C``, a very common way to script a cluster.

WHY DECODING THE VERSION SET IS NOT THE FIX: it would repair this one
comparison while leaving every other ``TEXT`` column returning bytes — ids,
names, audit actions — so the failures would simply move somewhere less
obvious. Declaring the encoding fixes the whole connection at its source.

These tests build their own SQL_ASCII database, so they run against CI's
stock UTF8 ``postgres:16`` image with no special setup.
"""

import os
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.store import SqlStore


PG = os.environ.get("TEST_DATABASE_URL")
ASCII_DB = "hs_sql_ascii_probe"


@unittest.skipUnless(PG, "PostgreSQL required (set TEST_DATABASE_URL)")
class SqlAsciiEncodingTest(unittest.TestCase):
    """A SQL_ASCII database must behave like any other."""

    @classmethod
    def setUpClass(cls):
        import psycopg
        cls.psycopg = psycopg
        base = PG.rstrip("/")
        cls.admin_url = base
        cls.ascii_url = base.rsplit("/", 1)[0] + "/" + ASCII_DB
        with psycopg.connect(base, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{ASCII_DB}"')
            c.execute(f'CREATE DATABASE "{ASCII_DB}" ENCODING \'SQL_ASCII\' '
                      "LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0")

    @classmethod
    def tearDownClass(cls):
        with cls.psycopg.connect(cls.admin_url, autocommit=True) as c:
            c.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()", (ASCII_DB,))
            c.execute(f'DROP DATABASE IF EXISTS "{ASCII_DB}"')

    def test_the_probe_database_really_is_sql_ascii(self):
        """ANTI-VACUITY. Every assertion below is about SQL_ASCII behaviour;
        if the fixture quietly produced a UTF8 database they would all pass
        against the unfixed code and prove nothing."""
        with self.psycopg.connect(self.ascii_url, autocommit=True) as c:
            enc = c.execute("SELECT current_setting('server_encoding')").fetchone()[0]
        # Decoded here only so the assertion reads the same on both sides —
        # that this value can ARRIVE as bytes is the defect itself.
        if isinstance(enc, bytes):
            enc = enc.decode("ascii")
        self.assertEqual(enc, "SQL_ASCII")

    def test_a_second_store_on_a_migrated_database_starts(self):
        """The restart path: first deploy migrates, every restart afterwards
        constructs a new store against the already-migrated database."""
        first = SqlStore(self.ascii_url)
        try:
            second = SqlStore(self.ascii_url)   # must not raise
        except Exception as exc:                 # pragma: no cover - failure path
            self.fail(f"the app cannot restart on a SQL_ASCII database: "
                      f"{type(exc).__name__}: {exc}")
        try:
            self.assertEqual(second.all_teams(), [])
        finally:
            second.close()
            first.close()

    def test_text_columns_come_back_as_str(self):
        """The root cause, asserted directly rather than through its symptom.

        `migrate()`'s membership test is only the first thing to break; ids,
        names and audit actions are all TEXT too.
        """
        store = SqlStore(self.ascii_url)
        try:
            row = store._exec(
                "SELECT version FROM schema_migrations ORDER BY version "
                "LIMIT 1").fetchone()
            value = row["version"]
            self.assertIsInstance(
                value, str,
                f"TEXT came back as {type(value).__name__} — every string "
                f"comparison in the app is unreliable on this connection")
            # And the comparison migrate() actually performs works.
            self.assertIn(value, {value})
        finally:
            store.close()

    def test_migrations_are_not_reapplied(self):
        """The ledger is intact and stays intact: a restart must add no rows."""
        store = SqlStore(self.ascii_url)
        try:
            before = len(store._exec(
                "SELECT version FROM schema_migrations").fetchall())
            self.assertGreater(before, 0, "fixture did not migrate")
        finally:
            store.close()
        again = SqlStore(self.ascii_url)
        try:
            after = len(again._exec(
                "SELECT version FROM schema_migrations").fetchall())
        finally:
            again.close()
        self.assertEqual(
            before, after,
            "constructing a second store re-applied migrations")


if __name__ == "__main__":
    unittest.main()
