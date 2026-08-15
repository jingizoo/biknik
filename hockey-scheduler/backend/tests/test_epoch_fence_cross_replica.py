"""Round-N+1 finding 3 REWORK: the exact deterministic cross-replica
AUTHENTICATED-HTTP regression the owner named — not a store-level-only proof.

TWO INDEPENDENT, REAL HTTP SERVER PROCESSES (``_cross_replica_server.py``,
launched via ``subprocess.Popen`` — mirroring ``e2e/server-park-harness.py``'s
own "run the real, unmodified app as a subprocess" convention), both bound to
the SAME real PostgreSQL database. A genuinely separate OS process is what
makes this a real proof rather than an approximation of one:
``services/context_gate.py``'s ``CONTEXT_GATE``/``LIFECYCLE_GATE`` and
``web/server.py``'s ``SESSIONS`` are Python module-level singletons — a
SECOND process cannot share them even by accident, which is exactly the
"HONEST SCOPE LIMIT" ``context_gate.py`` already documents about itself and
is precisely the gap finding 3 exists to close at the DATABASE layer instead.

WHAT THIS PROVES, for EVERY authenticated ``POST /api/context`` response
either replica returns IN THE TWO DETERMINISTICALLY-ORDERED CASES below:
independently RECOMPUTE ``context_epoch()`` from a FRESH read of the
persisted ``ActiveContext`` row (a THIRD, short-lived connection to the same
database — never either replica's own) and assert it equals the epoch
carried in THAT SAME response. This is the HTTP-level contract finding 3 is
about; ``tests/test_active_context.py``'s ``ContextSnapshotConsistencyPgTest``
already proves the SERVICE-level one (``ContextService.
set_with_league_and_epoch`` itself), and ``tests/test_epoch_fence.py``'s
cross-process falsifiability class already proves the SCOPED-READ side of
cross-process ordering — this file is the missing third leg: the WRITE side,
over real authenticated HTTP, across two real processes.

DETERMINISM, stated precisely. Every HTTP call here is SYNCHRONOUS (a client
waits for the response before the next request is sent), so a SEQUENTIAL
A->B->A (or B->A->B) ordering is deterministic BY CONSTRUCTION — no park or
control-plane seam is needed to force it, unlike the same-process gate tests'
own parked-read machinery, because nothing here needs to interleave WITHIN
one request's handling, only to control which of two INDEPENDENT requests is
DISPATCHED first. ``test_both_orderings_each_response_matches_the_persisted_
row`` drives A->B->A and, as a separate case, B->A->B; both check EVERY
response, immediately, against a fresh read — the strong per-response claim
above, proven exhaustively because no SECOND writer is ever racing the
window between "the response arrives" and "the fresh check runs" in either
ordering. A third case, ``test_genuinely_concurrent_switches_each_response_
still_matches_its_own_commit``, additionally fires one request at EACH
replica at (as close to) the same instant as two Python threads in this
process can manage; ITS invariant is necessarily weaker and stated precisely
in its own docstring — the per-response "matches a fresh read taken right
after" claim is UNSOUND once a second writer for the same row is genuinely
in flight (measured directly: an earlier revision of this test asserted it
for both legs and failed, deterministically, on whichever leg was not the
final committer), so what this third case instead proves is that the row
never lands on a THIRD, phantom state matching neither concurrent response —
no lost or blended update, regardless of which the database happens to
order first.
"""

import os
import subprocess
import sys
import threading
import time
import unittest
import uuid
from datetime import datetime, timezone
from http.cookiejar import CookieJar
import json
import urllib.error
import urllib.request

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api.service import ApiService
from hockey_scheduler.domain import Role
from hockey_scheduler.store import SqlStore

PATIENCE = 20.0
_READY_TIMEOUT = 15.0


def _pg_url():
    return os.environ.get("TEST_DATABASE_URL")


def _pg_db_url(base_url, dbname):
    head, _, _db = base_url.rpartition("/")
    return f"{head}/{dbname}"


class _Replica:
    """One real server subprocess, plus the plain-HTTP client helpers to
    talk to it. A `with` context so a failed test still kills the process."""

    def __init__(self, port, database_url):
        self.port = port
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "_cross_replica_server.py")
        self.proc = subprocess.Popen(
            [sys.executable, script, "--port", str(port),
             "--database-url", database_url],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True)
        self._wait_ready()

    def _wait_ready(self):
        deadline = time.monotonic() + _READY_TIMEOUT
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                out = self.proc.stdout.read() if self.proc.stdout else ""
                raise RuntimeError(
                    f"replica on port {self.port} exited early "
                    f"(code {self.proc.returncode}):\n{out}")
            line = self.proc.stdout.readline()
            if line.strip() == f"READY {self.port}":
                return
        raise RuntimeError(f"replica on port {self.port} never became ready")

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)

    # -- HTTP -----------------------------------------------------------
    def client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def req(self, opener, method, path, body=None, timeout=PATIENCE):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with opener.open(request, timeout=timeout) as r:
                raw = r.read()
                return r.status, json.loads(raw or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, json.loads(raw or b"{}")

    def login(self, username, password="demo"):
        c = self.client()
        status, body = self.req(c, "POST", "/api/auth/login",
                                {"username": username, "password": password})
        assert status == 200, (status, body)
        return c

    def switch(self, client, program_id, season_id):
        status, body = self.req(client, "POST", "/api/context",
                                {"program_id": program_id,
                                 "season_id": season_id})
        assert status == 200, (status, body)
        assert "context_epoch" in body, body
        return body


@unittest.skipUnless(_pg_url(), "PostgreSQL required (set TEST_DATABASE_URL)")
class CrossReplicaAuthenticatedHttpTest(unittest.TestCase):
    """Two real ``ThreadingHTTPServer`` PROCESSES, one real PostgreSQL
    database. See module docstring for the full claim."""

    @classmethod
    def setUpClass(cls):
        ts = int(time.time())
        cls.db_url = _pg_db_url(_pg_url(), f"hs_xrepl_{ts}_{uuid.uuid4().hex[:6]}")
        # Bootstrap the DATABASE ITSELF (a cluster-level object) via a
        # throwaway connection to the ADMIN db — mirrors `run_parallel.py`'s
        # own `_ensure_pg_database`. Schema migration is deliberately NOT done
        # here; see the ORDERING note below for why.
        import psycopg
        admin = _pg_db_url(_pg_url(), "postgres")
        dbname = cls.db_url.rsplit("/", 1)[1]
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{dbname}"')

        # ORDERING: start both replicas FIRST, and create the fixture ONLY
        # AFTER both report READY — never before.
        #
        # THE BUG THIS AVOIDS (found by running this file, not reasoned about
        # in the abstract): `_cross_replica_server.py` imports
        # `hockey_scheduler.web.server`, and that import's module-level
        # `STATE = DemoState()` (`web/server.py`) runs `DemoState.__init__` ->
        # `self.reset(seed=False)` UNCONDITIONALLY unless `APP_MODE=production`
        # — which this harness deliberately does NOT set (see below). In the
        # default "demo" posture, `reset(seed=False)` calls `store.
        # reset_schema()` and reseeds a single "admin" persona: a full,
        # unconditional WIPE of whatever the database held. An earlier
        # revision of this fixture created the program/Season/account fixture
        # BEFORE starting either replica, so EACH replica's own boot-time
        # reset silently erased it — the account genuinely did not exist by
        # the time either replica served a request, and every login in this
        # file failed with a bare 401 ("Invalid username or password"), not a
        # framework error. Migration itself is idempotent (`SqlStore.__init__`
        # -> `migrate()` only applies pending versions) so running it twice
        # (once per replica's own `SqlStore` construction) is harmless; the
        # DESTRUCTIVE step is `reset_schema()`, and it only ever runs at
        # IMPORT time, which is strictly BEFORE `_Replica._wait_ready()`
        # returns (the "READY" line is printed only after the import, and
        # therefore the reset, has already completed) — so waiting for BOTH
        # replicas to report ready before writing a single fixture row is
        # sufficient to guarantee no further reset can ever observe it.
        #
        # WHY NOT `APP_MODE=production` INSTEAD (the more obvious-looking
        # fix — it also disables the reset, per `DemoState.reset`'s own
        # production branch): it has a SECOND effect this file cannot absorb.
        # `_cookie_is_secure` (`web/server.py`) is unconditionally True in
        # production, so the session cookie `POST /api/auth/login` issues
        # would carry `Secure`. `http.cookiejar.CookieJar` (this file's real
        # `_Replica.client()`) correctly enforces that flag as any real
        # browser would: it refuses to attach a Secure cookie to a subsequent
        # PLAIN `http://127.0.0.1` request, which is all this harness ever
        # speaks (real TLS termination is out of scope for a same-host
        # two-process test). Login itself would still succeed, but every
        # following authenticated call (every `switch()`) would silently
        # revert to anonymous and fail differently — worse, not better.
        # Reordering avoids the destructive reset without touching
        # `_app_mode()` at all, so production's cookie hardening stays
        # exactly as strict as #76 requires.
        cls.replica_a = _Replica(_free_port(), cls.db_url)
        cls.replica_b = _Replica(_free_port(), cls.db_url)

        # NOW safe: both replicas are past their own one-time boot reset, so
        # nothing left running will ever wipe this. A fresh, throwaway
        # connection — never either replica's own — mirroring the read-side
        # independence `_fresh_epoch` below also insists on.
        bootstrap = SqlStore(cls.db_url)
        api = ApiService(bootstrap)
        program = api.create_program(f"XRepl {ts}", "US", "UTC")
        season_a = api.create_season(program["id"], "Season A")
        season_b = api.create_season(program["id"], "Season B")
        season_c = api.create_season(program["id"], "Season C")
        username = f"xrepl_{uuid.uuid4().hex[:10]}"
        account = api.accounts.create_account(
            username, "demo", Role.LEAGUE_ADMIN)
        cls.program_id = program["id"]
        cls.season_a = season_a["id"]
        cls.season_b = season_b["id"]
        cls.season_c = season_c["id"]
        cls.username = username
        cls.user_id = account.id
        bootstrap.close()

    @classmethod
    def tearDownClass(cls):
        cls.replica_a.close()
        cls.replica_b.close()
        import psycopg
        admin = _pg_db_url(_pg_url(), "postgres")
        dbname = cls.db_url.rsplit("/", 1)[1]
        try:
            with psycopg.connect(admin, autocommit=True) as conn:
                conn.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (dbname,))
                conn.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
        except Exception:
            pass

    # -- the independent recomputation ------------------------------------
    def _fresh_epoch(self, user_id):
        """Read the persisted ActiveContext row fresh (a NEW connection —
        never either replica's own) and recompute context_epoch() from it —
        the independent, ground-truth check every response is judged
        against."""
        store = SqlStore(self.db_url)
        try:
            api = ApiService(store)
            return api.context.current_epoch(user_id, Role.LEAGUE_ADMIN, {})
        finally:
            store.close()

    def _assert_response_epoch_matches_persisted(self, label, epoch):
        fresh = self._fresh_epoch(self.user_id)
        self.assertEqual(
            epoch, fresh,
            f"{label}: the epoch this HTTP response carried does not match "
            f"a FRESH, independent read of the persisted row taken right "
            f"after it — {epoch!r} != {fresh!r}")

    # -- deterministic sequential orderings --------------------------------
    def _run_ordering(self, label, first, second, first_season, second_season,
                      third_season):
        """``first``/``second`` are (replica, client) pairs for the SAME
        underlying account, on DIFFERENT replicas. Drives
        first->second->first, i.e. A->B->A when ``first`` is A."""
        r1, c1 = first
        r2, c2 = second
        body1 = r1.switch(c1, self.program_id, first_season)
        self._assert_response_epoch_matches_persisted(
            f"{label} leg 1", body1["context_epoch"])

        body2 = r2.switch(c2, self.program_id, second_season)
        self._assert_response_epoch_matches_persisted(
            f"{label} leg 2", body2["context_epoch"])
        self.assertNotEqual(
            body1["context_epoch"], body2["context_epoch"],
            f"{label}: switching to a DIFFERENT season on the OTHER replica "
            f"did not move the epoch")

        body3 = r1.switch(c1, self.program_id, third_season)
        self._assert_response_epoch_matches_persisted(
            f"{label} leg 3", body3["context_epoch"])
        self.assertNotEqual(
            body3["context_epoch"], body1["context_epoch"],
            f"{label}: leg 3 reused leg 1's epoch — the SAME class of bug "
            f"finding 5's generation counter closed for one process, now "
            f"observed (or not) ACROSS two")
        self.assertNotEqual(
            body3["context_epoch"], body2["context_epoch"],
            f"{label}: leg 3 reused leg 2's epoch")

    def test_both_orderings_each_response_matches_the_persisted_row(self):
        a, b = self.replica_a, self.replica_b
        ca = a.login(self.username)
        cb = b.login(self.username)
        with self.subTest(ordering="A->B->A"):
            self._run_ordering("A->B->A", (a, ca), (b, cb),
                               self.season_a, self.season_b, self.season_c)
        with self.subTest(ordering="B->A->B"):
            self._run_ordering("B->A->B", (b, cb), (a, ca),
                               self.season_b, self.season_a, self.season_c)

    # -- genuinely concurrent, undetermined winner --------------------------
    def test_genuinely_concurrent_switches_each_response_still_matches_its_own_commit(self):
        """Fires one switch at EACH replica at (as close to) the same
        instant as two threads in this process can manage. Whichever the
        database actually orders first is NOT controlled — the claim under
        test is that it does not matter: the row never ends up describing a
        THIRD, phantom state that corresponds to NEITHER commit (a torn or
        lost-update write), regardless of which of the two the database
        happens to order first.

        WHY NOT "each response's own epoch matches a fresh read taken right
        after it arrives" (the naive per-leg claim an earlier revision of
        this test asserted, for BOTH legs, right here). That claim is sound
        for a SEQUENTIAL ordering (see ``_run_ordering`` above: there is no
        SECOND writer racing the exact window between "the response arrives"
        and "the fresh check runs") but unsound for a genuinely concurrent
        one: whichever leg commits FIRST is, BY CONSTRUCTION, eligible to be
        overwritten by the second leg's commit at any point after its own
        response is already in the client's hand — including inside the gap
        between that arrival and this test's own follow-up read, a gap this
        test has no way to close without adding server-side instrumentation
        the production code has no other reason to carry. Measured directly,
        not merely reasoned about: an earlier revision of this test applied
        that check to BOTH legs and failed, deterministically, on whichever
        leg turned out to be the first (i.e. non-final) committer — not a
        flaky timing artifact, a structural mismatch between the claim and
        what a genuine write/write race can guarantee.

        THE INVARIANT THAT DOES HOLD, unconditionally, for two writers
        racing the SAME row: the row's FINAL state (read once, after BOTH
        responses are known to have arrived) must equal ONE of the two
        responses' own epochs — never a value that matches neither, which
        would mean a lost/blended update slipped past the per-user
        exclusive fence (``epoch_fence_acquire_exclusive(user_fence_key(...))``,
        held for the whole of each write's transaction — see
        ``ContextService._set_with_league_locked``) that is supposed to
        fully serialize two concurrent same-user writers. That is what is
        asserted below; the deterministic ``A->B->A``/``B->A->B`` orderings
        above already cover the per-response "matches a fresh read taken
        right after it" claim exhaustively, with no second writer in flight
        to invalidate it.
        """
        a, b = self.replica_a, self.replica_b
        ca = a.login(self.username)
        cb = b.login(self.username)
        # A known starting point so both concurrent switches are moving
        # AWAY from the same baseline, not building on each other's result.
        baseline = a.switch(ca, self.program_id, self.season_c)

        results = {}
        start = threading.Barrier(2, timeout=PATIENCE)

        def go(key, replica, client, season_id):
            start.wait()
            results[key] = replica.switch(client, self.program_id, season_id)

        ta = threading.Thread(target=go, args=("a", a, ca, self.season_a))
        tb = threading.Thread(target=go, args=("b", b, cb, self.season_b))
        ta.start()
        tb.start()
        ta.join(PATIENCE)
        tb.join(PATIENCE)
        self.assertIn("a", results, "replica A's switch never returned")
        self.assertIn("b", results, "replica B's switch never returned")

        epoch_a = results["a"]["context_epoch"]
        epoch_b = results["b"]["context_epoch"]
        # Real, distinguishable progress: neither response merely echoed the
        # pre-race baseline, and the two (genuinely different Seasons) did
        # not somehow collide on one epoch.
        self.assertNotEqual(epoch_a, baseline["context_epoch"],
                            "replica A's concurrent response reused the "
                            "pre-race baseline epoch")
        self.assertNotEqual(epoch_b, baseline["context_epoch"],
                            "replica B's concurrent response reused the "
                            "pre-race baseline epoch")
        self.assertNotEqual(epoch_a, epoch_b,
                            "two different Seasons produced the same epoch")

        # THE LOAD-BEARING CHECK. Whichever committed LAST is what the row
        # (and therefore a fresh read taken now, after BOTH are known to
        # have landed) must reflect -- both cannot be "the current one", and
        # NEITHER being current would mean the two concurrent writers
        # produced a third, corrupted state the per-user fence was supposed
        # to make impossible.
        final = self._fresh_epoch(self.user_id)
        self.assertIn(
            final, (epoch_a, epoch_b),
            "the final persisted epoch matches NEITHER concurrent response "
            "-- a third, phantom state exists that no response ever named")


def _free_port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


if __name__ == "__main__":
    unittest.main()
