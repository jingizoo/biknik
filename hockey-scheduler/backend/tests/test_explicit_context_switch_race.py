"""A concurrent context switch cannot land inside a scheduler mutation — the
TWO-CONNECTION proof, PostgreSQL only (#409, evidence for #386's guarantee).

THE HAZARD THIS FILE INTERROGATES. A mutation resolves the caller's active
Program/Season/League tuple and then writes. If a concurrent
``POST /api/context`` committed a DIFFERENT tuple in between, the write would
land against a tuple that was never authorized — the same check/use class of
defect #386 closed for the scheduler draft path and #410 closes for the ice
commit. ``ApiService._locked_draft_targets`` claims that cannot happen: it
takes the per-user ActiveContext mutex (``SqlStore.active_context_mutex`` →
session-scoped ``pg_advisory_lock``, entered OUTSIDE the transaction) and then
``resolve_with_league(lock=True)`` (``_lock_active_context_mutex`` →
``pg_advisory_xact_lock`` on the SAME key), while ``set_active_context`` takes
that same key from its own transaction.

Every existing test of that claim is single-connection. This file is the
missing one: it drives the two sides on TWO REAL PostgreSQL BACKENDS and reads
the outcome out of ``pg_stat_activity`` / ``pg_locks`` from a THIRD.

WHY A SECOND STORE IS NOT OPTIONAL — and why a second HTTP request to the same
server would prove nothing. ``SqlStore`` holds exactly ONE connection, and
``SqlStore.transaction()`` holds a per-instance ``threading.RLock`` for the
whole block. Two concurrent requests to one server therefore never hold two
open transactions: the second would block on a *Python* lock, inside the same
process, having never reached PostgreSQL. Worse, since connection A is paused
*inside* ``transaction()`` it still holds that RLock, so a same-process
connection B would wait on it forever and the "race" would be a deadlock
between two threads of the test. Connection B here is a SEPARATE ``SqlStore``
with its own PostgreSQL backend, which is the only shape in which the advisory
lock — the thing actually under test — can be the mechanism that orders them.

  Consequence for reading this file: WHILE A IS PAUSED, NOTHING MAY TOUCH
  ``self.store``. A's request thread holds that store's RLock. Every
  observation taken during the pause goes through ``self.observer`` (a raw
  psycopg connection) or ``self.store_b``; a stray ``self.store.get_*`` inside
  the pause window would hang the suite rather than fail it.

HOW A IS PAUSED, AND WHY THE PAUSE POINT IS REAL. No production file is
touched. The test wraps the LIVE ``ContextService.resolve_with_league`` on the
running service instance and blocks inside the wrapper after the real call
returns — that is exactly "A has resolved its tuple and has not committed".
The pause point is not asserted by narration: while it is held, this test reads
A's own backend out of ``pg_stat_activity`` and requires
``state = 'idle in transaction'`` with a non-null ``xact_start``, i.e. a real
open transaction parked between statements, and a GRANTED advisory
``ExclusiveLock``. Sleeps are used only to bound waits; no assertion in this
file is satisfied by a sleep.

WHAT IS PROVEN HERE:

  * B genuinely BLOCKS — ``pg_stat_activity`` shows B's backend ``active`` with
    ``wait_event_type = 'Lock'``, ``wait_event = 'advisory'``, running
    ``SELECT pg_advisory_xact_lock(...)``, and ``pg_locks`` shows B's request
    ``granted = false`` on the SAME ``(classid, objid)`` A holds granted. That
    is a lock wait, not slowness;
  * the interleaving the hazard needs is therefore UNREACHABLE: B's switch
    commits strictly AFTER A's mutation completes, on every run;
  * the block is USER-KEYED, not incidental serialization of two connections:
    the identical interleaving with B switching a DIFFERENT user's context
    completes immediately while A is still paused (``..._does_not_block``).

WHAT THIS FILE DOES **NOT** PROVE, stated plainly rather than implied:

  1. WHICH of the two advisory locks does the blocking is UNFALSIFIABLE from
     outside. ``pg_advisory_lock`` (session, taken by ``active_context_mutex``)
     and ``pg_advisory_xact_lock`` (transaction, taken by
     ``_lock_active_context_mutex``) use the SAME key, and PostgreSQL collapses
     both into ONE ``pg_locks`` row with no column distinguishing them —
     measured by ``test_pg_locks_cannot_distinguish_...``, not assumed. The
     limitation was then confirmed by experiment rather than left as a caveat:
     with ``SqlStore.active_context_mutex`` replaced by a no-op at runtime and
     the transaction-scoped lock left in place, **all eight tests in this file
     still pass**. So every blocking result here attributes the ordering to the
     advisory mutex AS A PAIR, and this file would not notice the session half
     being deleted. Separating them needs a production edit, which this branch
     forbids. Named here so the guard is never read as stronger than it is —
     the same shape of unfalsifiability this repo has recorded before.

     What the same experiment DOES establish, on the other side: with BOTH
     locks neutered, connection B's switch commits at ~6 ms while A is still
     parked, A resumes and discards the six drafts of the tuple it resolved
     BEFORE the switch, and the saved row by then names a different Program.
     The interleaving is real and reachable; the mutex is what removes it. So
     the passing assertions below are measuring a guard, not an impossibility.
  2. B's side does not traverse the HTTP layer. ``POST /api/context`` is a thin
     wrapper — authenticate, ``check_body``, then
     ``ApiService.set_active_context`` — and B calls that same method on its own
     store, because the wrapper cannot be reached on a second connection without
     a second server process. ``test_the_http_route_and_connection_b_write_the
     _same_row`` pins that equivalence on real HTTP rather than asserting it in
     prose.
  3. Nothing here says the resolved tuple was CHOSEN. Ordering is all the mutex
     provides: in ``..._with_no_saved_selection`` A still mutates the FALLBACK
     Program, and B's first-ever selection merely lands afterwards. That
     un-chosen mutation is #409's defect and is owned by the RED oracle in
     ``test_explicit_mutation_context.py``; this file deliberately does not
     re-assert it, and its race cases accept EITHER outcome for A's response so
     they stay green on both sides of that fix — the observed response is
     recorded in ``OBSERVATIONS`` and printed on every run instead.

A SKIP IS NOT A PASS. Without ``TEST_DATABASE_URL`` (or without ``psycopg``)
nothing in this file is exercised at all: there is no second connection, no
advisory lock and no race. The class skips cleanly with a reason that says so,
and importing the module prints a banner to stderr, so a green run with no
PostgreSQL can never be read as evidence for any claim above.
"""

import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND, commit_fresh_draft  # noqa: F401  (sets sys.path)

import hockey_scheduler.web.server as srv
from hockey_scheduler.api.service import ApiService
from hockey_scheduler.domain import Role
from hockey_scheduler.store.sql_store import SqlStore

try:                                    # the observer connection's driver
    import psycopg
except ImportError:                     # pragma: no cover - env without psycopg
    psycopg = None

PG_URL = os.environ.get("TEST_DATABASE_URL")

SKIP_REASON = (
    "PostgreSQL not configured (TEST_DATABASE_URL) or psycopg missing — the "
    "two-connection context-switch race was NOT exercised. A SKIP HERE IS NOT "
    "A PASS: nothing in this module has been proven.")

if not PG_URL or psycopg is None:       # pragma: no cover - visibility only
    print("\n[test_explicit_context_switch_race] " + SKIP_REASON + "\n",
          file=sys.stderr)

PASSWORD = "demo"
OPERATOR, OPERATOR_ID = "raceop", "user_raceop"
BYSTANDER, BYSTANDER_ID = "racebystander", "user_racebystander"

ICE_BASE = datetime(2026, 9, 7, 18, tzinfo=timezone.utc)

# Four teams => C(4,2) = 6 single round-robin fixtures. Six is distinguishable
# from zero and from a partial batch, so "A acted on its own tuple" can never
# pass because there was nothing to act on.
TEAMS = 4
EXPECTED_DRAFTS = 6

# How long B is given to prove it is stuck. Long enough that a merely slow
# commit would have finished; the assertion that matters is the pg_locks
# `granted = false` row, not this number.
BLOCK_OBSERVATION_SECONDS = 3.0
# How long B must STAY blocked after it is first seen ungranted, so a single
# sample cannot be mistaken for a lasting exclusion.
DWELL_SECONDS = 0.5
# Bounded waits, so a seam that stopped firing FAILS the suite instead of
# hanging it.
PAUSE_TIMEOUT = 30.0

# Verbatim, timestamped record of what each run actually saw. Printed per test
# so a PostgreSQL run leaves the evidence in the log even when it is green.
OBSERVATIONS = []


def _ok(result, label=""):
    assert isinstance(result, dict) and "error" not in result, (label, result)
    return result


@unittest.skipUnless(PG_URL and psycopg is not None, SKIP_REASON)
class PostgresContextSwitchRaceTest(unittest.TestCase):
    """Two real PostgreSQL backends, one paused mid-mutation."""

    # -- harness -----------------------------------------------------------
    def setUp(self):
        self._prev_db = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = PG_URL
        # Registered before anything that can fail: DATABASE_URL and STATE are
        # process-global, so a setUp raising past this point would leave every
        # later module in this worker pointed at this test's store.
        self.addCleanup(self._restore_environment)

        srv.STATE.reset(seed=False)     # clean slate; see _build_victim_program
        srv.RATE_LIMITER.reset()
        srv.LOGIN_THROTTLE.reset()
        self.api = srv.STATE.api
        self.store = self.api.store
        self.assertEqual(
            self.store.backend, "postgres",
            "this file is PostgreSQL-only: advisory locks are the mechanism "
            "under test and no other backend has them")

        self.timeline = []
        self._t0 = time.monotonic()
        self.resolved = threading.Event()   # A has resolved and is parked
        self.release = threading.Event()    # let A proceed
        self.armed = threading.Event()      # pause the NEXT locked resolve
        # Always let A go, even on failure: a request thread parked inside
        # `transaction()` holds the store's RLock and would wedge tearDown.
        self.addCleanup(self.release.set)

        self._build_victim_program()
        self._build_switch_target()
        self._create_accounts()
        self._install_pause_seam()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

        # Connection B: a SECOND SqlStore, hence a second PostgreSQL backend.
        self.store_b = SqlStore(PG_URL)
        self.addCleanup(self.store_b.close)
        self.api_b = ApiService(self.store_b)

        # The observer: a THIRD connection, autocommit, read-only by use. It is
        # the only thing that may run while A is paused.
        self.observer = psycopg.connect(PG_URL, autocommit=True)
        self.addCleanup(self.observer.close)

        # The two backends under test, named once so every later observation is
        # about THESE connections. Taken before any pause, while touching
        # `self.store` is still safe.
        self.a_pid = self._backend_pid(self.store)
        self.b_pid = self._backend_pid(self.store_b)
        self.assertNotEqual(
            self.a_pid, self.b_pid,
            "connection A and connection B share one PostgreSQL backend, so "
            "there is no concurrency here to observe")
        self.pids = (self.a_pid, self.b_pid)

        self.addCleanup(self._report_observations)

    def tearDown(self):
        self.release.set()
        self.httpd.shutdown()
        self.thread.join(timeout=10)
        self.httpd.server_close()

    def _restore_environment(self):
        self.api.context.resolve_with_league = self._real_resolve
        if self._prev_db is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self._prev_db
        try:
            srv.STATE.reset(seed=False)
        except Exception:
            pass

    def _report_observations(self):
        for entry in self.timeline:
            OBSERVATIONS.append(entry)
        print("\n--- %s: observed timeline ---" % self.id(), file=sys.stderr)
        for at, line in self.timeline:
            print("  %7.3fs  %s" % (at, line), file=sys.stderr)

    def _stamp(self, line):
        self.timeline.append((time.monotonic() - self._t0, line))

    # -- fixture -----------------------------------------------------------
    def _build_victim_program(self):
        """One schedulable Program holding six COMMITTED draft games.

        Committed rather than merely proposed: a committed draft has already
        flipped its ice slot to ALLOCATED (#277), so "A acted" and "A did not
        act" are both observable in persisted state, not only in a count.

        Built through ``STATE.api`` directly (``role is None``, the ungated
        internal path) so the Program belongs to no account's selection
        history — an HTTP-built one would have been created by some logged-in
        operator and muddy which selections exist.
        """
        api = self.api
        program = _ok(api.create_program("Race Victim Program"), "program")
        club = _ok(api.create_club("Race Club"), "club")
        # `Venue.league_id` is LEGACY vocabulary holding a PROGRAM id, not a
        # competition League (integrity_checks joins seasons.program_id =
        # venues.league_id).
        venue = _ok(api.create_venue("Race Venue", league_id=program["id"]),
                    "venue")
        rink = _ok(api.create_rink(venue["id"], "Race Rink"), "rink")
        season = _ok(api.create_season(program["id"], "Race Season",
                                       "2026-09-01", "2027-03-31"), "season")
        _ok(api.grant_season_venue_access(season["id"], venue["id"]), "grant")
        league = _ok(api.create_league(season["id"], "Race League"), "league")
        division = _ok(api.create_division(season["id"], "Race Division",
                                           league_id=league["id"]), "division")
        for index in range(TEAMS):
            team = _ok(api.create_team(club["id"], None, f"Race Team {index}",
                                       program_id=program["id"],
                                       league_id=league["id"]), "team")
            _ok(api.register_team_for_season(season["id"], team["id"],
                                             division["id"],
                                             league_id=league["id"]),
                "registration")
        for index in range(EXPECTED_DRAFTS + 2):
            start = ICE_BASE + timedelta(days=index)
            _ok(api.create_ice_slot(rink["id"], start.isoformat(),
                                    (start + timedelta(hours=2)).isoformat()),
                "slot")
        _ok(commit_fresh_draft(self.api, division_id=division["id"]), "commit")
        self.victim = {"program": program["id"], "season": season["id"],
                       "league": league["id"]}
        # Anti-vacuity: the drafts every case measures must exist before any
        # case can pass by finding nothing.
        self.assertEqual(
            len([g for g in self.store.all_games() if g.is_draft]),
            EXPECTED_DRAFTS,
            "the fixture did not commit the expected draft schedule, so every "
            "assertion below would be vacuous")

    def _build_switch_target(self):
        """The OTHER tuple B switches to.

        Deliberately empty — it owns no drafts and no ice. If A ever resolved
        it, A would act on zero games, so "A acted on six" is by itself proof
        that A used its own pre-switch tuple and not B's.
        """
        program = _ok(self.api.create_program("Race Other Program"), "other")
        season = _ok(self.api.create_season(program["id"], "Race Other Season",
                                            "2026-09-01", "2027-03-31"),
                     "other season")
        self.other = {"program": program["id"], "season": season["id"]}

    def _create_accounts(self):
        """Two real LEAGUE_ADMIN accounts — a global role holding
        MANAGE_SCHEDULE, so neither is ever refused for lack of entitlement.
        The bystander exists only for the different-user control."""
        for username, account_id in ((OPERATOR, OPERATOR_ID),
                                     (BYSTANDER, BYSTANDER_ID)):
            self.api.accounts.create_account(
                username, PASSWORD, Role.LEAGUE_ADMIN, scope={},
                actor_id="test_seed", account_id=account_id)

    # -- the pause seam ----------------------------------------------------
    def _install_pause_seam(self):
        """Park A after it has resolved its tuple and before it commits.

        Wraps the LIVE ``resolve_with_league`` on the running service; no file
        under ``hockey_scheduler/`` is modified. Only a ``lock=True`` call is
        parked — that is the authorization inside
        ``_locked_draft_targets``, taken inside the mutation's transaction —
        and only once per arming, so the read-only resolves the same request
        makes elsewhere run untouched.
        """
        self._real_resolve = self.api.context.resolve_with_league
        real = self._real_resolve

        def paused_resolve(user_id, role, scope, lock=False):
            resolved = real(user_id, role, scope, lock=lock)
            if lock and self.armed.is_set():
                self.armed.clear()
                program, season, _league = resolved
                self._stamp(
                    "A resolved (lock=True) program=%s season=%s and PARKED "
                    "inside its open transaction"
                    % (getattr(program, "id", None), getattr(season, "id", None)))
                self.resolved.set()
                self.release.wait(PAUSE_TIMEOUT)
                self._stamp("A released")
            return resolved

        self.api.context.resolve_with_league = paused_resolve

    # -- HTTP --------------------------------------------------------------
    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _req(self, client, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with client.open(request) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read() or b"{}")

    def _login(self, username):
        client = self._client()
        status, body = self._req(client, "POST", "/api/auth/login",
                                 {"username": username, "password": PASSWORD})
        self.assertEqual(status, 200, body)
        return client

    def _select(self, client, program_id, season_id, league_id=None):
        status, body = self._req(client, "POST", "/api/context", {
            "program_id": program_id, "season_id": season_id,
            "league_id": league_id})
        self.assertEqual(status, 200, body)
        return body

    # -- observation (runs on the THIRD connection only) -------------------
    @staticmethod
    def _text(value):
        """``name``/``text`` columns come back as ``bytes`` on a SQL_ASCII
        cluster and as ``str`` on a UTF-8 one. The observations below compare
        catalog values against literals, so normalise instead of assuming the
        encoding of whatever database ``TEST_DATABASE_URL`` points at (#405 is
        the reason SQL_ASCII is a shape this repo actually meets)."""
        return value.decode("utf-8", "replace") if isinstance(value, bytes) \
            else value

    def _rows(self, sql, params=None):
        return [tuple(self._text(value) for value in row)
                for row in self.observer.execute(sql, params).fetchall()]

    def _activity(self, pids=None):
        """``pg_stat_activity`` for the two backends under test.

        Pinned to the exact pids of connection A and connection B rather than
        to ``datname``. The parallel runner already gives each worker its own
        database, but pinning makes every observation below a statement about
        THESE two connections instead of about whatever else happens to be
        attached — and it is what lets the assertions say "A" and "B" and mean
        it.
        """
        return self._rows(
            "SELECT pid, state, wait_event_type, wait_event, "
            "       left(query, 80), xact_start IS NOT NULL "
            "FROM pg_stat_activity WHERE pid = ANY(%s)",
            (list(pids if pids is not None else self.pids),))

    def _advisory_locks(self, pids=None):
        return self._rows(
            "SELECT pid, classid, objid, mode, granted FROM pg_locks "
            "WHERE locktype = 'advisory' AND pid = ANY(%s)",
            (list(pids if pids is not None else self.pids),))

    @staticmethod
    def _backend_pid(store):
        """The PostgreSQL backend pid behind ``store``'s single connection.

        Run inside ``store.transaction()`` so the probe commits: psycopg opens
        a transaction on first execute, and leaving one open would park this
        very connection in ``idle in transaction`` — the exact state the pause
        assertion looks for.
        """
        with store.transaction():
            row = store.conn.execute(
                "SELECT pg_backend_pid() AS pid").fetchone()
        # The store configures a dict row factory; do not assume either shape.
        return row["pid"] if isinstance(row, dict) else row[0]

    def _assert_a_is_parked_in_a_real_transaction(self):
        """A is inside an OPEN transaction holding a GRANTED advisory lock.

        This is what makes the pause a real pause rather than a narrated one:
        an ``idle in transaction`` backend with a non-null ``xact_start`` has
        begun and not ended a transaction and is parked between statements
        waiting on its client — which is precisely the wrapper this test
        installed.
        """
        parked = [row for row in self._activity() if row[0] == self.a_pid]
        self.assertEqual(
            [row[1] for row in parked], ["idle in transaction"],
            "connection A is not parked inside an open transaction, so the "
            "pause point is not what this file claims: %r" % (parked,))
        self.assertTrue(
            parked[0][5],
            "A's backend reports no transaction start, so it is not actually "
            "inside a transaction: %r" % (parked[0],))
        held = [row for row in self._advisory_locks([self.a_pid]) if row[4]]
        self.assertEqual(
            len(held), 1,
            "A does not hold exactly one granted advisory lock while parked: "
            "%r" % (self._advisory_locks([self.a_pid]),))
        self._stamp("A verified parked: pg_stat_activity=%r advisory=%r"
                    % (parked[0], held[0]))
        return self.a_pid, held[0]

    def _wait_for_blocked_waiter(self, holder_lock, seconds):
        """Return B's ``pg_locks`` row once B is WAITING on ``holder_lock``.

        Polls for a STATE rather than sleeping a fixed duration, so a passing
        observation is "B's lock request is recorded ungranted", never "B had
        not finished yet". ``None`` if B never blocks (the control).
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            waiting = [row for row in self._advisory_locks([self.b_pid])
                       if not row[4]
                       and (row[1], row[2]) == (holder_lock[1], holder_lock[2])]
            if waiting:
                return waiting[0]
            time.sleep(0.05)
        return None

    # -- the shared race body ----------------------------------------------
    def _run_switch_race(self, client, verb, switch_user_id):
        """Park A mid-mutation, run B's switch on the second connection, and
        record exactly what each side did and when."""
        path = f"/api/scheduler/drafts/{verb}"
        a_result, b_result = {}, {}

        def connection_a():
            self._stamp("A: POST %s {\"all\": true}" % path)
            a_result["response"] = self._req(client, "POST", path,
                                             {"all": True})
            a_result["finished_at"] = time.monotonic()
            self._stamp("A: response %r" % (a_result["response"],))

        def connection_b():
            self._stamp("B: set_active_context -> program=%s season=%s "
                        "(second connection, user=%s)"
                        % (self.other["program"], self.other["season"],
                           switch_user_id))
            try:
                b_result["response"] = self.api_b.set_active_context(
                    switch_user_id, Role.LEAGUE_ADMIN, {},
                    self.other["program"], self.other["season"])
            except Exception as exc:                      # noqa: BLE001
                b_result["response"] = "EXCEPTION %s: %s" % (
                    type(exc).__name__, exc)
            b_result["finished_at"] = time.monotonic()
            self._stamp("B: returned %s"
                        % ("ok" if isinstance(b_result["response"], dict)
                           and "error" not in b_result["response"]
                           else repr(b_result["response"])))

        self.armed.set()
        thread_a = threading.Thread(target=connection_a, daemon=True)
        thread_a.start()
        self.assertTrue(
            self.resolved.wait(PAUSE_TIMEOUT),
            "connection A never reached the pause seam — the mutation no "
            "longer resolves its tuple through resolve_with_league(lock=True), "
            "so this file is testing nothing and must be repaired, not skipped")

        a_pid, a_lock = self._assert_a_is_parked_in_a_real_transaction()

        thread_b = threading.Thread(target=connection_b, daemon=True)
        thread_b.start()
        waiter = self._wait_for_blocked_waiter(a_lock,
                                               BLOCK_OBSERVATION_SECONDS)
        still_waiting = None
        if waiter is not None:
            # DWELL. A single ungranted sample could be the instant before the
            # lock is handed over; re-reading after a dwell makes the claim
            # "B stays blocked for as long as A holds it", which is the
            # property the hazard actually turns on.
            time.sleep(DWELL_SECONDS)
            still_waiting = self._wait_for_blocked_waiter(a_lock, 0.5)
        b_alive_during_pause = thread_b.is_alive()
        activity_during_pause = self._activity()
        # The whole advisory picture for BOTH backends at one instant. The
        # waiter row alone is filtered to B's pid and so cannot say who holds
        # the lock; this can.
        locks_during_pause = self._advisory_locks()
        self._stamp("during pause: waiter=%r still_waiting_after_%.1fs=%r "
                    "b_thread_alive=%s activity=%r"
                    % (waiter, DWELL_SECONDS, still_waiting,
                       b_alive_during_pause, activity_during_pause))

        self.release.set()
        thread_a.join(PAUSE_TIMEOUT)
        thread_b.join(PAUSE_TIMEOUT)
        self.assertFalse(thread_a.is_alive(), "connection A never finished")
        self.assertFalse(thread_b.is_alive(), "connection B never finished")
        return {"a": a_result, "b": b_result, "a_pid": a_pid,
                "a_lock": a_lock, "waiter": waiter,
                "still_waiting": still_waiting,
                "b_alive_during_pause": b_alive_during_pause,
                "activity_during_pause": activity_during_pause,
                "locks_during_pause": locks_during_pause}

    def _assert_b_was_blocked_and_ordered_after_a(self, race):
        """The whole point: B could not commit inside A's window."""
        self.assertIsNotNone(
            race["waiter"],
            "connection B never blocked on the advisory lock A holds, so a "
            "concurrent context switch CAN land inside a scheduler mutation. "
            "pg_stat_activity during the pause: %r"
            % (race["activity_during_pause"],))
        # Two DISTINCT backends on one advisory key at one instant: A holding
        # it granted, B queued behind it. `waiter` is filtered to B's pid and
        # so cannot establish who the holder is — this is the assertion that
        # does, and it is the reason "two real connections" is a fact here
        # rather than a claim.
        on_the_key = sorted(
            (row[0], row[4]) for row in race["locks_during_pause"]
            if (row[1], row[2]) == (race["a_lock"][1], race["a_lock"][2]))
        self.assertEqual(
            on_the_key, sorted([(self.a_pid, True), (self.b_pid, False)]),
            "the advisory key is not held granted by A with B queued behind "
            "it: %r" % (race["locks_during_pause"],))
        self.assertIsNotNone(
            race["still_waiting"],
            "B's lock request stopped being ungranted while A was still "
            "parked, so B was momentarily queued rather than held out for the "
            "whole mutation window")
        self.assertEqual(
            (race["waiter"][1], race["waiter"][2]),
            (race["a_lock"][1], race["a_lock"][2]),
            "B is waiting on a DIFFERENT advisory key than the one A holds, so "
            "the mutex is not what ordered them")
        self.assertFalse(
            race["waiter"][4],
            "B's advisory lock request is reported as granted while A holds "
            "it — the two are not mutually exclusive")
        self.assertTrue(
            race["b_alive_during_pause"],
            "connection B completed while A was still parked: the switch "
            "landed INSIDE the mutation's window")
        waiting = [row for row in race["activity_during_pause"]
                   if row[0] == race["waiter"][0]]
        self.assertEqual(
            [(row[2], row[3]) for row in waiting], [("Lock", "advisory")],
            "B is not reported as waiting on an advisory lock, so its delay is "
            "not provably a lock wait: %r" % (waiting,))
        self.assertLessEqual(
            race["a"]["finished_at"], race["b"]["finished_at"],
            "B's context switch committed BEFORE A's mutation finished")
        self.assertIsInstance(
            race["b"]["response"], dict,
            "B's switch did not complete normally after A released: %r"
            % (race["b"]["response"],))
        self.assertNotIn("error", race["b"]["response"],
                         race["b"]["response"])

    # -- case 1: an EXPLICIT pre-switch selection ---------------------------
    def test_a_context_switch_cannot_land_inside_a_discard(self):
        """A explicitly selected the victim tuple; B switches away mid-flight.

        The fix-stable core of this file. A is entitled and explicitly
        selected, so its ``200 {"discarded": 6}`` is today's truth and stays
        true after #409's fix lands — nothing here has to be revisited then.
        """
        client = self._login(OPERATOR)
        self._select(client, self.victim["program"], self.victim["season"],
                     self.victim["league"])

        race = self._run_switch_race(client, "discard", OPERATOR_ID)
        self._assert_b_was_blocked_and_ordered_after_a(race)

        status, body = race["a"]["response"]
        self.assertEqual(
            (status, body), (200, {"discarded": EXPECTED_DRAFTS}),
            "A's discard did not act on its own PRE-SWITCH tuple: %r"
            % (race["a"]["response"],))
        # Persisted state, not the count A reported back. The switch target
        # owns no drafts, so six discarded can only be the pre-switch tuple's.
        self.assertEqual([g for g in self.store.all_games() if g.is_draft], [])
        self.assertEqual(
            len([s for s in self.store.all_ice_slots()
                 if s.status.value == "allocated"]), 0,
            "a discarded draft must release its ice back to AVAILABLE")
        saved = self.store.get_active_context(OPERATOR_ID)
        self.assertEqual(
            (saved.program_id, saved.season_id),
            (self.other["program"], self.other["season"]),
            "B's switch did not end up persisted, so the race never happened")

    def test_a_context_switch_cannot_land_inside_a_publish(self):
        """The same proof on the other mutation route."""
        client = self._login(OPERATOR)
        self._select(client, self.victim["program"], self.victim["season"],
                     self.victim["league"])

        race = self._run_switch_race(client, "publish", OPERATOR_ID)
        self._assert_b_was_blocked_and_ordered_after_a(race)

        status, body = race["a"]["response"]
        self.assertEqual(
            (status, body), (200, {"published": EXPECTED_DRAFTS}),
            "A's publish did not act on its own PRE-SWITCH tuple: %r"
            % (race["a"]["response"],))
        self.assertEqual([g for g in self.store.all_games() if g.is_draft], [])
        self.assertEqual(
            len([g for g in self.store.all_games() if g.published]),
            EXPECTED_DRAFTS)
        self.assertEqual(
            len([s for s in self.store.all_ice_slots()
                 if s.status.value == "allocated"]), EXPECTED_DRAFTS,
            "published games must keep their ice ALLOCATED")

    # -- case 2: no saved selection at all (#409's fallback) ---------------
    def test_a_first_ever_selection_cannot_land_inside_a_fallback_mutation(self):
        """The case ``_lock_active_context_mutex`` was written for.

        Its docstring names exactly this: an operator with NO saved row
        authorizes from fallback tuple A while making their first selection B
        concurrently. A row lock cannot order that, because an absent-row read
        is not a lock; the advisory lock can, because it exists before the row
        does. Here that claim meets two real backends.

        A's RESPONSE is deliberately not pinned. Today the un-chosen mutation
        succeeds (#409's defect, owned by the RED oracle in
        ``test_explicit_mutation_context.py``); once that fix lands it will be
        refused. Either way the ORDERING property asserted here is the same,
        and the response actually observed is recorded in the timeline rather
        than frozen into an assertion this file would have to fight.
        """
        client = self._login(OPERATOR)
        self.assertIsNone(
            self.store.get_active_context(OPERATOR_ID),
            "this account was supposed to have no saved context row")

        race = self._run_switch_race(client, "discard", OPERATOR_ID)
        self._assert_b_was_blocked_and_ordered_after_a(race)

        status, body = race["a"]["response"]
        self._stamp("RECORDED (no saved selection): A answered %s %r"
                    % (status, body))
        # Whatever A did, it can only have been against the tuple it resolved
        # BEFORE the switch. B's target owns no drafts, so acting on it is
        # indistinguishable from acting on nothing — assert on the games.
        remaining = [g for g in self.store.all_games() if g.is_draft]
        if status == 200:
            self.assertEqual(
                body, {"discarded": EXPECTED_DRAFTS},
                "A acted on a partial or foreign batch: %r" % (body,))
            self.assertEqual(remaining, [],
                             "A reported six discards but drafts survived")
        else:
            self.assertEqual(
                len(remaining), EXPECTED_DRAFTS,
                "A refused but still destroyed drafts: %r" % (body,))
        saved = self.store.get_active_context(OPERATOR_ID)
        self.assertEqual(
            (saved.program_id, saved.season_id),
            (self.other["program"], self.other["season"]),
            "B's first-ever selection is not the persisted one")

    # -- the discriminating control: a DIFFERENT user does not block -------
    def test_a_different_users_context_switch_does_not_block(self):
        """Proves the block above is the per-user mutex, not incidental
        serialization of two connections against one database.

        Same interleaving, same pause, same second connection — only the user
        whose context is switched differs. If this ALSO blocked, the earlier
        assertions would be measuring something global (a table lock, the
        SERIALIZABLE snapshot, the fixture) rather than the keyed mutex, and
        nothing about context switching would have been shown.
        """
        client = self._login(OPERATOR)
        self._select(client, self.victim["program"], self.victim["season"],
                     self.victim["league"])

        race = self._run_switch_race(client, "discard", BYSTANDER_ID)
        self.assertIsNone(
            race["waiter"],
            "a DIFFERENT user's context switch blocked on A's advisory lock, "
            "so the mutex is not keyed per user: %r" % (race["waiter"],))
        self.assertFalse(
            race["b_alive_during_pause"],
            "the bystander's switch did not complete while A was parked, so "
            "the earlier blocking result is not attributable to the key")
        self.assertIsInstance(race["b"]["response"], dict)
        self.assertNotIn("error", race["b"]["response"], race["b"]["response"])
        self.assertLess(
            race["b"]["finished_at"], race["a"]["finished_at"],
            "the bystander's switch did not commit inside A's window")
        saved = self.store.get_active_context(BYSTANDER_ID)
        self.assertEqual((saved.program_id, saved.season_id),
                         (self.other["program"], self.other["season"]))
        # A still did its own work, unaffected.
        self.assertEqual(race["a"]["response"],
                         (200, {"discarded": EXPECTED_DRAFTS}))

    # -- positive control: the same mutation with NO concurrent switch -----
    def test_positive_control_the_same_discard_succeeds_unraced(self):
        """The same account, selection, route and payload with nothing
        concurrent. Without this, a refusal or an empty batch in the cases
        above could be explained by the fixture, the role or the route rather
        than by the race."""
        client = self._login(OPERATOR)
        self._select(client, self.victim["program"], self.victim["season"],
                     self.victim["league"])
        status, body = self._req(client, "POST",
                                 "/api/scheduler/drafts/discard",
                                 {"all": True})
        self._stamp("positive control (no concurrency): %s %r" % (status, body))
        self.assertEqual((status, body), (200, {"discarded": EXPECTED_DRAFTS}))
        self.assertEqual([g for g in self.store.all_games() if g.is_draft], [])
        self.assertEqual(
            len([s for s in self.store.all_ice_slots()
                 if s.status.value == "allocated"]), 0)

    def test_positive_control_the_pause_seam_is_inert_when_unarmed(self):
        """The seam only pauses when armed.

        Otherwise every ordering result above could be an artefact of the
        wrapper itself rather than of the code under test."""
        client = self._login(OPERATOR)
        self._select(client, self.victim["program"], self.victim["season"],
                     self.victim["league"])
        self.assertFalse(self.armed.is_set())
        started = time.monotonic()
        status, body = self._req(client, "POST",
                                 "/api/scheduler/drafts/publish",
                                 {"all": True})
        elapsed = time.monotonic() - started
        self._stamp("unarmed publish took %.3fs -> %s %r"
                    % (elapsed, status, body))
        self.assertEqual((status, body), (200, {"published": EXPECTED_DRAFTS}))
        self.assertFalse(self.resolved.is_set(),
                         "the seam parked a request it was never armed for")
        self.assertLess(elapsed, PAUSE_TIMEOUT / 2,
                        "an unarmed request was delayed by the seam")

    # -- the one hop connection B does not traverse ------------------------
    def test_the_http_route_and_connection_b_write_the_same_row(self):
        """``POST /api/context`` and B's direct call persist the SAME row.

        B cannot reach the HTTP layer without a second server process, so this
        pins the equivalence the race cases rely on instead of asserting it in
        the docstring: the route is authenticate + ``check_body`` +
        ``set_active_context``, and it is the last of those that both sides run.
        """
        client = self._login(OPERATOR)
        self._select(client, self.other["program"], self.other["season"])
        via_http = self.store.get_active_context(OPERATOR_ID)
        self.assertEqual((via_http.program_id, via_http.season_id,
                          via_http.league_id),
                         (self.other["program"], self.other["season"], None))

        # Same arguments, second connection, no HTTP.
        self.api_b.set_active_context(BYSTANDER_ID, Role.LEAGUE_ADMIN, {},
                                      self.other["program"],
                                      self.other["season"])
        via_store_b = self.store.get_active_context(BYSTANDER_ID)
        self.assertEqual(
            (via_store_b.program_id, via_store_b.season_id,
             via_store_b.league_id),
            (via_http.program_id, via_http.season_id, via_http.league_id),
            "the direct call connection B makes does not persist what the "
            "HTTP route persists, so the race cases model the wrong write")

    # -- the recorded unfalsifiable part -----------------------------------
    def test_pg_locks_cannot_distinguish_the_session_mutex_from_the_xact_lock(self):
        """UNFALSIFIABLE, MEASURED — not assumed, and not a guarantee.

        ``active_context_mutex`` (session-scoped ``pg_advisory_lock``, taken
        outside the transaction) and ``_lock_active_context_mutex``
        (``pg_advisory_xact_lock``, taken inside it) use the SAME key. This
        drives both on one connection and shows ``pg_locks`` reports ONE row
        either way, with no column separating them. So every "B blocked on the
        advisory lock" result in this file attributes the block to the pair,
        never to one of them.

        "These tests could not tell" is a measurement, not a worry: re-running
        this whole module with ``SqlStore.active_context_mutex`` monkeypatched
        to a bare ``yield`` — the session half gone, the transaction-scoped
        half intact — produced ``failures=0 errors=0``. B still blocked, on
        ``pg_advisory_xact_lock``, for the same dwell. Naming that here keeps
        the guard from being read as stronger evidence than it is, and tells
        whoever edits ``active_context_mutex`` next that this file will NOT
        catch them.
        """
        key = (SqlStore._ACTIVE_CONTEXT_LOCK_NAMESPACE, "probe_user_409")
        probe = psycopg.connect(PG_URL, autocommit=True)
        try:
            probe_pid = probe.execute("SELECT pg_backend_pid()").fetchone()[0]
            probe.execute("SELECT pg_advisory_lock(%s, hashtext(%s))", key)
            session_only = self._advisory_locks([probe_pid])
            probe.execute("BEGIN")
            probe.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", key)
            both_held = self._advisory_locks([probe_pid])
            probe.execute("COMMIT")
            probe.execute("SELECT pg_advisory_unlock(%s, hashtext(%s))", key)
        finally:
            probe.close()
        self._stamp("session-only pg_locks=%r; session+xact pg_locks=%r"
                    % (session_only, both_held))
        self.assertEqual(len(session_only), 1, session_only)
        self.assertEqual(
            both_held, session_only,
            "pg_locks DOES distinguish the two advisory locks — if this ever "
            "fails, the unfalsifiability recorded throughout this file no "
            "longer applies and the blocking assertions can be sharpened to "
            "name which mechanism ordered the two connections")


if __name__ == "__main__":
    unittest.main()
