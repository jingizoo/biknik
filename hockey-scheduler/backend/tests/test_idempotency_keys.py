"""Idempotency keys for externally-retried writes (#201).

A client that retries a create (network hiccup, double-tap) presents the same
``Idempotency-Key``; the write must run at most once per ``(actor, key)`` and
every retry must replay the first response — never create a duplicate. This
slice wires the mechanism through two representative opaque-id creates
(``create_venue`` and ``create_ice_slot``); the remaining unprotected creates
are a tracked follow-up.

Coverage:
  * Memory / SQLite / PostgreSQL parity — retry replays the identical response
    with no duplicate row; the key is scoped per-actor; re-using a key for a
    different request is an ``idempotency_conflict``; a failed create records
    nothing (a retry re-attempts); no key ⇒ unchanged behavior.
  * Forced PostgreSQL race — two concurrent identical retries, one paused after
    it writes its key row, with a per-PID ``pg_stat_activity`` lock-wait proof:
    exactly one row is created and the loser replays the winner's response.
"""

import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api.service import ApiService
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web.server import STATE, Handler

_SLOT_A = ("2027-03-01T18:00:00+00:00", "2027-03-01T19:30:00+00:00")


def _venue_count(store):
    return len(store.all_venues())


class IdempotencyParityTest(unittest.TestCase):
    def _backends(self):
        yield "memory", InMemoryStore()
        yield "sqlite", SqlStore(":memory:")
        url = os.environ.get("TEST_DATABASE_URL")
        if url:
            SqlStore(url).clear_all_data()
            yield "postgres", SqlStore(url)

    def _close(self, store):
        if isinstance(store, SqlStore):
            store.close()

    def test_migration_present_on_sql(self):
        for label, store in self._backends():
            with self.subTest(backend=label):
                if isinstance(store, SqlStore):
                    self.assertIn("043_idempotency_keys",
                                  store.migration_status()["applied"], label)
                self._close(store)

    def test_retry_replays_and_never_duplicates(self):
        for label, store in self._backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                r1 = api.create_venue("Arena", address="1 Rink Rd",
                                      actor_id="u1", idempotency_key="k1")
                r2 = api.create_venue("Arena", address="1 Rink Rd",
                                      actor_id="u1", idempotency_key="k1")
                self.assertNotIn("error", r1, (label, r1))
                self.assertEqual(r1, r2, label)              # identical replay
                self.assertEqual(_venue_count(store), 1, label)  # no duplicate

                # A third replay is still the same row.
                r3 = api.create_venue("Arena", address="1 Rink Rd",
                                      actor_id="u1", idempotency_key="k1")
                self.assertEqual(r3["id"], r1["id"], label)
                self.assertEqual(_venue_count(store), 1, label)
                self._close(store)

    def test_key_is_scoped_per_actor(self):
        for label, store in self._backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                a = api.create_venue("V", actor_id="u1", idempotency_key="same")
                b = api.create_venue("V", actor_id="u2", idempotency_key="same")
                self.assertNotEqual(a["id"], b["id"], label)  # different actors
                self.assertEqual(_venue_count(store), 2, label)
                self._close(store)

    def test_same_key_different_request_is_conflict(self):
        for label, store in self._backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                api.create_venue("First", actor_id="u1", idempotency_key="k")
                clash = api.create_venue("DIFFERENT", actor_id="u1",
                                         idempotency_key="k")
                self.assertEqual(clash.get("error", {}).get("code"),
                                 "idempotency_conflict", (label, clash))
                self.assertEqual(_venue_count(store), 1, label)  # nothing new
                self._close(store)

    def test_failed_create_records_nothing(self):
        # A create that fails its own validation must NOT burn the key — a later
        # retry with the same key can still succeed (the record is written in the
        # create's transaction, which rolled back).
        for label, store in self._backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                v = api.create_venue("V", actor_id="u1")
                bad = api.create_ice_slot("rink_ghost", *_SLOT_A,
                                          actor_id="u1", idempotency_key="ks")
                self.assertIn("error", bad, label)            # missing rink
                rink = api.create_rink(v["id"], "R1", actor_id="u1")
                ok = api.create_ice_slot(rink["id"], *_SLOT_A,
                                         actor_id="u1", idempotency_key="ks")
                self.assertNotIn("error", ok, (label, ok))    # key was free to reuse
                self._close(store)

    def test_no_key_is_unchanged(self):
        for label, store in self._backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                a = api.create_venue("V", actor_id="u1")
                b = api.create_venue("V", actor_id="u1")
                self.assertNotEqual(a["id"], b["id"], label)  # each a new row
                self.assertEqual(_venue_count(store), 2, label)
                self._close(store)

    def test_ice_slot_retry_replays(self):
        for label, store in self._backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                v = api.create_venue("V", actor_id="u1")
                rink = api.create_rink(v["id"], "R1", actor_id="u1")
                s1 = api.create_ice_slot(rink["id"], *_SLOT_A,
                                         actor_id="u1", idempotency_key="ks")
                s2 = api.create_ice_slot(rink["id"], *_SLOT_A,
                                         actor_id="u1", idempotency_key="ks")
                self.assertNotIn("error", s1, (label, s1))
                self.assertEqual(s1, s2, label)
                self.assertEqual(len(store.all_ice_slots()), 1, label)
                self._close(store)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class IdempotencyRaceTest(unittest.TestCase):
    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        SqlStore(self.url).clear_all_data()

    def _backend_pid(self, store):
        with store.conn.cursor() as cur:
            cur.execute("SELECT pg_backend_pid() AS pid")
            return cur.fetchone()["pid"]

    def _wait_until_blocked_on_lock(self, backend_pid, timeout=10.0):
        import psycopg
        deadline = time.monotonic() + timeout
        with psycopg.connect(self.url, autocommit=True) as mon:
            while time.monotonic() < deadline:
                with mon.cursor() as cur:
                    cur.execute(
                        "SELECT state, wait_event_type FROM pg_stat_activity "
                        "WHERE pid = %s", (backend_pid,))
                    row = cur.fetchone()
                    if (row is not None and row[0] == "active"
                            and row[1] == "Lock"):
                        return True
                time.sleep(0.02)
        return False

    def test_concurrent_retries_create_one_row_loser_replays(self):
        # Two identical retries race. The winner is paused AFTER it writes its
        # idempotency-key row (transaction open, holding the write locks); the
        # loser then blocks on a row lock — proven via pg_stat_activity — until
        # the winner commits, at which point the loser loses the UNIQUE(key_hash)
        # insert, rolls its duplicate venue back, and replays the winner's row.
        winner_store = SqlStore(self.url)
        api_w = ApiService(winner_store)
        loser_store = SqlStore(self.url)
        api_l = ApiService(loser_store)

        orig = winner_store.add_idempotency_key
        written = threading.Event()
        release = threading.Event()
        calls = [0]

        def instrumented(rec):
            result = orig(rec)     # the INSERT takes the UNIQUE(key_hash) lock
            calls[0] += 1
            if calls[0] == 1:
                written.set()
                release.wait(15)
            return result

        winner_store.add_idempotency_key = instrumented
        results = {}

        def run(key, api):
            try:
                results[key] = api.create_venue(
                    "Race Arena", address="1 Rink Rd", actor_id="op",
                    idempotency_key="race-key")
            except Exception as exc:   # a raw driver error would land here → fail
                results[key] = f"ERR:{exc}"

        tw = threading.Thread(target=lambda: run("winner", api_w))
        tl = threading.Thread(target=lambda: run("loser", api_l))
        loser_pid = self._backend_pid(loser_store)
        tw.start()
        if not written.wait(15):
            release.set(); tw.join(15)
            self.fail("winner never wrote its idempotency key")
        tl.start()
        self.assertTrue(self._wait_until_blocked_on_lock(loser_pid),
                        "loser never blocked on a row lock")
        self.assertNotIn("loser", results,
                         "loser completed without blocking on the winner")
        release.set()
        tw.join(25); tl.join(25)

        self.assertNotIn("ERR", str(results.get("winner")), results)
        self.assertNotIn("ERR", str(results.get("loser")), results)
        # Exactly one venue, and both callers observed the SAME response.
        check = SqlStore(self.url)
        self.assertEqual(_venue_count(check), 1, results)
        self.assertEqual(results["winner"], results["loser"], results)
        check.close()
        winner_store.close(); loser_store.close()


class IdempotencyHttpTest(unittest.TestCase):
    """End-to-end: the ``Idempotency-Key`` request header threads through the
    real HTTP handler into the facade, so a retried POST replays."""

    @classmethod
    def setUpClass(cls):
        os.environ.pop("DATABASE_URL", None)   # the default in-memory demo store
        STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def _post(self, path, body, idempotency_key=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "X-Demo-Role": "league_admin"})
        if idempotency_key is not None:
            req.add_header("Idempotency-Key", idempotency_key)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_retried_post_with_key_replays(self):
        before = len(STATE.api.store.all_venues())
        s1, b1 = self._post("/api/setup/venue", {"name": "HTTP Arena"},
                            idempotency_key="http-1")
        s2, b2 = self._post("/api/setup/venue", {"name": "HTTP Arena"},
                            idempotency_key="http-1")
        self.assertEqual(s1, 200, b1)
        self.assertEqual(s2, 200, b2)
        self.assertEqual(b1["id"], b2["id"])                     # replayed
        self.assertEqual(len(STATE.api.store.all_venues()), before + 1)  # one row

        # Same key, different request body → 409 idempotency_conflict.
        s3, b3 = self._post("/api/setup/venue", {"name": "Different"},
                            idempotency_key="http-1")
        self.assertEqual(s3, 409, b3)
        self.assertEqual(b3["error"]["code"], "idempotency_conflict")

        # No key ⇒ a fresh row each time (unchanged behavior).
        _, n1 = self._post("/api/setup/venue", {"name": "NoKey"})
        _, n2 = self._post("/api/setup/venue", {"name": "NoKey"})
        self.assertNotEqual(n1["id"], n2["id"])


if __name__ == "__main__":
    unittest.main()
