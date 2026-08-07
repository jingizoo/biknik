"""A dead database connection must not brick the process (#404).

`store/db.py`'s ``connect()`` opens exactly ONE psycopg connection and
``sql_store.py`` calls it once in ``__init__``. There is no pool, no
reconnect and no retry. So when that connection dies — a managed-Postgres
restart, a failover, a maintenance window, an idle reaper, a network blip —
every store-touching request fails PERMANENTLY until someone restarts the
process. Measured before this change: the first call after
``pg_terminate_backend`` raises ``AdminShutdown``, and every call after it
raises ``OperationalError: the connection is closed``, forever.

Two halves, both here:

  * the store re-establishes a dead connection, so the outage lasts ONE
    request instead of until a human intervenes;
  * ``/api/health`` stops reporting 200 while the database is unreachable, so
    a platform health check can actually restart the instance. Previously
    ``status`` was the literal ``"ok"`` regardless, with reachability
    demoted to a body field nothing acted on.

WHAT IS DELIBERATELY NOT DONE: the failed statement is NOT retried. In
autocommit a write may have committed server-side just before the socket
died, so a silent retry could double-apply it. The request that hits the dead
connection still errors (#403 renders that as a structured 500); recovery is
for the NEXT one. Nor is a reconnect ever attempted inside a transaction —
swapping the connection there would silently discard the writes already made
in it, turning an outage into corruption.

The oracle is structural, never a string match on driver text: psycopg sets
``conn.closed`` for a dead connection and leaves it False for an ordinary SQL
error. Measured both ways in ``test_an_ordinary_sql_error_never_reconnects``.
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web import server as web


PG = os.environ.get("TEST_DATABASE_URL")


@unittest.skipUnless(PG, "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresConnectionRecoveryTest(unittest.TestCase):
    """Driven against a REAL PostgreSQL, with the database — not the
    application — closing the socket. Nothing is monkeypatched: the whole
    point is that this failure arrives from outside the process."""

    def setUp(self):
        self.store = SqlStore(PG)
        self.store.clear_all_data()

    def _kill_my_backend(self):
        """Terminate exactly this store's own backend, from a separate
        connection — what a managed Postgres does on restart/failover."""
        import psycopg
        pid = self.store.conn.info.backend_pid
        with psycopg.connect(PG, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
        return pid

    def test_a_terminated_backend_is_reestablished_on_the_next_call(self):
        # ANTI-VACUITY: healthy first, so a later success cannot be a store
        # that was never really broken.
        self.assertEqual(self.store.all_teams(), [])
        before = self.store.conn.info.backend_pid
        self._kill_my_backend()

        # The request that meets the dead socket still fails. That is the
        # deliberate contract — it is not retried.
        with self.assertRaises(Exception):
            self.store.all_teams()

        # ...and the NEXT one works, on a genuinely different backend.
        self.assertEqual(self.store.all_teams(), [])
        after = self.store.conn.info.backend_pid
        self.assertNotEqual(
            before, after,
            "same backend pid — the connection was never re-established")
        self.assertFalse(self.store.conn.closed)

    def test_recovery_survives_repeated_kills(self):
        """One reconnect is not a fix if the second outage bricks it again."""
        pids = []
        for _ in range(3):
            self.assertEqual(self.store.all_teams(), [])
            pids.append(self.store.conn.info.backend_pid)
            self._kill_my_backend()
            with self.assertRaises(Exception):
                self.store.all_teams()
        self.assertEqual(self.store.all_teams(), [])
        self.assertEqual(len(set(pids)), 3, f"backends reused: {pids}")

    def test_writes_work_after_recovery(self):
        """Reads recovering while writes stay broken would be a false green."""
        self._kill_my_backend()
        with self.assertRaises(Exception):
            self.store.all_teams()
        org = self.store.add_organization(
            __import__("hockey_scheduler.domain", fromlist=["Organization"])
            .Organization(id="org_recov", name="After Recovery"))
        self.assertEqual(org.id, "org_recov")
        self.assertEqual(
            [o.id for o in self.store.all_organizations()], ["org_recov"])

    def test_no_reconnect_inside_a_transaction(self):
        """Atomicity outranks availability.

        Swapping the connection mid-transaction would discard the writes
        already made in it while letting LATER ones commit on a fresh
        autocommit connection — an outage turned into corruption.

        The sequence is exact, and it has to be. Immediately after the kill
        ``conn.closed`` is still False (the client has not noticed), so a
        write there fails identically whether or not the transaction guard
        exists — an earlier version of this test stopped there and passed
        against a build with the guard removed. The first in-transaction
        failure is therefore swallowed ON PURPOSE, to reach the state that
        actually discriminates: connection KNOWN closed, still inside the
        transaction. A build that heals there commits ``org_after_death``.
        """
        import hockey_scheduler.domain as domain
        with self.assertRaises(Exception):
            with self.store.transaction():
                self.store.add_organization(
                    domain.Organization(id="org_txn", name="In Flight"))
                self._kill_my_backend()
                try:
                    self.store.add_organization(
                        domain.Organization(id="org_txn2", name="Doomed"))
                except Exception:
                    pass          # sets conn.closed; still inside the txn
                self.assertTrue(self.store.conn.closed, "setup did not arm")
                self.store.add_organization(
                    domain.Organization(id="org_after_death", name="Corrupt"))

        ids = [o.id for o in self.store.all_organizations()]
        self.assertNotIn(
            "org_after_death", ids,
            "a write made after a mid-transaction reconnect was committed on "
            "its own — the aborted transaction leaked a row")
        # The whole transaction committed nothing.
        self.assertNotIn("org_txn", ids)
        self.assertNotIn("org_txn2", ids)

    def test_close_during_an_in_flight_reconnect_wins(self):
        """The close/reconnect race (#404 review).

        The sequential reset tests cannot falsify this: they close a store
        nobody is touching. The dangerous interleaving is a request already
        INSIDE the reconnect critical section when a reset closes the store.
        If the two are not serialized on one lock, the request assigns a fresh
        connection to a store that was deliberately closed — resurrecting a
        superseded demo store, and leaking a PostgreSQL connection that
        nothing will ever close.

        ``connect`` is barriered so the reconnect is provably parked inside
        its critical section when ``close()`` is called. There is no sleep and
        no timing assumption: the events make the interleaving exact.
        """
        import hockey_scheduler.store.sql_store as ss

        real_connect = ss.connect
        inside = threading.Event()     # reconnect is in its critical section
        release = threading.Event()    # let it finish
        made = []

        def barriered_connect(url):
            inside.set()
            release.wait(10)
            result = real_connect(url)
            made.append(result[0])
            return result

        # Arm: kill the backend and burn the one unavoidable failure, so the
        # next call takes the reconnect path.
        self._kill_my_backend()
        with self.assertRaises(Exception):
            self.store.all_teams()
        self.assertTrue(self.store.conn.closed, "setup did not arm")

        ss.connect = barriered_connect
        try:
            reconnect_done = threading.Event()
            close_returned = threading.Event()

            def do_query():
                try:
                    self.store.all_teams()
                except Exception:
                    pass
                reconnect_done.set()

            def do_close():
                self.store.close()
                close_returned.set()

            a = threading.Thread(target=do_query, daemon=True)
            a.start()
            self.assertTrue(inside.wait(10), "reconnect never entered connect()")

            b = threading.Thread(target=do_close, daemon=True)
            b.start()
            # close() must BLOCK: the reconnect holds the lock. If it returns
            # here, the two are not serialized and the assignment below will
            # land on an already-closed store.
            self.assertFalse(
                close_returned.wait(1.0),
                "close() returned while a reconnect was inside its critical "
                "section — they are not serialized on the same lock")

            release.set()
            a.join(10)
            b.join(10)
            self.assertTrue(close_returned.is_set(), "close() never returned")
        finally:
            ss.connect = real_connect
            release.set()

        # Close wins, whichever order the lock granted.
        self.assertTrue(self.store._closed, "the store is not marked closed")
        self.assertTrue(self.store.conn.closed,
                        "a live connection survived close()")
        for conn in made:
            self.assertTrue(
                conn.closed,
                "a connection created by the racing reconnect is still live "
                "— that is the leak")
        # ...and it stays closed: no later query may resurrect it.
        with self.assertRaises(Exception):
            self.store.all_teams()
        self.assertTrue(self.store.conn.closed)

    def test_a_closed_store_never_reconnects_on_a_later_query(self):
        """The sequential half of the same contract.

        Guards the under-lock ``_closed`` recheck specifically: with it gone,
        a query after ``close()`` sees a closed connection, reconnects, and
        the store silently serves again.
        """
        import hockey_scheduler.store.sql_store as ss

        real_connect = ss.connect
        made = []

        def counting_connect(url):
            result = real_connect(url)
            made.append(result[0])
            return result

        self.store.close()
        ss.connect = counting_connect
        try:
            for _ in range(3):
                with self.assertRaises(Exception):
                    self.store.all_teams()
        finally:
            ss.connect = real_connect
        self.assertEqual(
            made, [],
            "a closed store opened a new connection — close() was undone")
        self.assertTrue(self.store.conn.closed)

    def test_an_ordinary_sql_error_never_reconnects(self):
        """The discriminator. A failing query is not a failing connection.

        Reconnecting on any error would mask real bugs and silently drop
        session state. `conn.closed` separates the two, and this pins that it
        is what the code actually keys on.
        """
        before = self.store.conn.info.backend_pid
        with self.assertRaises(Exception):
            self.store._exec("SELECT * FROM definitely_not_a_table")
        self.assertFalse(self.store.conn.closed)
        self.assertEqual(
            before, self.store.conn.info.backend_pid,
            "an ordinary SQL error reconnected — that would hide real bugs")
        self.assertEqual(self.store.all_teams(), [])


class HealthStatusContractTest(unittest.TestCase):
    """`/api/health` must fail its STATUS when the database is unreachable.

    It previously returned the literal ``"ok"`` with 200 regardless, demoting
    reachability to a body field. A platform health check reads the status
    code, so a bricked instance looked healthy and was never restarted — which
    is what made #404's outage last until a human noticed.
    """

    def setUp(self):
        self.api = ApiService(InMemoryStore())

    def test_healthy_database_is_ok(self):
        """Control: the healthy shape is unchanged."""
        health = self.api.get_health()
        self.assertEqual(health["status"], "ok")
        self.assertTrue(health["database_reachable"])

    def test_unreachable_database_is_not_ok(self):
        self.api.store.db_reachable = lambda: False
        health = self.api.get_health()
        self.assertNotEqual(
            health["status"], "ok",
            "health reported ok while the database was unreachable")
        self.assertFalse(health["database_reachable"])


class HealthHttpStatusTest(unittest.TestCase):
    """The same contract at the HTTP layer, where the platform reads it."""

    @classmethod
    def setUpClass(cls):
        web.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()

    def _get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urllib.request.urlopen(url) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_health_is_200_when_the_database_is_reachable(self):
        status, body = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_health_is_not_200_when_the_database_is_unreachable(self):
        original = web.STATE.api.store.db_reachable
        web.STATE.api.store.db_reachable = lambda: False
        try:
            status, body = self._get("/api/health")
        finally:
            web.STATE.api.store.db_reachable = original
        self.assertNotEqual(
            status, 200,
            "a platform health check reads the STATUS CODE — 200 here is why "
            "a bricked instance is never restarted")
        self.assertEqual(status, 503)
        self.assertFalse(body["database_reachable"])
        # Still a structured, public, non-sensitive payload.
        self.assertNotEqual(body["status"], "ok")

    def test_health_recovers_to_200(self):
        """Anti-vacuity for the failure above: the endpoint is not simply
        broken now — it returns to 200 once the database is reachable."""
        original = web.STATE.api.store.db_reachable
        web.STATE.api.store.db_reachable = lambda: False
        self._get("/api/health")
        web.STATE.api.store.db_reachable = original
        status, body = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")


if __name__ == "__main__":
    unittest.main()
