"""round-N+3 finding 1: the exact cross-replica RESPONSE-PAIRING regression
named by the review — distinct from, and not covered by, the write-ORDERING
proof ``test_epoch_fence_cross_replica.py`` (round-N+1 finding 3) already
gives.

THE GAP THAT TEST LEAVES OPEN, stated precisely. Every call in that file is
a SYNCHRONOUS, SEQUENTIAL HTTP request: a client waits for replica A's full
response before replica B's request is even sent. That determinism is real
and valuable for what it proves (A->B->A / B->A->B write ordering, and that
every response's epoch matches a FRESH READ TAKEN AFTER it arrives), but it
never forces a second writer's commit to land INSIDE the narrow window a
single response is built from: after replica A's own write has committed,
but before A has finished serializing and sending that response. A response
whose tuple and epoch were assembled from TWO DIFFERENT INSTANTS — one
pre-interleave, one post — would still pass every assertion in that file,
because nothing there ever samples state from inside that window. This file
builds a real, deterministic PARK/BARRIER seam that sits exactly there (see
``_cross_replica_park_server.py``'s own module docstring for precisely where
and why) and drives a genuine second-replica commit through it.

WHY THIS MATTERS, restated from the review. The response-serialization step
(``web/server.py``'s ``_with_context_epoch``) is, by direct reading, a pure
dict-attach with no re-derivation (see that method's own docstring, and its
one call site in the ``POST /api/context`` handler: ``payload, epoch =
api.set_active_context(..., include_epoch=True)`` — ONE call, whose return
value is used, verbatim, for both halves of the response). ``ApiService.
set_active_context``'s ``include_epoch=True`` path folds the write and the
epoch derivation into ONE serializable snapshot
(``ContextService.set_with_league_and_epoch``), so by construction there is
no SECOND read left for a concurrent commit to land inside. Reading the code
says there is no live bug. This file does not stop at reading the code: it
mounts the exact adversarial condition (a second replica committing a REAL
write for the SAME account in the precise post-commit/pre-response gap) and
independently proves, over real HTTP, across two real OS processes, that the
response which reaches the client is unmolested by it — never a hybrid of
one replica's tuple and the other's epoch, in EITHER direction.

WHAT EACH TEST PROVES, mapped to the review's own required shape:
  * two independent handlers/stores for one account — two real
    ``ThreadingHTTPServer`` PROCESSES (``_cross_replica_park_server.py``,
    launched via ``subprocess.Popen``, mirroring
    ``_cross_replica_server.py``'s own "run the real, unmodified app as a
    subprocess" convention), each with its OWN ``ApiService``/``SqlStore``,
    sharing one physical database.
  * a real park/barrier seam after the write commits, before the response
    is built/sent — the monkey-patched post-commit park described above;
    NOT a sleep, NOT a same-process mock.
  * replica B commits for the SAME account while A sits parked there — a
    genuine, synchronous, fully-awaited HTTP write against the OTHER
    replica, run to completion WHILE the first replica's status endpoint
    confirms it is still parked.
  * every response's tuple and epoch are asserted mutually consistent with
    EACH OTHER and with the write's OWN ground truth, captured THREE
    independent ways: (1) the Python return value the write itself
    produced, captured inside the parked process at the instant of park;
    (2) an independent fresh read of the persisted row, via a THIRD
    connection neither replica owns, taken at that SAME instant; (3) the
    actual bytes that eventually cross the wire to the test's own HTTP
    client once the park releases. All three must agree.
  * both switch orderings, including A->B->A — ``test_a_parked_b_
    interleaves_...`` and ``test_b_parked_a_interleaves_...`` cover the two
    symmetric interleavings; ``test_a_then_b_then_a_...`` additionally
    drives the literal three-leg sequence the review named by that exact
    label, with an ordinary (unparked) third leg proving the row is left
    fit for continued use, not merely "did not crash".
  * both backends — ``SqliteResponsePairingTest`` (two real OS processes
    sharing one FILE-BACKED SQLite database — SQLite supports genuine
    multi-process access via its own file-lock primitives, unlike the
    ``:memory:``/``InMemoryStore`` topologies used elsewhere in this suite)
    and ``PostgresResponsePairingTest`` (two real OS processes sharing one
    PostgreSQL database, skipped unless ``TEST_DATABASE_URL`` is set).

WHAT THIS FILE DOES **NOT** CLAIM. It is not a general concurrency-control
proof (``test_epoch_fence_cross_replica.py`` and ``test_epoch_fence.py``
already carry that weight for write ORDERING and the scoped-READ side,
respectively) and it does not exercise the in-process ``CONTEXT_GATE`` at
all — deliberately: the whole point is that two independent OS processes
share no such gate, so if response-pairing correctness depended on it this
test would be the one to show that.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from http.cookiejar import CookieJar

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Role
from hockey_scheduler.store import SqlStore

PATIENCE = 20.0
_READY_TIMEOUT = 15.0
_PARK_POLL_TIMEOUT = 10.0
_SCOPE = {}


def _pg_url():
    return os.environ.get("TEST_DATABASE_URL")


def _pg_db_url(base_url, dbname):
    head, _, _db = base_url.rpartition("/")
    return f"{head}/{dbname}"


def _free_port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _ParkableReplica:
    """One real server subprocess running ``_cross_replica_park_server.py``
    — the real app plus the test-only post-commit park seam. Exposes both
    the ordinary app HTTP client helpers (``login``/``req``) and the
    loopback park CONTROL plane (``arm``/``status``/``release``)."""

    def __init__(self, port, control_port, database_url):
        self.port = port
        self.control_port = control_port
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "_cross_replica_park_server.py")
        self.proc = subprocess.Popen(
            [sys.executable, script, "--port", str(port),
             "--control-port", str(control_port),
             "--database-url", database_url],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self._wait_ready()

    def _wait_ready(self):
        deadline = time.monotonic() + _READY_TIMEOUT
        seen = set()
        wanted = {f"READY {self.port}", f"PARK-CONTROL {self.control_port}"}
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                out = self.proc.stdout.read() if self.proc.stdout else ""
                raise RuntimeError(
                    f"park replica on port {self.port} exited early "
                    f"(code {self.proc.returncode}):\n{out}")
            line = self.proc.stdout.readline().strip()
            if line in wanted:
                seen.add(line)
            if seen == wanted:
                return
        raise RuntimeError(f"park replica on port {self.port} never became ready")

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        if self.proc.stdout:
            self.proc.stdout.close()

    # -- app HTTP -----------------------------------------------------------
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
        return self.req(client, "POST", "/api/context",
                        {"program_id": program_id, "season_id": season_id})

    # -- park control plane ---------------------------------------------------
    def _control(self, method, path, body=None):
        url = f"http://127.0.0.1:{self.control_port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=PATIENCE) as r:
            return json.loads(r.read() or b"{}")

    def arm(self, user_id):
        self._control("POST", "/arm", {"user_id": user_id})

    def status(self):
        return self._control("GET", "/status")

    def release(self):
        self._control("POST", "/release")

    def reset_park(self):
        self._control("POST", "/reset")

    def wait_parked(self, timeout=_PARK_POLL_TIMEOUT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            s = self.status()
            if s["parked"]:
                return s
            time.sleep(0.02)
        raise AssertionError(f"replica on port {self.port} never parked")


class _ResponsePairingContract:
    """Shared fixture + test bodies. A backend subclass provides
    ``_make_db_url()`` (called once, in ``setUpClass``, before either
    replica starts) and ``_drop_db()`` (``tearDownClass`` cleanup)."""

    @classmethod
    def setUpClass(cls):
        cls.db_url = cls._make_db_url()

        # SEQUENTIAL start, deliberately — mirrors `_cross_replica_server.py`
        # / `test_epoch_fence_cross_replica.py`'s own `setUpClass` ordering
        # note verbatim: each `_ParkableReplica(...)` constructor blocks on
        # readiness (which is strictly AFTER that process's own import-time
        # `DemoState.reset(seed=False)` -> `store.reset_schema()` has
        # already run) before the NEXT replica is even started, so no
        # fixture row written afterward can ever be wiped by a later boot.
        cls.port_a, cls.cport_a = _free_port(), _free_port()
        cls.port_b, cls.cport_b = _free_port(), _free_port()
        cls.replica_a = _ParkableReplica(cls.port_a, cls.cport_a, cls.db_url)
        cls.replica_b = _ParkableReplica(cls.port_b, cls.cport_b, cls.db_url)

        # A fresh, throwaway connection — never either replica's own —
        # writes the fixture only now that both replicas are past their own
        # one-time boot reset.
        bootstrap = SqlStore(cls.db_url)
        api = ApiService(bootstrap)
        ts = int(time.time())
        program = api.create_program(f"RPair {ts}", "US", "UTC")
        cls.program_id = program["id"]
        cls.season_1 = api.create_season(program["id"], "Season 1")["id"]
        cls.season_2 = api.create_season(program["id"], "Season 2")["id"]
        cls.season_3 = api.create_season(program["id"], "Season 3")["id"]
        cls.username = f"rpair_{uuid.uuid4().hex[:10]}"
        account = api.accounts.create_account(
            cls.username, "demo", Role.LEAGUE_ADMIN)
        cls.user_id = account.id
        bootstrap.close()

    @classmethod
    def tearDownClass(cls):
        cls.replica_a.close()
        cls.replica_b.close()
        cls._drop_db()

    def setUp(self):
        self.replica_a.reset_park()
        self.replica_b.reset_park()

    # -- the independent, third-connection ground truth ----------------------
    def _fresh_epoch_and_tuple(self, user_id):
        """Read the persisted ActiveContext row fresh — a NEW connection,
        never either replica's own — and independently recompute
        ``context_epoch()`` from it, mirroring
        ``test_epoch_fence_cross_replica.py``'s own ``_fresh_epoch``."""
        store = SqlStore(self.db_url)
        try:
            api = ApiService(store)
            epoch = api.context.current_epoch(user_id, Role.LEAGUE_ADMIN, _SCOPE)
            program, season, league = api.context.resolve_with_league(
                user_id, Role.LEAGUE_ADMIN, _SCOPE)
            return epoch, (program.id if program else None,
                           season.id if season else None,
                           league.id if league else None)
        finally:
            store.close()

    # -- the core adversarial drive -------------------------------------------
    def _park_then_interleave(self, parked, other, parked_season,
                              other_season, label):
        """Park ``parked`` right after ITS OWN write commits (and before its
        response is built/sent); while it sits there, run ``other``'s full
        switch for the SAME account to completion on the OTHER real
        process; capture ground truth at the parked instant BOTH from the
        write's own return value and from an independent fresh read;
        release ``parked``; assert its EVENTUAL response matches that
        ground truth exactly — never a hybrid with ``other``'s commit.

        Returns the parked replica's actual response body, for a caller
        that wants to chain a further leg onto it."""
        r_p, c_p = parked
        r_o, c_o = other

        r_p.arm(self.user_id)
        result_holder = {}

        def fire():
            result_holder["status"], result_holder["body"] = r_p.switch(
                c_p, self.program_id, parked_season)

        t = threading.Thread(target=fire)
        t.start()
        try:
            status_snapshot = r_p.wait_parked()
            captured_payload = status_snapshot["captured_payload"]
            captured_epoch = status_snapshot["captured_epoch"]
            self.assertIsNotNone(
                captured_epoch, f"{label}: park server captured no epoch "
                f"for the parked write")
            self.assertEqual(
                captured_payload.get("season_id"), parked_season,
                f"{label}: the write's OWN captured return value does not "
                f"even name its own requested Season")

            # Ground truth #2: an INDEPENDENT fresh read, taken while still
            # parked (i.e. before `other` has committed anything), via a
            # connection neither replica owns.
            fresh_epoch, fresh_tuple = self._fresh_epoch_and_tuple(self.user_id)
            self.assertEqual(
                fresh_epoch, captured_epoch,
                f"{label}: an independent fresh read taken at the parked "
                f"instant disagrees with the write's own captured return "
                f"value — the write is already internally inconsistent "
                f"before any interleaving is even introduced")
            self.assertEqual(
                fresh_tuple[1], parked_season,
                f"{label}: the persisted Season at the parked instant is "
                f"not {label}'s own requested Season")

            # NOW commit a REAL, independent write for the SAME account, on
            # the OTHER real process — landing exactly in the gap between
            # `parked`'s commit and `parked`'s response.
            status_o, body_o = r_o.switch(c_o, self.program_id, other_season)
            self.assertEqual(status_o, 200, (label, status_o, body_o))
            self.assertEqual(
                body_o.get("season_id"), other_season,
                f"{label}: the interleaved write did not land on its own "
                f"requested Season")
            self.assertNotEqual(
                body_o["context_epoch"], captured_epoch,
                f"{label}: the interleaved write produced the SAME epoch "
                f"as the parked write — the two legs would not be "
                f"distinguishable by the assertions below")
        finally:
            r_p.release()
            t.join(PATIENCE)

        self.assertFalse(t.is_alive(), f"{label}: the parked request never returned")
        self.assertIn("body", result_holder,
                      f"{label}: the parked request never returned")
        actual_status = result_holder["status"]
        actual_body = result_holder["body"]
        self.assertEqual(actual_status, 200, (label, actual_status, actual_body))

        # THE LOAD-BEARING ASSERTIONS. The bytes that actually crossed the
        # wire to `parked`'s own client must equal the ground truth captured
        # BEFORE `other` ever committed — in every field, not just the
        # epoch, so a regression that swapped either half alone is caught.
        self.assertEqual(
            actual_body.get("context_epoch"), captured_epoch,
            f"{label}: the RESPONSE that reached the client carries a "
            f"DIFFERENT epoch than the write itself already committed to "
            f"before the interleaved write landed — response tuple/epoch "
            f"pairing was corrupted by the concurrent commit")
        self.assertEqual(
            actual_body.get("program_id"), captured_payload.get("program_id"),
            f"{label}: response program_id diverged from the write's own "
            f"captured value")
        self.assertEqual(
            actual_body.get("season_id"), captured_payload.get("season_id"),
            f"{label}: response season_id diverged from the write's own "
            f"captured value")
        self.assertEqual(
            actual_body.get("league_id"), captured_payload.get("league_id"),
            f"{label}: response league_id diverged from the write's own "
            f"captured value")
        # Restated in terms a reader can check without cross-referencing the
        # capture: the response is `parked`'s OWN Season, never `other`'s.
        self.assertEqual(
            actual_body.get("season_id"), parked_season,
            f"{label}: the response adopted the INTERLEAVED write's "
            f"Season rather than its own — a client would render a "
            f"selection this replica never committed")
        self.assertNotEqual(
            actual_body.get("context_epoch"), body_o["context_epoch"],
            f"{label}: the response's epoch equals the OTHER replica's "
            f"response epoch — exactly the tuple-A/epoch-B fold the "
            f"review describes")

        # Sanity that the interleaving was REAL and not a no-op: once BOTH
        # writes are known committed, the row now reflects `other`'s (later,
        # higher-generation) write — proving `parked`'s correct response
        # above is not vacuously true because nothing actually happened.
        final_epoch, final_tuple = self._fresh_epoch_and_tuple(self.user_id)
        self.assertEqual(
            final_tuple[1], other_season,
            f"{label}: after both writes settle, the persisted Season is "
            f"not the interleaved write's — the interleaving did not "
            f"actually happen")
        self.assertEqual(final_epoch, body_o["context_epoch"])

        return actual_body

    # -- the two symmetric orderings -------------------------------------------
    def test_a_parked_b_interleaves_response_stays_paired_with_own_commit(self):
        a, b = self.replica_a, self.replica_b
        ca = a.login(self.username)
        cb = b.login(self.username)
        self._park_then_interleave(
            (a, ca), (b, cb), self.season_1, self.season_2, "A-parked/B-interleaves")

    def test_b_parked_a_interleaves_response_stays_paired_with_own_commit(self):
        a, b = self.replica_a, self.replica_b
        ca = a.login(self.username)
        cb = b.login(self.username)
        self._park_then_interleave(
            (b, cb), (a, ca), self.season_2, self.season_1, "B-parked/A-interleaves")

    # -- the literal three-leg sequence the review named -----------------------
    def test_a_then_b_then_a_full_sequence_every_leg_self_consistent(self):
        """The exact ``A->B->A`` sequence named by the review: leg 1 (A) is
        parked at the post-commit/pre-response seam, leg 2 (B) commits fully
        for the same account while leg 1 sits parked, leg 1 is released and
        checked against its own pre-interleave ground truth — then a THIRD,
        entirely ORDINARY (unparked) leg on A again, proving the row is left
        fit for continued use after the adversarial window, not merely "did
        not crash"."""
        a, b = self.replica_a, self.replica_b
        ca = a.login(self.username)
        cb = b.login(self.username)
        self._park_then_interleave(
            (a, ca), (b, cb), self.season_1, self.season_2, "A->B->A legs 1+2")

        status3, body3 = a.switch(ca, self.program_id, self.season_3)
        self.assertEqual(status3, 200, body3)
        self.assertEqual(body3.get("season_id"), self.season_3)
        fresh_epoch, fresh_tuple = self._fresh_epoch_and_tuple(self.user_id)
        self.assertEqual(
            fresh_epoch, body3["context_epoch"],
            "A->B->A leg 3 (an ordinary, unparked switch after the "
            "adversarial window) does not match a fresh independent read")
        self.assertEqual(fresh_tuple[1], self.season_3)


class SqliteResponsePairingTest(_ResponsePairingContract, unittest.TestCase):
    """Two real OS PROCESSES sharing one FILE-BACKED SQLite database.
    SQLite supports genuine multi-process access via its own file-lock
    primitives (see ``store/sql_store.py``'s ``SqlStore.transaction``
    docstring) — a real second process, not ``:memory:``/``InMemoryStore``,
    is what this file's whole claim depends on."""

    @classmethod
    def _make_db_url(cls):
        cls._tmpdir = tempfile.mkdtemp(prefix="hs_rpair_sqlite_")
        return os.path.join(cls._tmpdir, "rpair.db")

    @classmethod
    def _drop_db(cls):
        shutil.rmtree(cls._tmpdir, ignore_errors=True)


@unittest.skipUnless(_pg_url(), "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresResponsePairingTest(_ResponsePairingContract, unittest.TestCase):
    """Two real OS PROCESSES sharing one real PostgreSQL database."""

    @classmethod
    def _make_db_url(cls):
        import psycopg
        ts = int(time.time())
        cls._db_url_full = _pg_db_url(
            _pg_url(), f"hs_rpair_{ts}_{uuid.uuid4().hex[:6]}")
        admin = _pg_db_url(_pg_url(), "postgres")
        dbname = cls._db_url_full.rsplit("/", 1)[1]
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{dbname}"')
        return cls._db_url_full

    @classmethod
    def _drop_db(cls):
        import psycopg
        admin = _pg_db_url(_pg_url(), "postgres")
        dbname = cls._db_url_full.rsplit("/", 1)[1]
        try:
            with psycopg.connect(admin, autocommit=True) as conn:
                conn.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (dbname,))
                conn.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
