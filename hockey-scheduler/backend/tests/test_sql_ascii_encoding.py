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
stock UTF8 ``postgres:16`` image with no image or cluster changes.

FIXTURE SAFETY. Creating a database is a cluster-global act, so the fixture is
built to be safe on a cluster that is NOT disposable:

  * the name is unique per run (``uuid4``), never a fixed shared one — two
    concurrent workers cannot collide, and no pre-existing database can share
    it;
  * nothing is ever dropped that this fixture did not create. There is no
    ``DROP DATABASE IF EXISTS`` on a well-known name, which on a developer or
    staging cluster could destroy real data that merely shares a name;
  * ``CREATEDB`` is a privilege the connected role may not have. CI's
    ``POSTGRES_USER`` happens to be a superuser, but a least-privileged
    application role is not, so the tests SKIP with a precise reason rather
    than erroring.
"""

import os
import unittest
import uuid
from urllib.parse import urlsplit, urlunsplit

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.store import SqlStore


PG = os.environ.get("TEST_DATABASE_URL")
# Unique per run: this database is CREATED and DROPPED, so a fixed name would
# be both a collision risk between workers and a data-loss risk on any cluster
# where something else happens to own that name.
ASCII_DB = f"hs_ascii_probe_{uuid.uuid4().hex[:16]}"


def _with_database(url, dbname):
    """``url`` pointed at ``dbname``, with every other connection parameter
    left exactly as it was.

    The database name is the URL's PATH segment, and it is the only part that
    may be rewritten. A libpq URL can ALSO carry connection parameters in a
    query string, and those routinely contain slashes: the socket form
    ``postgresql://hockey@/hockey?host=/tmp&port=55643`` is what a local
    ``initdb``/``pg_ctl`` cluster produces and what ``psql -h /tmp`` speaks.

    Splitting the whole URL on its last ``/`` — as this fixture originally did
    — cuts inside ``host=/tmp`` on that form and yields
    ``postgresql://hockey@/hockey?host=/hs_ascii_probe_…``: the probe database
    name becomes a socket DIRECTORY, the port disappears with the rest of the
    truncated query string, and all four tests below ERROR on a connection
    failure that has nothing to do with #405. Split the URL structurally
    instead, so anything after ``?`` is carried through untouched.
    """
    return urlunsplit(urlsplit(url)._replace(path="/" + dbname))


def _database_of(url):
    """The database ``url`` names — its path segment, and nothing else.

    ``?host=/tmp`` is a connection parameter, not a path, so anything that
    reads the database name off the END of the string reads that instead.
    """
    return urlsplit(url).path.strip("/")


@unittest.skipUnless(PG, "PostgreSQL required (set TEST_DATABASE_URL)")
class SqlAsciiEncodingTest(unittest.TestCase):
    """A SQL_ASCII database must behave like any other."""

    created = False

    @classmethod
    def setUpClass(cls):
        import psycopg
        cls.psycopg = psycopg
        # Both URLs go through the same structural rewrite: it is what keeps
        # the probe out of the connection parameters, and it also normalises
        # away a trailing slash that would otherwise land in the database name.
        cls.admin_url = _with_database(PG, _database_of(PG))
        cls.ascii_url = _with_database(PG, ASCII_DB)
        with psycopg.connect(cls.admin_url, autocommit=True) as c:
            row = c.execute(
                "SELECT rolsuper OR rolcreatedb AS may_create FROM pg_roles "
                "WHERE rolname = current_user").fetchone()
            may_create = bool(row and (row[0] if not isinstance(row, dict)
                                       else row["may_create"]))
            if not may_create:
                raise unittest.SkipTest(
                    "the test role lacks CREATEDB, so a SQL_ASCII database "
                    "cannot be provisioned; run this suite as a role with "
                    "CREATEDB to cover #405")
            # No DROP first: the name is unique, so a collision would mean
            # something is badly wrong and must surface, never be dropped.
            c.execute(f'CREATE DATABASE "{ASCII_DB}" ENCODING \'SQL_ASCII\' '
                      "LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0")
            cls.created = True

    @classmethod
    def tearDownClass(cls):
        # Drop ONLY what this fixture created.
        if not cls.created:
            return
        with cls.psycopg.connect(cls.admin_url, autocommit=True) as c:
            c.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()", (ASCII_DB,))
            c.execute(f'DROP DATABASE IF EXISTS "{ASCII_DB}"')
            cls.created = False

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


class ProbeUrlConstructionTest(unittest.TestCase):
    """The probe URL must name the probe DATABASE, on every URL form libpq
    accepts.

    Pure string work, so — unlike the fixture it serves — this runs everywhere,
    with or without a PostgreSQL server. It has to: the defect it pins made all
    four tests above ERROR rather than fail, which reads like a broken
    environment rather than a bug in this file, and it struck only the people
    whose ``TEST_DATABASE_URL`` used the socket form. On a TCP URL the old
    last-slash split happened to be right, so ``test_a_tcp_url_…`` alone would
    pass against the unfixed code and prove nothing; ``test_a_socket_url_…`` is
    the one that fails against it, and the TCP case is here to pin that the fix
    did not trade one form for the other.
    """

    # The two shapes a local cluster hands out: a UNIX socket (what `initdb`
    # + `pg_ctl -k /tmp` and most run-it-yourself setups produce) and TCP.
    SOCKET = "postgresql://hockey@/hockey_415?host=/tmp&port=55643"
    TCP = "postgresql://hockey@127.0.0.1:55643/hockey_415"

    def test_a_socket_url_keeps_its_connection_parameters(self):
        """THE REGRESSION. Splitting on the last ``/`` cut inside ``host=/tmp``
        and produced ``postgresql://hockey@/hockey_415?host=/probe`` — a socket
        directory named after the database, on the default port, against the
        original database."""
        self.assertEqual(
            _with_database(self.SOCKET, "probe"),
            "postgresql://hockey@/probe?host=/tmp&port=55643")

    def test_a_tcp_url_still_gets_its_database_replaced(self):
        self.assertEqual(
            _with_database(self.TCP, "probe"),
            "postgresql://hockey@127.0.0.1:55643/probe")

    def test_the_database_name_is_read_from_the_path_not_the_tail(self):
        """What the admin URL is normalised with. The socket form's tail is
        ``/tmp`` — a directory, not a database."""
        self.assertEqual(_database_of(self.SOCKET), "hockey_415")
        self.assertEqual(_database_of(self.TCP), "hockey_415")
        self.assertEqual(_database_of(self.TCP + "/"), "hockey_415")

    def test_libpq_reads_the_rewritten_urls_the_way_this_module_intends(self):
        """String equality above asserts what WE think those URLs mean; this
        asserts what the driver thinks, which is the part that actually
        decides whether the fixture connects."""
        try:
            from psycopg.conninfo import conninfo_to_dict
        except ImportError:                      # driver optional for the rest
            raise unittest.SkipTest("psycopg is not installed")
        for url, host in ((self.SOCKET, "/tmp"), (self.TCP, "127.0.0.1")):
            with self.subTest(url=url):
                info = conninfo_to_dict(_with_database(url, "probe"))
                self.assertEqual(info.get("dbname"), "probe")
                self.assertEqual(info.get("host"), host)
                self.assertEqual(info.get("port"), "55643")


if __name__ == "__main__":
    unittest.main()
