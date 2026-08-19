"""#424 round-N+1 owner review finding B: the atomicity/concurrency proof for
``list_players(include_identity=True)``, ``player_duplicate_report``, and
``evaluate_player_eligibility`` was incomplete -- round-N's own
``test_sensitive_read_audit_atomicity.py`` mostly asserted ``_txn_depth``
(a Python-level nesting counter), checked PostgreSQL transaction identity
only for ``evaluate_player_eligibility``, forced audit-write failure only
for that one call site on SQLite, and had no deterministic read-vs-
concurrent-update test or two-connection PostgreSQL barrier for any of the
three. This module closes both gaps for ALL THREE call sites:

1. FORCED AUDIT-WRITE FAILURE (``_FailureInjectionMatrixTests``): a real
   injected failure in ``store.add_data_access`` -- the write
   ``_record_sensitive_read`` makes for an ALLOWED disclosure -- on Memory,
   SQLite, AND PostgreSQL. Asserts the exception propagates UNCAUGHT (never
   swallowed into a partial response: ``@catch`` only catches
   ``DomainError``, and a raw ``RuntimeError`` is neither), that
   ``list_data_access()`` is empty afterward (no half-committed audit row
   survives the exception unwinding the whole ``with self.store.
   transaction():`` block), and — the property finding B names directly —
   that no caller-visible RETURN VALUE was ever produced: the facade call
   raises, it does not return a dict with the sensitive payload attached.

2. SNAPSHOT CONSISTENCY UNDER CONTENTION: for each call site, a concurrent
   write to the SAME player races the target's read, deterministically (a
   real two-thread/two-connection barrier, never sleep-based timing). Every
   one of these three facade methods opens a WRITE-CAPABLE transaction (it
   also writes the ALLOWED audit row inside it) -- never a read_only one --
   which splits the correct proof shape in two, by backend:

   * MEMORY / SQLITE (``_GenuineBlockSnapshotTests``): a write-capable
     transaction holds the WHOLE store's lock for its entire duration
     (InMemoryStore's single process-wide lock; SQLite's file-level
     RESERVED lock from ``BEGIN IMMEDIATE`` -- see ``transaction()``'s own
     docstring), so a racer's write CANNOT even start while the target is
     paused mid-transaction. The proof polls that the racer has genuinely
     NOT completed while paused (mirroring test_device_token_writer_
     race.py's own "locked_before" pattern), then resumes and confirms
     both finish, racer strictly second -- so the target's own return
     value and audit can only ever reflect the PRE-race snapshot, by
     construction.
   * POSTGRESQL (``PostgresSnapshotConsistencyTest``): a plain read takes
     no row lock, so a racer's write to an unrelated row is NEVER blocked
     by our open transaction -- it commits in full WHILE we are paused,
     using TWO REAL PHYSICAL psycopg CONNECTIONS (mirroring test_delivery_
     resolve_audit_race.py's own ``_RaceHelpers`` pattern). The proof here
     is AGREEMENT, not blocking: the target's own returned payload and its
     own audited subjects must describe the identical snapshot, whichever
     one that turns out to be.

   THE BUG THIS FOUND (documented here, fixed in api/service.py, not by
   this module): ``player_duplicate_report``'s ALLOWED audit reads the
   SAME players collection TWICE inside its ``with self.store.
   transaction():`` block -- once in the facade (for ``subjects``), once
   again inside ``SetupService.player_duplicate_report``'s own internal
   call (for ``warnings``) -- two SEPARATE statements on the same
   connection. Under PostgreSQL's default READ COMMITTED (this store's
   ``transaction()`` default when ``isolation`` is not given explicitly),
   each statement independently sees the latest COMMITTED data as of ITS
   OWN start, so a concurrent INSERT landing between the two statements
   let the SECOND read's warning mention a player the FIRST read's
   subjects never saw -- proven with a real two-connection repro BEFORE
   the fix. The fix raises this one transaction to
   ``isolation="REPEATABLE READ"`` (SQLite and Memory already get this for
   free through their own full-transaction locking), so every statement
   in the transaction observes one snapshot taken at its first statement,
   regardless of what commits land afterward.
   ``PostgresSnapshotConsistencyTest.test_player_duplicate_report_
   snapshot_consistent_under_race`` is the permanent regression for this.
"""

import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone

from helpers import BACKEND, fresh_sql_store  # noqa: F401

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    Division, League, LeagueSeason, Position, Program, Role, Season, Team)
from hockey_scheduler.store import InMemoryStore, SqlStore


def _seed(store):
    store.add_program(Program(id="pr", name="P"))
    store.add_season(Season(id="se", program_id="pr", name="Fall",
                            start_date=datetime(2026, 9, 1,
                                                tzinfo=timezone.utc)))
    store.add_league(League(id="lg", program_id="pr", name="L"))
    store.add_league_season(
        LeagueSeason(id="ls", league_id="lg", season_id="se"))
    store.add_division(Division(id="d", league_season_id="ls", name="D1",
                                age_group="U10"))
    store.add_team(Team(id="t1", name="T1", program_id="pr"))
    api = ApiService(store)
    api.set_age_eligibility_rule("ls", 12, 31, [{"code": "U10",
                                                  "max_age": 30}])
    p = api.setup.add_player("t1", None, Position.FORWARD,
                             first_name="Priv", last_name="Fields",
                             birthdate="2015-03-02",
                             registration_number="REG-1")
    return api, p


# =========================================================================== #
# 1. FORCED AUDIT-WRITE FAILURE -- 3 call sites x 3 backends                  #
# =========================================================================== #
class _FailureInjectionMatrixTests:
    """Shared body; subclasses provide ``self._store()`` -> a fresh store.

    (#424 round-N+3 owner review GAP 1): a ``boom`` that raises
    unconditionally on the very FIRST ``add_data_access`` call can only ever
    prove "zero audit rows if the earliest possible write fails" -- every
    one of these three call sites makes at least TWO ALLOWED
    ``add_data_access`` calls (one per sensitive-field category, or one per
    SUMMARY/RAW tier for eligibility), and a fail-on-first boom never lets
    the FIRST of those calls actually land, so it can never distinguish "no
    insert ever ran" from "an insert ran, committed, and should have been
    but was NOT rolled back when a LATER sibling write in the same
    transaction failed". This ``boom`` instead lets the FIRST call through
    to the real store method -- a real row really gets written -- and only
    the SECOND call fails, so an empty ledger afterward is proof the
    earlier, already-executed write was actually undone, not merely proof
    that a write attempt never began.
    """

    def _check(self, api_fn):
        store = self._store()
        try:
            api, p = _seed(store)

            orig_add = store.add_data_access
            call_count = {"n": 0}

            def boom(*a, **kw):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    # Let the FIRST write really happen, through the real
                    # store method -- this is the row a fail-on-first-call
                    # boom could never produce, and whose survival past the
                    # second write's failure would prove the transaction is
                    # not actually atomic.
                    return orig_add(*a, **kw)
                raise RuntimeError("simulated audit-write failure")
            store.add_data_access = boom

            with self.assertRaises(RuntimeError):
                api_fn(api, p)

            # This test is only a genuine rollback proof if a SECOND write
            # was actually attempted (and hence a first one actually
            # landed before the failure). If a call site only ever makes
            # one ALLOWED add_data_access call under these arguments, this
            # assertion catches that -- silently degrading back to the
            # weaker "fails on first call" shape must never pass unnoticed.
            self.assertGreaterEqual(
                call_count["n"], 2,
                "add_data_access was called fewer than twice -- this "
                "call site/argument combination does not exercise a "
                "second audit write, so this test cannot prove rollback "
                "of an already-written earlier row")

            # No half-done state: the FIRST audit write (which really
            # landed, through the real store method above) must NOT
            # survive the SECOND write's failure -- the sensitive read and
            # BOTH audit writes are the SAME transaction, so the raise
            # unwinds all of them together, not just the one that failed.
            # Because the first write genuinely committed before the
            # failure (unlike a fail-on-first-call boom, which never lets
            # any write land), an empty ledger here is real proof of
            # same-transaction rollback of an already-written row -- and
            # (since the exception propagated before any `return`) no
            # caller ever observed the sensitive payload either.
            self.assertEqual(
                store.list_data_access(), [],
                "an audit row written before the later failure survived "
                "the transaction's rollback -- the read and both audit "
                "writes are not actually one atomic unit")
        finally:
            store.close()

    def test_list_players_include_identity(self):
        # LEAGUE_ADMIN holds RAW on both BIRTHDATE and REGISTRATION_NUMBER
        # (visibility_policy._POLICY), so list_players's include_identity
        # block makes TWO ALLOWED add_data_access calls -- one per category,
        # BIRTHDATE then REGISTRATION_NUMBER (api/service.py's
        # ``identity_categories`` tuple order) -- exactly the two calls
        # this test's ``boom`` needs to see.
        self._check(lambda api, p: api.list_players(
            team_id="t1", include_identity=True, role=Role.LEAGUE_ADMIN,
            user_id="user_admin"))

    def test_player_duplicate_report(self):
        # LEAGUE_ADMIN also drives TWO ALLOWED add_data_access calls here --
        # REGISTRATION_NUMBER then BIRTHDATE (api/service.py's
        # ``player_duplicate_report``, which checks the two categories in
        # that order and audits both against the same ``subjects`` list).
        self._check(lambda api, p: api.player_duplicate_report(
            actor_role=Role.LEAGUE_ADMIN, actor_user_id="user_admin"))

    def test_evaluate_player_eligibility(self):
        # COACH (the pre-round-N+3 actor here) only ever reaches ONE
        # ALLOWED add_data_access call -- the SUMMARY tier -- because COACH
        # holds no RAW grant for BIRTHDATE, so ``include_details=True``
        # would route the details-tier write through
        # ``add_data_access_durable`` instead (a different store method
        # this ``boom`` never intercepts), never through ``add_data_access``
        # a second time. LEAGUE_ADMIN holds RAW too, so
        # ``include_details=True`` here reaches the details tier via the
        # SAME ALLOWED ``add_data_access`` path as the SUMMARY tier --
        # SUMMARY succeeds (call 1), the details write fails (call 2), and
        # this test can actually prove the SUMMARY row is rolled back too.
        self._check(lambda api, p: api.evaluate_player_eligibility(
            p.id, "d", actor_role=Role.LEAGUE_ADMIN,
            actor_user_id="user_admin", include_details=True))


class MemoryFailureInjectionTest(_FailureInjectionMatrixTests,
                                 unittest.TestCase):
    def _store(self):
        return InMemoryStore()


class SqliteFailureInjectionTest(_FailureInjectionMatrixTests,
                                 unittest.TestCase):
    def _store(self):
        fd, self._tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.remove, self._tmp)
        return SqlStore(self._tmp)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL suite only (TEST_DATABASE_URL not set)")
class PostgresFailureInjectionTest(_FailureInjectionMatrixTests,
                                   unittest.TestCase):
    def _store(self):
        return fresh_sql_store(os.environ["TEST_DATABASE_URL"])



# =========================================================================== #
# 2. SNAPSHOT CONSISTENCY UNDER CONTENTION -- 3 call sites.                   #
#                                                                              #
# Every one of these three facade methods opens a WRITE-CAPABLE transaction  #
# (it also writes the ALLOWED audit row inside it), never a read_only one.   #
# That single fact splits the correct proof shape in two, by backend:        #
#                                                                              #
# MEMORY / SQLITE (``_GenuineBlockSnapshotTests``): a write-capable          #
# transaction takes the WHOLE store's lock for its entire duration --        #
# InMemoryStore's single process-wide lock, or SQLite's file-level           #
# RESERVED lock from ``BEGIN IMMEDIATE`` (see ``transaction()``'s own        #
# docstring). That means a racer's own write CANNOT so much as start its     #
# OWN transaction while our target is paused mid-transaction: the proof      #
# here is "the racer's write is GENUINELY BLOCKED until we finish", mirroring#
# ``test_device_token_writer_race.py``'s own "locked_before" pattern --      #
# proven (never assumed) by polling that the racer has NOT completed while   #
# still paused, before resuming and letting both finish, racer strictly      #
# second. Because the racer's write is provably excluded for our whole       #
# read+audit-write's duration, our own return value and our own audit can    #
# only ever reflect ONE snapshot: whatever existed before the racer, by      #
# construction.                                                              #
#                                                                              #
# POSTGRESQL (``PostgresSnapshotConsistencyTest``): a plain read takes no    #
# row lock, so a racer's UPDATE on an UNRELATED row is never blocked by our  #
# open transaction -- it can commit in full WHILE we are paused mid-read.    #
# The proof here is different: the racer's write completes (unblocked,      #
# genuinely concurrent, TWO REAL PHYSICAL psycopg CONNECTIONS -- mirroring   #
# test_delivery_resolve_audit_race.py's own ``_RaceHelpers`` pattern) WHILE  #
# we are paused, and we assert our own returned payload and our own audited  #
# subjects still agree with EACH OTHER -- describing the SAME snapshot,      #
# whichever one (old or new) that turns out to be.                          #
# =========================================================================== #

def _pause_list_players(store, paused, resume):
    orig = store.players_for_team

    def instrumented(team_id):
        result = orig(team_id)
        paused.set()
        resume.wait(15)
        return result
    store.players_for_team = instrumented


def _pause_duplicate_report(store, paused, resume):
    """Pauses AFTER the facade's OWN first ``all_players()`` read (used to
    build ``subjects``) but BEFORE ``SetupService.player_duplicate_
    report``'s internal second ``all_players()`` call -- the exact
    two-statement gap the PostgreSQL fix (``isolation="REPEATABLE READ"``)
    closes. Only the FIRST call pauses; the second (inside SetupService)
    runs straight through so the transaction can finish."""
    orig = store.all_players
    call_count = {"n": 0}

    def instrumented():
        call_count["n"] += 1
        result = orig()
        if call_count["n"] == 1:
            paused.set()
            resume.wait(15)
        return result
    store.all_players = instrumented


def _pause_eligibility(store, paused, resume):
    orig = store.get_player

    def instrumented(player_id):
        result = orig(player_id)
        paused.set()
        resume.wait(15)
        return result
    store.get_player = instrumented


class _GenuineBlockSnapshotTests:
    """Memory + SQLite: the racer's write is PROVEN to genuinely block for
    the target's whole paused duration, then both complete, racer second.
    Subclasses provide ``self._store()`` -> a store/connection sharing the
    SAME backing data as every other ``self._store()`` call this test makes
    (the identical shared instance for Memory; a fresh connection onto the
    SAME file for SQLite)."""

    def _race(self, pause_fn, mutate_fn, target_fn):
        paused = threading.Event()
        resume = threading.Event()
        racer_attempted = threading.Event()
        racer_done = threading.Event()
        pause_fn(paused, resume)

        result = {}
        errors = {}

        def run_target():
            try:
                result["value"] = target_fn()
            except BaseException as exc:  # noqa: BLE001
                errors["target"] = exc

        def run_racer():
            try:
                if not paused.wait(15):
                    errors["racer"] = RuntimeError(
                        "target never reached the pause")
                    return
                # ``mutate_fn`` is handed ``racer_attempted`` and sets it
                # itself, immediately before its OWN store call (#424
                # round-N+3 owner review GAP 2) -- never here, and never
                # merely "the racer thread got CPU time at all": setting it
                # any earlier (e.g. right after ``paused.wait`` returns)
                # would only prove the OS scheduler ran this thread, not
                # that it reached the actual mutating call that could
                # contend with the target's lock.
                mutate_fn(racer_attempted)
                racer_done.set()
            except BaseException as exc:  # noqa: BLE001
                errors["racer"] = exc

        t_target = threading.Thread(target=run_target)
        t_racer = threading.Thread(target=run_racer)
        t_target.start()
        t_racer.start()

        self.assertTrue(paused.wait(15), "target never reached the pause")

        # PROOF THE RACER GENUINELY REACHED ITS OWN WRITE ATTEMPT (#424
        # round-N+3 owner review GAP 2): a bare ``racer_done.wait(0.3) ==
        # False`` below is scheduler-dependent -- it is equally consistent
        # with "the racer's write is genuinely blocked" and with "the OS
        # simply never scheduled the racer thread within this 0.3s window",
        # and the two are indistinguishable from that assertion alone. This
        # wait is the fix: it blocks (up to 15s) for PROOF the racer thread
        # is now actually contending for the target's lock -- set by
        # ``mutate_fn`` itself, immediately before ITS OWN store call, never
        # inferred from timing.
        self.assertTrue(
            racer_attempted.wait(15),
            "racer thread never reached its own mutation/transaction -- "
            "cannot prove genuine blocking without first proving the "
            "racer was actually contending")

        # PROOF OF GENUINE BLOCKING: NOW that the racer is proven to have
        # actually reached its own write attempt, it still cannot COMPLETE
        # while our write-capable transaction holds the store's lock
        # (SQLite RESERVED / Memory's process-wide lock) -- poll a short,
        # bounded window and assert it has NOT completed. Unlike the old,
        # unguarded version of this same assertion, a pass here can no
        # longer be explained by "the racer thread just hadn't run yet".
        self.assertFalse(
            racer_done.wait(0.3),
            "racer completed its write while the target was still paused "
            "mid-transaction -- the read and the audit write are not "
            "actually atomic against a concurrent writer")

        resume.set()
        t_target.join(15)
        t_racer.join(15)
        if "target" in errors:
            raise errors["target"]
        if "racer" in errors:
            raise AssertionError(f"racer thread raised: {errors['racer']!r}")
        self.assertTrue(racer_done.is_set(), "racer never completed")
        return result["value"]

    def test_list_players_snapshot_consistent_under_race(self):
        store = self._store()
        try:
            api, p = _seed(store)

            def pause_fn(paused, resume):
                _pause_list_players(store, paused, resume)

            def mutate(racer_attempted):
                racer_api = ApiService(self._racer_store(store))
                # Set immediately before the racer's OWN store call -- the
                # instant this fires, the racer is genuinely attempting its
                # write, not merely "scheduled at some point" (GAP 2).
                racer_attempted.set()
                racer_api.update_player(p.id, registration_number="REG-DURING")

            def go():
                rows = api.list_players(team_id="t1", include_identity=True,
                                        role=Role.LEAGUE_ADMIN,
                                        user_id="user_admin")
                return rows[0]

            row = self._race(pause_fn, mutate, go)
            rows_out = [r for r in store.list_data_access()
                       if r.purpose == "list_players_identity"
                       and r.category.name == "REGISTRATION_NUMBER"]
            self.assertEqual(len(rows_out), 1, rows_out)
            self.assertEqual(rows_out[0].subject_id, f"player:{p.id}")
            # The racer was PROVEN blocked for our whole transaction, so our
            # own read can only ever have seen the PRE-race value.
            self.assertEqual(row["registration_number"], "REG-1")

            # Confirm the racer's write landed AFTER we finished (sequential
            # completion, not lost): a fresh read now sees it.
            fresh = api.list_players(team_id="t1", include_identity=True,
                                     role=Role.LEAGUE_ADMIN,
                                     user_id="user_admin")[0]
            self.assertEqual(fresh["registration_number"], "REG-DURING")
        finally:
            store.close() if isinstance(store, SqlStore) else None

    def test_player_duplicate_report_snapshot_consistent_under_race(self):
        store = self._store()
        try:
            api, p1 = _seed(store)
            p2 = api.setup.add_player(
                "t1", None, Position.GOALIE, first_name="Warn",
                last_name="Pair", registration_number="REG-SHARED")

            def pause_fn(paused, resume):
                _pause_duplicate_report(store, paused, resume)

            inserted = {}

            def mutate(racer_attempted):
                racer_api = ApiService(self._racer_store(store))
                racer_attempted.set()
                inserted["p"] = racer_api.setup.add_player(
                    "t1", None, Position.DEFENSE, first_name="Race",
                    last_name="Inserted", registration_number="REG-RACE")

            def go():
                return api.player_duplicate_report(
                    actor_role=Role.LEAGUE_ADMIN, actor_user_id="user_admin")

            report = self._race(pause_fn, mutate, go)
            self.assertNotIn("error", report, report)
            warning_ids = {pid for w in report["warnings"]
                          for pid in w["player_ids"]}
            # The racer's insert was PROVEN blocked until our transaction
            # finished, so it cannot appear in our OWN warnings at all.
            self.assertNotIn(inserted["p"].id, warning_ids)

            rows = [r for r in store.list_data_access()
                   if r.purpose == "player_duplicate_report"]
            reg_subjects = {r.subject_id for r in rows
                           if r.category.name == "REGISTRATION_NUMBER"}
            self.assertNotIn(f"player:{inserted['p'].id}", reg_subjects,
                             "racer's player was audited despite its write "
                             "being blocked until after our transaction")
            self.assertEqual(reg_subjects,
                             {f"player:{p1.id}", f"player:{p2.id}"})
        finally:
            store.close() if isinstance(store, SqlStore) else None

    def test_evaluate_player_eligibility_snapshot_consistent_under_race(self):
        store = self._store()
        try:
            api, p = _seed(store)

            def pause_fn(paused, resume):
                _pause_eligibility(store, paused, resume)

            def mutate(racer_attempted):
                racer_api = ApiService(self._racer_store(store))
                racer_attempted.set()
                racer_api.update_player(p.id, birthdate="1990-01-01")

            def go():
                return api.evaluate_player_eligibility(
                    p.id, "d", actor_role=Role.LEAGUE_ADMIN,
                    actor_user_id="user_admin", include_details=True)

            result = self._race(pause_fn, mutate, go)
            self.assertNotIn("error", result, result)
            rows = [r for r in store.list_data_access()
                   if r.purpose == "evaluate_player_eligibility"]
            self.assertEqual(len(rows), 1, rows)
            self.assertEqual(rows[0].subject_id, f"player:{p.id}")
            # The racer's birthdate change was PROVEN blocked until after
            # our transaction, so our verdict reflects the ORIGINAL
            # (eligible, U10-aged) birthdate, not the racer's (too old).
            self.assertEqual(result["status"], "eligible")
        finally:
            store.close() if isinstance(store, SqlStore) else None


class MemorySnapshotConsistencyTest(_GenuineBlockSnapshotTests,
                                    unittest.TestCase):
    """A single shared ``InMemoryStore`` -- there is only ONE store, and
    its own ``transaction()`` holds one process-wide lock for the target's
    whole paused duration, so ANY racer call through it (a fresh
    ``ApiService`` wrapping the SAME store) blocks on that same lock."""

    def setUp(self):
        self._shared = InMemoryStore()

    def _store(self):
        return self._shared

    def _racer_store(self, seed_store):
        return seed_store


class SqliteSnapshotConsistencyTest(_GenuineBlockSnapshotTests,
                                    unittest.TestCase):
    """Every ``self._store()``/``self._racer_store()`` call opens a
    genuinely separate connection onto the SAME file -- real
    inter-process-equivalent concurrency, never two threads sharing one
    connection."""

    def setUp(self):
        fd, self._tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        SqlStore(self._tmp).close()  # migrate, then release
        self.addCleanup(os.remove, self._tmp)
        self._opened = []
        self.addCleanup(self._close_all)

    def _close_all(self):
        for s in self._opened:
            s.close()

    def _store(self):
        s = SqlStore(self._tmp)
        self._opened.append(s)
        return s

    def _racer_store(self, seed_store):
        return self._store()


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL suite only (TEST_DATABASE_URL not set)")
class PostgresSnapshotConsistencyTest(unittest.TestCase):
    """PostgreSQL, TWO REAL PHYSICAL psycopg CONNECTIONS: a racer's write to
    an UNRELATED row is never blocked by our own open transaction (no row
    lock is ever taken by a plain read), so it genuinely commits WHILE we
    are paused mid-transaction. The proof here is snapshot AGREEMENT, not
    blocking: whatever our own returned payload and our own audited
    subjects reflect, they must reflect the SAME thing."""

    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        self._opened = []
        self.addCleanup(self._close_all)
        self._track(fresh_sql_store(self.url)).clear_all_data()

    def _close_all(self):
        for s in self._opened:
            s.close()

    def _track(self, store):
        self._opened.append(store)
        return store

    def _store(self):
        return self._track(SqlStore(self.url))

    def _txid(self, store):
        """The database's OWN transaction id (mirrors ``test_sensitive_
        read_audit_atomicity.PostgresAtomicityTest._txid``): proof of ONE
        physical PostgreSQL backend transaction, not merely one Python-level
        ``with self.store.transaction():`` block. Two calls made under the
        SAME backend transaction always return the SAME value; two calls
        under different transactions (or different connections) never do."""
        cur = store.conn.cursor()
        cur.execute("SELECT txid_current() AS tx")
        row = cur.fetchone()
        cur.close()
        try:
            return row["tx"]
        except (TypeError, KeyError):
            return row[0]

    def _race(self, pause_fn, mutate_fn, target_fn):
        paused = threading.Event()
        resume = threading.Event()
        racer_done = threading.Event()
        pause_fn(paused, resume)

        result = {}
        errors = {}

        def run_target():
            try:
                result["value"] = target_fn()
            except BaseException as exc:  # noqa: BLE001
                errors["target"] = exc

        def run_racer():
            try:
                if not paused.wait(15):
                    errors["racer"] = RuntimeError(
                        "target never reached the pause")
                    return
                mutate_fn()
                racer_done.set()
            except BaseException as exc:  # noqa: BLE001
                errors["racer"] = exc
            finally:
                resume.set()

        t_target = threading.Thread(target=run_target)
        t_racer = threading.Thread(target=run_racer)
        t_target.start()
        t_racer.start()
        t_target.join(15)
        t_racer.join(15)
        if "target" in errors:
            raise errors["target"]
        if "racer" in errors:
            raise AssertionError(f"racer thread raised: {errors['racer']!r}")
        self.assertTrue(racer_done.is_set(), "racer never completed")
        return result["value"]

    def test_list_players_snapshot_consistent_under_race(self):
        """(#424 round-N+3 owner review GAP 3): the pre-round-N+3 version of
        this test raced a mutation of a FIELD on the one already-included
        player and accepted EITHER value -- a shape that also passes if the
        protected read and the audit write are split into two separate
        transactions (nothing here ever depended on them sharing one). This
        version closes both halves the review names:

        1. The racer INSERTS A SECOND PLAYER instead of mutating the
           existing one -- moving the RACE onto the RETURNED/AUDITED
           SUBJECT SET itself. Whichever snapshot our read lands on (with
           or without the new player), the set of player ids RETURNED and
           the set of player ids AUDITED must be EXACTLY EQUAL -- a
           genuinely discriminating check: if the read and the audit write
           ran as two separate statements/transactions, a racer landing
           between them could make one see the insert and the other not,
           splitting the two sets.
        2. ``txid_current()`` is captured at the protected read
           (``players_for_team``) and at the ALLOWED audit write
           (``add_data_access``) and asserted EQUAL -- proof of one
           physical PostgreSQL backend transaction, not merely one
           Python-level ``transaction()`` block. See this class's
           ``test_list_players_txid_identity_negative_control`` for the
           required falsifiability proof that this specific assertion
           actually fails when the read is moved outside the transaction.
        """
        store = self._store()
        api, p1 = _seed(store)

        read_txids = []
        write_txids = []

        orig_players_for_team = store.players_for_team

        def spy_read(team_id):
            result = orig_players_for_team(team_id)
            read_txids.append(self._txid(store))
            return result
        store.players_for_team = spy_read

        orig_add_data_access = store.add_data_access

        def spy_add(*a, **kw):
            write_txids.append(self._txid(store))
            return orig_add_data_access(*a, **kw)
        store.add_data_access = spy_add

        def pause_fn(paused, resume):
            # Wraps the ALREADY-instrumented ``players_for_team`` (spy_read
            # above) so txid capture still happens, exactly once, on every
            # call -- the pause point stays AFTER the read returns, still
            # inside the transaction, matching every other pause helper in
            # this module.
            orig = store.players_for_team

            def instrumented(team_id):
                result = orig(team_id)
                paused.set()
                resume.wait(15)
                return result
            store.players_for_team = instrumented

        inserted = {}

        def mutate():
            racer_api = ApiService(self._store())
            inserted["p"] = racer_api.setup.add_player(
                "t1", None, Position.DEFENSE, first_name="Race",
                last_name="Inserted", registration_number="REG-RACE")

        def go():
            return api.list_players(team_id="t1", include_identity=True,
                                    role=Role.LEAGUE_ADMIN,
                                    user_id="user_admin")

        rows = self._race(pause_fn, mutate, go)
        returned_ids = {row["id"] for row in rows}

        audit_rows = [r for r in store.list_data_access()
                     if r.purpose == "list_players_identity"
                     and r.category.name == "REGISTRATION_NUMBER"]
        audited_ids = {r.subject_id.split(":", 1)[1] for r in audit_rows}

        # THE DISCRIMINATING PROOF: whichever snapshot the read landed on,
        # the RETURNED and AUDITED subject sets must be the identical set.
        # This can only fail if the protected read and the audit write
        # disagreed about which players were examined.
        self.assertEqual(
            returned_ids, audited_ids,
            "returned player ids and audited subject ids disagree -- the "
            "protected read and the audit write did not observe the same "
            "snapshot")
        # p1 (seeded before the race even starts) is unconditionally in
        # both sets on every run -- a sanity floor under the discriminating
        # check above, not itself the proof.
        self.assertIn(p1.id, returned_ids)
        self.assertIn(p1.id, audited_ids)
        # The racer's insert is either fully absent from BOTH sets (read
        # snapshot predates the insert) or fully present in BOTH (insert
        # committed before the read) -- asserting membership rather than
        # equality to either fixed set, since which snapshot wins is a
        # genuine, expected race outcome the store's own lock behavior
        # decides, not something this test dictates.
        self.assertEqual(inserted["p"].id in returned_ids,
                         inserted["p"].id in audited_ids)

        # SAME PHYSICAL POSTGRESQL BACKEND TRANSACTION at the protected
        # read and the ALLOWED audit write -- not merely "a python-level
        # transaction() block was open".
        self.assertTrue(read_txids, "protected read was never instrumented")
        self.assertTrue(write_txids, "audit write was never instrumented")
        self.assertEqual(
            set(read_txids), set(write_txids),
            f"protected read ran under txid(s) {set(read_txids)}, audit "
            f"write under {set(write_txids)} -- two different PostgreSQL "
            "backend transactions, not one atomic unit")

    def test_list_players_txid_identity_negative_control(self):
        """FALSIFIABILITY (#424 round-N+3 owner review GAP 3 -- the review's
        own required negative control): temporarily route the protected
        read through a SEPARATE physical PostgreSQL connection/transaction
        -- reproducing the exact pre-round-N owner-review bug shape
        ``test_sensitive_read_audit_atomicity.py``'s own module docstring
        describes ("a bare read made outside transaction() is its own
        already-committed unit of work, never joined to whatever
        transaction opens later purely for the audit write") -- and confirm
        THIS test's own txid-identity assertion actually FAILS against that
        broken shape. If it did not, the assertion in the test above would
        be decorative, not discriminating."""
        store = self._store()
        api, p1 = _seed(store)
        separate = self._store()

        read_txids = []
        write_txids = []

        orig_players_for_team = separate.players_for_team

        def broken_read(team_id):
            # THE BUG: this read runs on a SEPARATE connection, in its OWN
            # transaction, never joined to the one that will make the
            # audit write below.
            with separate.transaction():
                result = orig_players_for_team(team_id)
                read_txids.append(self._txid(separate))
            return result
        store.players_for_team = broken_read

        orig_add_data_access = store.add_data_access

        def spy_add(*a, **kw):
            write_txids.append(self._txid(store))
            return orig_add_data_access(*a, **kw)
        store.add_data_access = spy_add

        api.list_players(team_id="t1", include_identity=True,
                         role=Role.LEAGUE_ADMIN, user_id="user_admin")

        self.assertTrue(read_txids, "protected read was never instrumented")
        self.assertTrue(write_txids, "audit write was never instrumented")
        self.assertNotEqual(
            set(read_txids), set(write_txids),
            "sanity check failed: the deliberately-broken read landed on "
            "the SAME txid as the write -- the negative control did not "
            "actually reproduce a split transaction, so it proves nothing")
        # THE REQUIRED PROOF: the real test's own assertion, re-run here
        # against the broken shape, must raise.
        with self.assertRaises(AssertionError):
            self.assertEqual(
                set(read_txids), set(write_txids),
                f"protected read ran under txid(s) {set(read_txids)}, "
                f"audit write under {set(write_txids)}")

    def test_player_duplicate_report_snapshot_consistent_under_race(self):
        """THE bug this module found: pre-fix, under READ COMMITTED, the
        facade's own subjects-read and SetupService's internal warnings-
        read were two separate statements that could see DIFFERENT
        committed snapshots. Post-fix (isolation="REPEATABLE READ"), both
        observe the transaction's OPENING snapshot regardless of what the
        racer commits afterward -- so the racer's insert is invisible to
        BOTH reads, and warnings/subjects always agree.

        The racer MUST collide on ``registration_number`` with an already-
        seeded player (``REG-SHARED``, shared with ``p2``) -- otherwise the
        racer's insert can never be mentioned in a ``shared_registration_
        number`` warning at all, and the ``if racer_id in warning_ids``
        branch below never executes on either isolation level, making this
        test pass unconditionally regardless of the fix. Same-team
        duplicate registration numbers are hard-refused synchronously at
        write time (``SetupService._assert_registration_number_available``),
        so the racer is inserted on a SECOND team (``t2``) -- cross-team
        duplicates are allowed at write time and are exactly what ``shared_
        registration_number`` warns about (see ``SetupService.player_
        duplicate_report``'s docstring)."""
        store = self._store()
        api, p1 = _seed(store)
        p2 = api.setup.add_player(
            "t1", None, Position.GOALIE, first_name="Warn", last_name="Pair",
            registration_number="REG-SHARED")
        store.add_team(Team(id="t2", name="T2", program_id="pr"))

        def pause_fn(paused, resume):
            _pause_duplicate_report(store, paused, resume)

        inserted = {}

        def mutate():
            racer_api = ApiService(self._store())
            inserted["p"] = racer_api.setup.add_player(
                "t2", None, Position.DEFENSE, first_name="Race",
                last_name="Inserted", registration_number="REG-SHARED")

        def go():
            return api.player_duplicate_report(
                actor_role=Role.LEAGUE_ADMIN, actor_user_id="user_admin")

        report = self._race(pause_fn, mutate, go)
        self.assertNotIn("error", report, report)
        warning_ids = {pid for w in report["warnings"]
                      for pid in w["player_ids"]}
        rows = [r for r in store.list_data_access()
               if r.purpose == "player_duplicate_report"]
        reg_subjects = {r.subject_id for r in rows
                       if r.category.name == "REGISTRATION_NUMBER"}
        racer_id = inserted["p"].id
        # THE PROOF: if the racer's insert is mentioned in a warning, it
        # MUST also be an audited subject -- never one without the other.
        # With REPEATABLE READ (fix in place), the racer's REG-SHARED
        # insert is invisible to the internal warnings-read too (same
        # opening snapshot as the facade's subjects-read), so this always
        # lands in the "else" branch below. Under READ COMMITTED (isolation
        # reverted -- see falsifiability), the warnings-read sees the
        # racer's already-committed row and pairs it with p2 under
        # REG-SHARED, so this branch genuinely fires and the assertion
        # below genuinely fails.
        if racer_id in warning_ids:
            self.assertIn(f"player:{racer_id}", reg_subjects,
                         "warning mentions a player never audited as an "
                         "examined subject -- snapshot divergence under "
                         "contention")
        else:
            self.assertNotIn(f"player:{racer_id}", reg_subjects,
                             "player audited as examined but absent from "
                             "every warning read -- snapshot divergence")

    def test_evaluate_player_eligibility_snapshot_consistent_under_race(self):
        """(#424 round-N+3 owner review GAP 3): eligibility examines exactly
        ONE player by construction (``player_id`` is a caller-supplied
        argument, not a collection this race could ever widen or narrow),
        so -- unlike ``list_players``/``player_duplicate_report`` -- there
        is no "returned/audited subject SET" for a race to move; the
        pre-existing ``self.assertIn(result["status"], (...))`` shape was
        already correct in that narrow sense. What it never proved is the
        review's second, still-missing half: that the protected read
        (``get_player``) and the ALLOWED audit write(s) (``add_data_access``
        -- both the SUMMARY tier and, with ``include_details=True`` as
        LEAGUE_ADMIN, the RAW/details tier) actually share ONE physical
        PostgreSQL backend transaction, proven with ``txid_current()``, not
        merely "a python-level transaction() block was open". See this
        class's ``test_evaluate_player_eligibility_txid_identity_negative_
        control`` for the required falsifiability proof that this specific
        assertion actually fails when the read is moved outside the
        transaction."""
        store = self._store()
        api, p = _seed(store)

        read_txids = []
        write_txids = []

        orig_get_player = store.get_player

        def spy_read(player_id):
            result = orig_get_player(player_id)
            read_txids.append(self._txid(store))
            return result
        store.get_player = spy_read

        orig_add_data_access = store.add_data_access

        def spy_add(*a, **kw):
            write_txids.append(self._txid(store))
            return orig_add_data_access(*a, **kw)
        store.add_data_access = spy_add

        def pause_fn(paused, resume):
            orig = store.get_player

            def instrumented(player_id):
                result = orig(player_id)
                paused.set()
                resume.wait(15)
                return result
            store.get_player = instrumented

        def mutate():
            racer_api = ApiService(self._store())
            racer_api.update_player(p.id, birthdate="1990-01-01")

        def go():
            return api.evaluate_player_eligibility(
                p.id, "d", actor_role=Role.LEAGUE_ADMIN,
                actor_user_id="user_admin", include_details=True)

        result = self._race(pause_fn, mutate, go)
        self.assertNotIn("error", result, result)
        rows = [r for r in store.list_data_access()
               if r.purpose == "evaluate_player_eligibility"]
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0].subject_id, f"player:{p.id}")
        # A single get_player() call cannot tear -- the status is
        # unambiguously derived from ONE birthdate value, old or new.
        self.assertIn(result["status"], ("eligible", "ineligible"))

        # SAME PHYSICAL POSTGRESQL BACKEND TRANSACTION at the protected
        # read and EVERY ALLOWED audit write (SUMMARY tier plus, since
        # LEAGUE_ADMIN + include_details=True reaches it, the RAW/details
        # tier) -- not merely "a python-level transaction() block was open".
        self.assertTrue(read_txids, "protected read was never instrumented")
        self.assertTrue(write_txids, "audit write was never instrumented")
        self.assertEqual(
            set(read_txids), set(write_txids),
            f"protected read ran under txid(s) {set(read_txids)}, audit "
            f"write under {set(write_txids)} -- two different PostgreSQL "
            "backend transactions, not one atomic unit")

    def test_evaluate_player_eligibility_txid_identity_negative_control(self):
        """FALSIFIABILITY (#424 round-N+3 owner review GAP 3 -- the review's
        own required negative control): temporarily route the protected
        ``get_player`` read through a SEPARATE physical PostgreSQL
        connection/transaction -- the same pre-round-N broken shape
        ``test_list_players_txid_identity_negative_control`` reproduces for
        ``list_players`` -- and confirm THIS test's own txid-identity
        assertion actually FAILS against it."""
        store = self._store()
        api, p = _seed(store)
        separate = self._store()

        read_txids = []
        write_txids = []

        orig_get_player = separate.get_player

        def broken_read(player_id):
            with separate.transaction():
                result = orig_get_player(player_id)
                read_txids.append(self._txid(separate))
            return result
        store.get_player = broken_read

        orig_add_data_access = store.add_data_access

        def spy_add(*a, **kw):
            write_txids.append(self._txid(store))
            return orig_add_data_access(*a, **kw)
        store.add_data_access = spy_add

        result = api.evaluate_player_eligibility(
            p.id, "d", actor_role=Role.LEAGUE_ADMIN,
            actor_user_id="user_admin", include_details=True)
        self.assertNotIn("error", result, result)

        self.assertTrue(read_txids, "protected read was never instrumented")
        self.assertTrue(write_txids, "audit write was never instrumented")
        self.assertNotEqual(
            set(read_txids), set(write_txids),
            "sanity check failed: the deliberately-broken read landed on "
            "the SAME txid as the write -- the negative control did not "
            "actually reproduce a split transaction, so it proves nothing")
        with self.assertRaises(AssertionError):
            self.assertEqual(
                set(read_txids), set(write_txids),
                f"protected read ran under txid(s) {set(read_txids)}, "
                f"audit write under {set(write_txids)}")


if __name__ == "__main__":
    unittest.main()
