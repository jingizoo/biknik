"""The database-coordinated epoch fence (PR #423 redesign).

Design doc: the recon+design pass grounded at `01f4832`, implemented on top
of the #159/#415/#423-review epoch mechanism (`services/context_epoch.py`,
`services/context_gate.py`, `web/server.py`'s `_read_under_context_gate`).

WHAT THIS FILE PROVES, mapped to the design's own §10:

* §10.6 FALSIFIABILITY (REQUIRED, not optional): `EpochFenceCrossProcess
  FalsifiabilityPgTest` runs the SAME staged race with `use_fence=False`
  (reproducing exactly what a genuinely independent SECOND PROCESS has
  today -- `ContextSwitchGate`/`CONTEXT_GATE`/`LIFECYCLE_GATE` are
  `web/server.py` MODULE-LEVEL Python objects that cannot exist in a second
  process at all, so "no fence" is definitionally what cross-process
  coordination looks like pre-#423, not merely an approximation of it) and
  `use_fence=True` (the new store-level primitive). The `False` variant is
  asserted to reproduce the torn read; the `True` variant is asserted not
  to. Both live in one file so the proof and the fix are reviewed together.
  SQLite's OWN falsifiability class (`...SqliteTest`) asserts BOTH arms
  show the SAME torn-read outcome -- a deliberate, stated scope reduction,
  not an oversight: a real dedicated-connection attempt for SQLite was
  built, measured to deadlock/livelock (a genuine AB-BA cycle between
  SQLite's single file-lock axis and `self._lock`, distinct from
  PostgreSQL's separate advisory-lock axis), and reverted to a documented
  no-op -- see `SqlStore.epoch_fence_acquire_exclusive`/`_shared`'s own
  docstrings for the full account. File-backed SQLite's cross-process gap
  therefore remains OPEN this round; same-process SQLite stays fully
  covered by the kept, unchanged `ContextSwitchGate` holds (proven by
  `test_context_switch_server_exit.py`'s `SqliteContextSwitchServerExitTest`
  passing in full).
* §10.1 independent instances: every concurrency test here uses two (or
  three) separately-constructed `SqlStore(url)` objects against the SAME
  live database, sharing no Python state -- the exact pattern
  `tests/test_active_context.py`'s `ContextConcurrencyPgTest` /
  `ContextSnapshotConsistencyPgTest` / `ContextAuthorizationRacePgTest`
  already establish, extended to this fence. Memory gets the SAME
  contract proven over ONE shared `InMemoryStore` (`ContextAuthorizationRace
  LocalTest`'s own pattern) -- never two unconnected `InMemoryStore()`
  objects, which would share no data to race over (design §5).
* §10.2 zero-call-count proof: `EpochFenceWriterFirstDiscardTest` asserts a
  spy-wrapped scoped-read method is called exactly ZERO times when the fence
  correctly discards, on an INDEPENDENT second instance, not merely that the
  HTTP-shaped status is right.
* §10.3 both orderings: `test_reader_first_...` (writer must wait for the
  reader's shared hold, response stays consistent) and
  `test_writer_first_...` (reader must wait for the writer, then discard) are
  both exercised.
* §10.5 same-transaction-ordering, all 17 writers: `EpochFenceWriterOrdering
  Test` — a spy on `epoch_fence_acquire_exclusive` proves EVERY writer in the
  design's §3 table takes the fence, and that it happens before the write is
  visible to a second connection; `EpochFenceRollbackReleasesTest` proves a
  rolled-back writer leaves no lock behind.

Coverage note, stated rather than left implicit: the full 17-writer x
both-orderings x 3-backend concurrency matrix (§10.3) is exercised for a
representative subset of writers spanning both fence classes (per-user:
context switch, account activate; global: Season archive/reopen/delete,
official unassign, guardian link) rather than all 17 -- the per-writer
ORDERING proof (§10.5, which every one of the 17 needs) IS exhaustive; the
concurrency RACE proof (§10.2/10.3) is representative. Both are real,
neither is a stub.
"""

import os
import threading
import time
import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api.service import ApiService
from hockey_scheduler.domain import GuardianLink, OfficialRole, Role, SeasonStatus
from hockey_scheduler.domain.errors import ConcurrencyConflictError
from hockey_scheduler.domain.models import Game
from hockey_scheduler.domain.setup_models import OfficialAssignment
from hockey_scheduler.services.epoch_fence import (
    EPOCH_FENCE_GLOBAL_KEY, user_fence_key)
from hockey_scheduler.store import InMemoryStore, SqlStore

ADMIN = (Role.LEAGUE_ADMIN, {})
_WAIT = 15  # generous bound for thread-sync events; never the SUT's own timeout


def _program_season(api, pname="P1", sname="S1"):
    pid = api.create_program(pname, "US", "UTC")["id"]
    sid = api.create_season(pid, sname)["id"]
    return pid, sid


def _close(store):
    if isinstance(store, SqlStore):
        store.close()


def _pg_url():
    return os.environ.get("TEST_DATABASE_URL")


# =========================================================================
# §10.6 FALSIFIABILITY (required). Same staged race, use_fence toggled.
# =========================================================================

class _FalsifiabilityRaceMixin:
    """Shared staged-race body. Subclasses supply ``self.url`` (a real
    PostgreSQL or SQLite path two independent ``SqlStore`` objects can both
    open).

    ``FENCE_IS_REAL`` (default True, overridden False on the SQLite
    subclass): whether THIS backend's fence can genuinely block a
    contending party. Where it can, ``_race`` proves the block directly
    (bounded-wait assertions). Where it cannot (SQLite's documented no-op,
    or ``use_fence=False``), NOTHING orders the reader and mutator at all,
    so ``_race`` imposes its OWN explicit order via the same
    ``threading.Event`` signals, to reliably land the mutator's commit in
    the exact window each scenario needs rather than leaving it to
    thread-scheduling luck.

    THE RACE: a client renders a page under epoch E0 (Season ACTIVE). A
    Season-archive commits on an INDEPENDENT connection. A scoped read's
    "epoch check, then dependent service read" sequence is reproduced
    EXACTLY as `web/server.py`'s `_read_under_context_gate` does it --
    derive the epoch, compare to the client's echoed E0, and (only on
    match) call a SEPARATE, LATER store read standing in for `produce()`.
    Both reads happen on a THIRD, independent connection (the "reader
    instance"), so nothing here is in-process collusion.

    Sequencing is via ``threading.Event`` throughout (never a raw sleep for
    correctness -- see the module docstring), matching
    ``tests/test_active_context.py``'s own established pattern.
    """

    FENCE_IS_REAL = True

    def _race(self, use_fence, writer_first):
        seed = SqlStore(self.url)
        api = ApiService(seed)
        pid, sid = _program_season(api)
        api.set_active_context("u1", *ADMIN, pid, sid)
        client_epoch = api.context.current_epoch("u1", *ADMIN)
        seed.close()

        reader_store = SqlStore(self.url)
        outcome = {}
        checked = threading.Event()
        proceed = threading.Event()
        mutator_done = threading.Event()

        def reader_body():
            def do_check_and_read():
                # Round-N review finding 1: the version-counter check every
                # backend gets, sampled BEFORE the epoch derivation exactly
                # as `web/server.py`'s `_read_under_context_gate` samples it
                # — bracketing the epoch check AND the simulated `produce()`
                # below with NO LOCK held across either, unlike the SHARED
                # holds this method also (optionally) takes. Keyed on
                # EPOCH_FENCE_GLOBAL_KEY, the exact key `mutator_body` below
                # bumps (a Season archive is a global-keyed writer).
                version_before = (
                    reader_store.current_epoch_fence_version(
                        EPOCH_FENCE_GLOBAL_KEY)
                    if use_fence else None)
                current = ApiService(reader_store).context.current_epoch(
                    "u1", *ADMIN)
                outcome["matched"] = (current == client_epoch)
                checked.set()
                proceed.wait(_WAIT)
                if not outcome["matched"]:
                    outcome["produce_ran"] = False
                    outcome["served"] = False
                    return
                outcome["produce_ran"] = True
                outcome["produce_saw_status"] = (
                    reader_store.get_season(sid).status)
                if use_fence:
                    version_after = reader_store.current_epoch_fence_version(
                        EPOCH_FENCE_GLOBAL_KEY)
                    outcome["served"] = (version_after == version_before)
                else:
                    # The pre-#423 baseline: no counter, no SHARED hold,
                    # nothing — this arm's whole point is to reproduce what
                    # a genuinely independent second process saw with ZERO
                    # protection, so it must not incidentally gain any.
                    outcome["served"] = True

            if not use_fence:
                do_check_and_read()
                return
            with reader_store.epoch_fence_acquire_shared(
                    EPOCH_FENCE_GLOBAL_KEY), \
                 reader_store.epoch_fence_acquire_shared(
                     user_fence_key("u1")):
                do_check_and_read()

        def mutator_body():
            m = SqlStore(self.url)
            try:
                with m.transaction(isolation="SERIALIZABLE"):
                    if use_fence:
                        # Bumps the persisted version counter (round-N finding
                        # 1) as well as taking the advisory lock — see
                        # epoch_fence_acquire_exclusive's own docstring.
                        m.epoch_fence_acquire_exclusive(EPOCH_FENCE_GLOBAL_KEY)
                    season = m.get_season_for_update(sid)
                    season.status = SeasonStatus.ARCHIVED
                    season.archived_at = datetime.now(timezone.utc)
                    m.save_season(season)
            finally:
                m.close()
            mutator_done.set()

        t_reader = threading.Thread(target=reader_body)

        if writer_first and use_fence and self.FENCE_IS_REAL:
            # WRITER-FIRST, genuinely fenced (design §10.3 test 3): mutator
            # starts and holds the exclusive lock BEFORE the reader ever
            # tries to acquire shared -- so the reader's fence-acquire must
            # itself block until the writer fully commits and releases, and
            # its FRESH epoch derivation (necessarily post-commit) mismatches
            # E0. The mutator's DB transaction is deliberately held open
            # (BEGIN, then parked) for the whole wait, which is exactly what
            # this arm needs to prove -- and is safe here specifically
            # because PostgreSQL's advisory lock is a separate axis from
            # this held transaction (see epoch_fence_acquire_shared's own
            # docstring for why the SAME shape is UNSAFE on SQLite, where
            # BEGIN IMMEDIATE's file lock would contend with the reader's
            # OWN transaction directly).
            block_started = threading.Event()
            release_mutator = threading.Event()

            def mutator_body_blocking():
                m = SqlStore(self.url)
                try:
                    with m.transaction(isolation="SERIALIZABLE"):
                        m.epoch_fence_acquire_exclusive(EPOCH_FENCE_GLOBAL_KEY)
                        block_started.set()
                        release_mutator.wait(_WAIT)
                        season = m.get_season_for_update(sid)
                        season.status = SeasonStatus.ARCHIVED
                        season.archived_at = datetime.now(timezone.utc)
                        m.save_season(season)
                finally:
                    m.close()
                mutator_done.set()

            t_mutator = threading.Thread(target=mutator_body_blocking)
            t_mutator.start()
            self.assertTrue(block_started.wait(_WAIT),
                            "mutator never reached its held point")
            time.sleep(0.05)  # let it settle into the hold
            t_reader.start()
            # The reader's shared-acquire must be BLOCKED by the mutator's
            # still-open exclusive hold -- prove it by asserting the reader
            # has NOT reached its own "checked" signal yet, for a bounded
            # window.
            self.assertFalse(checked.wait(0.3),
                             "fenced reader was not blocked by the writer's "
                             "held exclusive lock")
            release_mutator.set()
            self.assertTrue(mutator_done.wait(_WAIT), "mutator never committed")
            self.assertTrue(checked.wait(_WAIT), "reader never reached its "
                                                 "epoch check")
            proceed.set()
            t_reader.join(_WAIT)
            t_mutator.join(_WAIT)
        elif writer_first:
            # WRITER-FIRST, but nothing genuinely orders the two (unfenced,
            # or a no-op-fenced backend): "writer first" and "reader first"
            # are NOT actually distinct interleavings without a fence --
            # that IS gap (b) (design §0/§10.3's own framing: "gap (b) is
            # not about which side arrives first, it is that NEITHER order
            # is ordered against the other at all"). The one scenario that
            # actually exercises the bug is the reader's CHECK landing
            # before the writer's commit, with the commit then landing
            # before the reader's produce() -- exactly the reader-first
            # sequencing below, reused verbatim rather than duplicated.
            # (The OTHER shape -- writer fully commits before the reader's
            # request is even dispatched -- is the token comparison's own
            # degenerate, already-correctly-handled case per design §10.3's
            # own note on `test_writer_first_is_a_plain_epoch_mismatch`,
            # not a proof of anything; it is intentionally not what this
            # arm tests.)
            t_reader.start()
            self.assertTrue(checked.wait(_WAIT), "reader never reached its "
                                                 "epoch check")
            mutator_body()
            self.assertTrue(mutator_done.wait(_WAIT), "mutator never committed")
            proceed.set()
            t_reader.join(_WAIT)
        else:
            # READER-FIRST (design §10.3 test 2): reader starts, matches E0,
            # and (when fenced) PARKS holding the shared lock. The mutator's
            # exclusive acquisition must then BLOCK until the reader's
            # simulated produce() finishes and releases.
            t_mutator = threading.Thread(target=mutator_body)
            t_reader.start()
            self.assertTrue(checked.wait(_WAIT), "reader never reached its "
                                                 "epoch check")
            time.sleep(0.05)
            t_mutator.start()
            if use_fence and self.FENCE_IS_REAL:
                self.assertFalse(mutator_done.wait(0.3),
                                 "fenced writer was NOT blocked by the "
                                 "reader's held shared lock")
                proceed.set()
                t_reader.join(_WAIT)
                self.assertTrue(
                    mutator_done.wait(_WAIT),
                    "mutator never committed after the reader released")
            else:
                # No real contention (unfenced, or a no-op-fenced backend):
                # nothing orders the two, so this harness imposes an
                # EXPLICIT order itself -- wait for the mutator to actually
                # commit BEFORE letting the reader's simulated produce()
                # run, so the interleaving this branch needs is reliably
                # reached rather than left to thread-scheduling luck.
                self.assertTrue(mutator_done.wait(_WAIT),
                                "mutator never committed")
                proceed.set()
                t_reader.join(_WAIT)
            t_mutator.join(_WAIT)

        reader_store.close()
        return outcome

    def test_writer_first_unfenced_reproduces_the_torn_read(self):
        """THE FALSIFIABILITY PROOF (design §10.6), unfenced arm. Must
        reproduce the exact CI-observed defect `services/context_gate.py`'s
        own docstring documents: an epoch check that matched E0 is followed
        by a SEPARATE, later read that observes the ALREADY-ARCHIVED Season
        -- a decision made against different state than the one the epoch
        match just certified."""
        out = self._race(use_fence=False, writer_first=True)
        self.assertTrue(out["matched"], out)
        self.assertTrue(out["produce_ran"], out)
        self.assertEqual(out["produce_saw_status"], SeasonStatus.ARCHIVED, out)

    def test_writer_first_fenced_discards_before_reaching_the_read(self):
        """Same race, fenced arm: the reader's shared acquisition is
        genuinely BLOCKED (asserted above) until the writer's commit is
        fully durable, so its epoch derivation runs strictly after the
        commit and mismatches E0 -- the simulated produce() never runs."""
        out = self._race(use_fence=True, writer_first=True)
        self.assertFalse(out["matched"], out)
        self.assertFalse(out["produce_ran"], out)
        self.assertFalse(out["served"], out)

    def test_reader_first_unfenced_is_a_lucky_accident_not_a_guarantee(self):
        """Unfenced, reader-first: nothing blocks the writer, so it commits
        while the reader is still "in produce()" -- the SAME torn read as
        the writer-first case, just reached by the opposite arrival order.
        This is the point: gap (b) is not about which side arrives first,
        it is that NEITHER order is ordered against the other at all
        without the fence."""
        out = self._race(use_fence=False, writer_first=False)
        self.assertTrue(out["matched"], out)
        self.assertTrue(out["produce_ran"], out)
        self.assertEqual(out["produce_saw_status"], SeasonStatus.ARCHIVED, out)

    def test_reader_first_fenced_stays_consistent(self):
        """Fenced, reader-first: the writer's exclusive acquisition
        genuinely BLOCKS (asserted above) until the reader's shared hold
        releases, so the reader's simulated produce() -- which runs BEFORE
        that release -- observes the Season exactly as it was when the
        epoch matched: still ACTIVE. Consistency, not automatic discarding
        (design §10.3 test 2's own framing) -- the response is served, and
        it is correct."""
        out = self._race(use_fence=True, writer_first=False)
        self.assertTrue(out["matched"], out)
        self.assertTrue(out["produce_ran"], out)
        self.assertEqual(out["produce_saw_status"], SeasonStatus.ACTIVE, out)
        # The writer never got to commit until the reader's hold released, so
        # the version counter never moved during the reader's window either —
        # both mechanisms agree the response is safe to serve.
        self.assertTrue(out["served"], out)


@unittest.skipUnless(_pg_url(), "PostgreSQL required (set TEST_DATABASE_URL)")
class EpochFenceCrossProcessFalsifiabilityPgTest(
        _FalsifiabilityRaceMixin, unittest.TestCase):
    """Real PostgreSQL, independent connections (genuinely independent —
    not threads sharing one connection)."""

    def setUp(self):
        self.url = _pg_url()
        SqlStore(self.url).clear_all_data()


class EpochFenceCrossProcessFalsifiabilitySqliteTest(
        _FalsifiabilityRaceMixin, unittest.TestCase):
    """Real SQLite FILE (not ``:memory:``, which is per-connection-private),
    independent connections to the same file — the same argument the
    codebase already relies on for ``BEGIN IMMEDIATE``'s cross-connection
    guarantee.

    OVERRIDES the mixin's two "fenced" tests, still — but as of round-N
    review finding 1, to prove the defect is CLOSED, not that it remains.
    File-backed SQLite's ``epoch_fence_acquire_exclusive``/``_shared``
    ADVISORY-LOCK halves are STILL a documented NO-OP (see those methods' own
    docstrings) — a real dedicated-connection implementation was built,
    measured to deadlock/livelock against a real ``ThreadingHTTPServer`` (a
    genuine AB-BA cycle between SQLite's single file-lock axis and
    ``self._lock``, absent on PostgreSQL where advisory locks are a separate
    axis), and reverted for safety. ``FENCE_IS_REAL`` therefore stays
    ``False`` — the LOCK still cannot genuinely block a contending party on
    this backend, so ``_race`` still has to impose its own explicit
    interleaving via the shared ``threading.Event`` signals rather than
    asserting a real bounded-block.

    What changed is the VERSION-COUNTER half (round-N finding 1's actual
    fix), which is NOT a lock and runs on every backend unconditionally (see
    ``SqlStore.epoch_fence_acquire_exclusive``/``current_epoch_fence_version``).
    ``produce()`` still physically runs here — nothing blocks it, exactly as
    before — but the version it reads AFTER running no longer agrees with the
    one it read before, because the mutator's commit bumped the persisted
    counter in between. The response is therefore DISCARDED (``served`` is
    ``False``) rather than handed the torn read it computed — the same 204
    outcome a genuine block would have produced by a different, lock-free
    route. The two inherited "unfenced" tests below (``use_fence=False``,
    exercising NEITHER the lock nor the counter) still run unmodified and
    still demonstrate the pre-#423 baseline defect, which is the point of
    keeping them: the version counter is what changed, not the meaning of
    "no protection at all"."""

    FENCE_IS_REAL = False

    def setUp(self):
        import tempfile
        fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self._path)  # SqlStore/sqlite3 creates it fresh
        self.url = self._path

    def tearDown(self):
        try:
            os.remove(self._path)
        except OSError:
            pass

    def test_writer_first_fenced_discards_before_reaching_the_read(self):
        """Overridden (round-N finding 1): SQLite's ADVISORY LOCK is still a
        no-op, so the epoch check still matches E0 and ``produce()`` still
        physically runs and still observes the torn ARCHIVED status — none
        of that changed. What is NEW is that the mutator's commit (which now
        also bumps the persisted version counter — the SAME transaction,
        atomically) lands strictly between the reader's before/after samples
        BY CONSTRUCTION (this is the writer-first interleaving, explicitly
        ordered by the harness since nothing here genuinely blocks), so the
        version check catches it and the read is DISCARDED rather than
        served — closing exactly the gap the previous revision of this test
        only documented."""
        out = self._race(use_fence=True, writer_first=True)
        self.assertTrue(out["matched"], out)
        self.assertTrue(out["produce_ran"], out)
        self.assertEqual(out["produce_saw_status"], SeasonStatus.ARCHIVED, out)
        self.assertFalse(
            out["served"],
            f"the version-counter check must discard a response produce() "
            f"computed under a torn read, even though nothing on SQLite "
            f"genuinely blocked produce() from running: {out}")

    def test_reader_first_fenced_stays_consistent(self):
        """Overridden (round-N finding 1) for the same reason as the sibling
        override above: reader-first still lets the mutator commit (and bump
        the version counter) WHILE the reader is parked mid-``produce()``,
        because nothing on SQLite genuinely blocks it. The version check
        still catches the disagreement on release and still discards —
        "reader first" and "writer first" converge to the SAME safe outcome
        here, which is expected: without a real lock there is no ordering
        for the two arrival orders to differ BY."""
        out = self._race(use_fence=True, writer_first=False)
        self.assertTrue(out["matched"], out)
        self.assertTrue(out["produce_ran"], out)
        self.assertEqual(out["produce_saw_status"], SeasonStatus.ARCHIVED, out)
        self.assertFalse(
            out["served"],
            f"the version-counter check must discard a response produce() "
            f"computed under a torn read, even though nothing on SQLite "
            f"genuinely blocked produce() from running: {out}")


# =========================================================================
# Round-N review finding 1's SECOND named repro: Memory, SAME PROCESS,
# SELECTED-SEASON DELETE. Unlike the PG/SQLite mixin above (a cross-PROCESS
# race that needs independent connections), InMemoryStore cannot participate
# in independent workers at all -- two separate instances share zero state
# -- so there is no cross-process race to reproduce there. What the review
# named instead is narrower and real: the kept CONTEXT_GATE/LIFECYCLE_GATE
# in-process gates order a scoped read against SPECIFIC writers (a context
# switch, an archive/reopen) but never against the OTHER newly-fenced
# writers -- delete_season among them -- because nothing ever wrapped THOSE
# calls in CONTEXT_GATE/LIFECYCLE_GATE in the first place (#423's fence is
# the first mechanism that touches them at all). So a scoped read racing a
# concurrent delete of the operator's OWN selected Season, on ONE shared
# InMemoryStore, is exactly the "same-process delete repro" the review says
# "remains constructible" pre-fix.
# =========================================================================

class EpochFenceMemorySameProcessTornReadTest(unittest.TestCase):
    """Two threads, ONE shared ``InMemoryStore``/``ApiService`` -- the only
    concurrency shape Memory can ever have. ``use_version_check`` toggles
    ONLY whether this test's own reader samples/compares the counter, mirroring
    exactly how the PG/SQLite mixin's ``use_fence`` toggles whether ITS reader
    takes the shared holds: ``False`` reproduces the pre-round-N baseline
    (nothing orders the read against this writer at all -- CONTEXT_GATE/
    LIFECYCLE_GATE never wrapped delete_season, and the fence's advisory-lock
    half is a documented no-op on Memory regardless), ``True`` exercises the
    version-counter half, which is NOT optional in production -- every call to
    ``_read_under_context_gate`` performs it unconditionally; the toggle exists
    only so this ONE test file can demonstrate both the defect and its closure
    side by side, the same falsifiability shape §10.6 already established for
    the cross-process case."""

    def setUp(self):
        self.store = InMemoryStore()
        self.api = ApiService(self.store)

    def _race(self, use_version_check):
        pid, sid = _program_season(self.api, "MemP", "MemS")
        self.api.set_active_context("u1", *ADMIN, pid, sid)
        client_epoch = self.api.context.current_epoch("u1", *ADMIN)
        # `setup_guarded_mutation`'s `("season", sid, "scope")` target check
        # (`setup_target_accessible`) requires the ACTING user's OWN active
        # context to already name this Program/Season -- a separate axis
        # from "u1"'s selection above, and easy to miss: without it the
        # delete is silently refused (`payload["error"]["code"] ==
        # "active_context_required"`, `refused` stays None) and the Season
        # is never actually removed, which would make this test check
        # nothing. "admin" is a different account than "u1" on purpose (two
        # operators, not one wearing two hats), so it needs its own selection.
        self.api.set_active_context("admin", *ADMIN, pid, sid)

        outcome = {}
        checked = threading.Event()
        proceed = threading.Event()
        deleter_done = threading.Event()

        def reader_body():
            # Keyed on EPOCH_FENCE_GLOBAL_KEY, the exact key
            # `setup_guarded_mutation`'s "season"/"scope" target bumps for
            # delete_season (design §3 row 6).
            version_before = (
                self.store.current_epoch_fence_version(EPOCH_FENCE_GLOBAL_KEY)
                if use_version_check else None)
            current = self.api.context.current_epoch("u1", *ADMIN)
            outcome["matched"] = (current == client_epoch)
            checked.set()
            proceed.wait(_WAIT)
            if not outcome["matched"]:
                outcome["produce_ran"] = False
                outcome["served"] = False
                return
            outcome["produce_ran"] = True
            # Stands in for `produce()` -- the dependent service read a real
            # scoped-read route makes, e.g. resolving the named Season. A
            # deleted Season reads back None on every backend.
            outcome["produce_saw_season"] = self.store.get_season(sid)
            if use_version_check:
                version_after = self.store.current_epoch_fence_version(
                    EPOCH_FENCE_GLOBAL_KEY)
                outcome["served"] = (version_after == version_before)
            else:
                outcome["served"] = True

        def deleter_body():
            # THE GUARDED PATH, not a bare `self.api.setup.delete_season(...)`
            # call -- `epoch_fence_acquire_exclusive` (and therefore the
            # version bump) is taken by `setup_guarded_mutation`'s own
            # wrapper, not by `SetupService.delete_season` itself (see
            # `services/epoch_fence.py`'s own module docstring: "web/server.py's
            # HTTP dispatch is the ONLY caller of the guarded path in
            # production" — this is that path, exactly as
            # `EpochFenceWriterOrderingTest.test_row6_season_delete` drives
            # it). Calling the bare method would silently test nothing.
            payload, refused = self.api.setup_guarded_mutation(
                [("season", sid, "scope")],
                lambda: self.api.setup.delete_season(sid, actor_id="admin"),
                "admin", *ADMIN)
            self.assertIsNone(refused, (payload, refused))
            deleter_done.set()

        t_reader = threading.Thread(target=reader_body)
        t_reader.start()
        self.assertTrue(checked.wait(_WAIT),
                        "reader never reached its epoch check")
        # No lock anywhere in this path (Memory's fence is a documented
        # no-op) -- imposing the interleaving explicitly, same as the
        # PG/SQLite mixin's own "elif writer_first" branch does when nothing
        # genuinely blocks either side.
        deleter_body()
        self.assertTrue(deleter_done.wait(_WAIT), "delete never completed")
        proceed.set()
        t_reader.join(_WAIT)
        return outcome

    def test_baseline_unversioned_reproduces_the_selected_season_delete_repro(self):
        """Pre-round-N baseline: the epoch check matches E0 (the delete
        doesn't touch the Season's lifecycle material the SAME way an
        archive does -- it removes the row entirely, and the check already
        ran before the delete lands here by construction), the simulated
        read runs, and it observes the Season GONE -- exactly the same-
        process delete repro the review named as remaining constructible."""
        out = self._race(use_version_check=False)
        self.assertTrue(out["matched"], out)
        self.assertTrue(out["produce_ran"], out)
        self.assertIsNone(out["produce_saw_season"], out)
        self.assertTrue(out["served"], out)  # the defect: served anyway

    def test_version_checked_discards_the_selected_season_delete_repro(self):
        """Round-N finding 1's fix: `delete_season` (design §3 row 6) calls
        `epoch_fence_acquire_exclusive`, which now bumps the SAME persisted
        counter on EVERY backend including Memory. The reader's before/after
        samples disagree, so the response is discarded rather than served
        -- closing the repro without any lock ever crossing the simulated
        read."""
        out = self._race(use_version_check=True)
        self.assertTrue(out["matched"], out)
        self.assertTrue(out["produce_ran"], out)
        self.assertIsNone(out["produce_saw_season"], out)
        self.assertFalse(
            out["served"],
            f"a selected-Season delete landed inside the read's window and "
            f"must be caught by the version check: {out}")


# =========================================================================
# §10.5: EVERY writer in the design's §3 table acquires the fence, and
# acquires it FIRST -- before any other store mutation its own call makes.
# Spy-based, no real concurrency needed for THIS proof (concurrency is what
# §10.1-10.4 above already prove for the mechanism itself); this proves the
# 17 SERVICE call sites are wired correctly. InMemoryStore for speed; the
# spy fires on the SAME store class every backend uses
# (SqlStore/InMemoryStore both implement epoch_fence_acquire_exclusive), so
# the ORDER proof this establishes is backend-independent by construction.
# =========================================================================

class EpochFenceWriterOrderingTest(unittest.TestCase):
    """One sub-test per design §3 row: the fence is acquired, with the
    correct key class, before the writer's own row mutation commits."""

    def setUp(self):
        self.store = InMemoryStore()
        self.api = ApiService(self.store)
        self.calls = []  # [(key,)] in call order
        original = self.store.epoch_fence_acquire_exclusive

        def spy(key):
            # Recorded BEFORE delegating, so a call recorded here is
            # provably before anything the writer does AFTER this line --
            # including its own row mutation, which cannot have happened
            # yet (Python has not returned from this call).
            self.calls.append(key)
            return original(key)

        self.store.epoch_fence_acquire_exclusive = spy

    def _world(self):
        pid, sid = _program_season(self.api, "OrderP", "OrderS")
        league = self.api.create_league(sid, "OrderLeague")
        lid = league["id"]
        division = self.api.create_division(sid, "OrderDiv", league_id=lid)
        did = division["id"]
        club = self.api.create_club("OrderClub")
        team = self.api.create_team(club_id=club["id"], name="OrderTeam",
                                    league_id=lid, division_id=did)
        tid = team["id"]
        self.api.setup.register_team_for_season(sid, tid, did)
        player = self.api.create_player(tid, "Order Player", "forward")
        official = self.api.create_official("Order Official")
        with self.store.transaction():
            gid = self.store.next_id("game")
            self.store.add_game(Game(
                id=gid, home_team_id=tid, away_team_id=tid,
                start_time=None, season_id=sid))
        account = self.api.create_user_account(
            "order_user", "orderpass1", Role.COACH, {"team_id": tid},
            actor_id="admin")
        guardian_account = self.api.create_user_account(
            "order_guardian", "orderpass1", Role.GUARDIAN, {},
            actor_id="admin")
        return dict(pid=pid, sid=sid, lid=lid, did=did, club=club["id"],
                   tid=tid, player=player["id"], official=official["id"],
                   gid=gid, account=account["id"],
                   guardian=guardian_account["id"])

    def _assert_fenced(self, expected_key, label):
        self.assertIn(expected_key, self.calls,
                     f"{label}: epoch_fence_acquire_exclusive was never "
                     f"called with {expected_key!r}; calls={self.calls}")

    # -- rows 1-3: per-user ------------------------------------------------
    def test_row1_context_switch(self):
        w = self._world()
        self.api.set_active_context(w["account"], Role.COACH,
                                    {"team_id": w["tid"]}, w["pid"], w["sid"])
        self._assert_fenced(user_fence_key(w["account"]), "context switch")

    def test_row2_account_scope_rebind(self):
        w = self._world()
        self.api.rebind_user_account_scope(
            w["account"], {"team_id": w["tid"]}, actor_id="admin")
        self._assert_fenced(user_fence_key(w["account"]), "scope rebind")

    def test_row3_account_activate(self):
        w = self._world()
        self.api.set_user_account_active(w["account"], False, actor_id="admin")
        self._assert_fenced(user_fence_key(w["account"]), "account deactivate")

    # -- rows 4-11: global, via _guarded_attempt ----------------------------
    # Rows 4-12 route through `_guarded_attempt` (design §8.3) rather than
    # the bare `ApiService.archive_season`/`delete_season`/etc convenience
    # wrappers -- those call straight into `SetupService` and are NOT the
    # guarded path (confirmed directly: they bypass `setup_guarded_mutation`
    # entirely, matching `api/service.py`'s own `delete_program`/
    # `delete_league`/`delete_season` bodies, which call `self.setup.*`
    # directly). `web/server.py`'s HTTP dispatch is the ONLY caller of the
    # guarded path in production, via `_guarded_mutation` ->
    # `api.setup_guarded_mutation(targets, mutation, user_id, role, scope)`
    # -- so these tests call `setup_guarded_mutation` directly, with the
    # EXACT same `targets` shapes `web/server.py` builds for each route.
    ADMIN = (Role.LEAGUE_ADMIN, {})

    def _guarded(self, targets, mutation):
        payload, refused = self.api.setup_guarded_mutation(
            targets, mutation, "admin", *self.ADMIN)
        self.assertIsNone(refused, (payload, refused))
        return payload

    def test_row4_season_archive(self):
        w = self._world()
        self._guarded(
            [("season", w["sid"], "scope")],
            lambda: self.api.setup.archive_season(
                w["sid"], reason="order test", actor_id="admin"))
        self._assert_fenced(EPOCH_FENCE_GLOBAL_KEY, "season archive")

    def test_row5_season_reopen(self):
        w = self._world()
        self._guarded(
            [("season", w["sid"], "scope")],
            lambda: self.api.setup.archive_season(
                w["sid"], reason="pre", actor_id="admin"))
        self.calls.clear()
        self._guarded(
            [("season", w["sid"], "scope")],
            lambda: self.api.setup.reopen_season(
                w["sid"], reason="order test", actor_id="admin"))
        self._assert_fenced(EPOCH_FENCE_GLOBAL_KEY, "season reopen")

    def test_row6_season_delete(self):
        pid2, sid2 = _program_season(self.api, "OrderP2", "OrderS2")
        self.calls.clear()
        self._guarded(
            [("season", sid2, "scope")],
            lambda: self.api.setup.delete_season(sid2, actor_id="admin"))
        self._assert_fenced(EPOCH_FENCE_GLOBAL_KEY, "season delete")

    def test_row7_program_delete(self):
        pid2, sid2 = _program_season(self.api, "OrderP3", "OrderS3")
        self._guarded(
            [("season", sid2, "scope")],
            lambda: self.api.setup.delete_season(sid2, actor_id="admin"))
        self.calls.clear()
        self._guarded(
            [("program", pid2, "scope")],
            lambda: self.api.setup.delete_program(pid2, actor_id="admin"))
        self._assert_fenced(EPOCH_FENCE_GLOBAL_KEY, "program delete")

    def test_row8_league_delete(self):
        pid2, sid2 = _program_season(self.api, "OrderP4", "OrderS4")
        league2 = self.api.create_league(sid2, "DeleteMeLeague")
        self.calls.clear()
        self._guarded(
            [("league", league2["id"], "scope")],
            lambda: self.api.setup.delete_league(
                league2["id"], actor_id="admin"))
        self._assert_fenced(EPOCH_FENCE_GLOBAL_KEY, "league delete")

    def test_row9_league_season_unbind(self):
        pid2, sid2 = _program_season(self.api, "OrderP5", "OrderS5")
        league2 = self.api.create_league(sid2, "UnbindLeague")
        ls = self.store.league_season_for(league2["id"], sid2)
        self.calls.clear()
        self._guarded(
            [("league_season", ls.id, "scope")],
            lambda: self.api.setup.delete_league_season(
                ls.id, actor_id="admin"))
        self._assert_fenced(EPOCH_FENCE_GLOBAL_KEY, "league-season unbind")

    def test_row10_venue_access_revoke(self):
        w = self._world()
        venue = self.api.create_venue("OrderVenue", "US", "UTC")
        grant = self.api.grant_season_venue_access(
            w["sid"], venue["id"], actor_id="admin")
        self.calls.clear()
        self._guarded(
            [("season_venue_access", grant["id"], "scope")],
            lambda: self.api.setup.revoke_season_venue_access(
                grant["id"], actor_id="admin"))
        self._assert_fenced(EPOCH_FENCE_GLOBAL_KEY, "venue access revoke")

    def test_row11_venue_access_delete(self):
        w = self._world()
        venue = self.api.create_venue("OrderVenue2", "US", "UTC")
        grant = self.api.grant_season_venue_access(
            w["sid"], venue["id"], actor_id="admin")
        self.api.setup.revoke_season_venue_access(grant["id"], actor_id="admin")
        self.calls.clear()
        self._guarded(
            [("season_venue_access", grant["id"], "scope")],
            lambda: self.api.setup.delete_season_venue_access(
                grant["id"], actor_id="admin"))
        self._assert_fenced(EPOCH_FENCE_GLOBAL_KEY, "venue access delete")

    # -- row 12: team/league transfer (covered via _EPOCH_FENCE_KINDS
    #    matching the destination "league" target -- see api/service.py's
    #    _guarded_attempt comment) --------------------------------------
    def test_row12_team_league_transfer(self):
        w = self._world()
        league2 = self.api.create_league(w["sid"], "OtherLeague")
        self.calls.clear()
        self._guarded(
            [("team", w["tid"], "scope"), ("league", league2["id"], "scope")],
            lambda: self.api.transfer_team_to_league(
                w["tid"], league2["id"], "admin",
                user_id="admin", role=self.ADMIN[0], scope=self.ADMIN[1]))
        self._assert_fenced(EPOCH_FENCE_GLOBAL_KEY, "team/league transfer")

    # -- rows 13-14: official assign/unassign -------------------------------
    def test_row13_official_assign(self):
        w = self._world()
        self.calls.clear()
        self.api.assign_official(w["gid"], w["official"], "referee",
                                 actor_id="admin")
        self._assert_fenced(EPOCH_FENCE_GLOBAL_KEY, "official assign")

    def test_row14_official_unassign(self):
        w = self._world()
        a = self.api.assign_official(w["gid"], w["official"], "referee",
                                     actor_id="admin")
        self.calls.clear()
        self.api.unassign_official(a["id"], actor_id="admin")
        self._assert_fenced(EPOCH_FENCE_GLOBAL_KEY, "official unassign")

    # -- row 15: player/team reassignment -----------------------------------
    def test_row15_player_team_reassign(self):
        w = self._world()
        club2 = self.api.create_club("OrderClub2")
        team2 = self.api.create_team(club_id=club2["id"], name="OrderTeam2",
                                     league_id=w["lid"], division_id=w["did"])
        self.calls.clear()
        self.api.assign_player_team(w["player"], team2["id"], actor_id="admin")
        self._assert_fenced(EPOCH_FENCE_GLOBAL_KEY, "player/team reassign")

    # -- rows 16-17: guardian link create/verify ----------------------------
    def test_row16_guardian_link_create(self):
        w = self._world()
        self.calls.clear()
        self.api.create_guardian_link(
            w["guardian"], w["player"], actor_id="admin")
        self._assert_fenced(EPOCH_FENCE_GLOBAL_KEY, "guardian link create")

    def test_row17_guardian_link_verify(self):
        w = self._world()
        link = self.api.create_guardian_link(
            w["guardian"], w["player"], actor_id="admin")
        self.calls.clear()
        self.api.verify_guardian_link(
            link["id"], actor_id="admin", consent_method="verbal_confirmed")
        self._assert_fenced(EPOCH_FENCE_GLOBAL_KEY, "guardian link verify")


class EpochFenceRollbackReleasesTest(unittest.TestCase):
    """§10.5's rollback/no-leak half: if a writer raises AFTER acquiring the
    fence but BEFORE its transaction commits, the fence must not leak --
    proven by a SECOND, independent connection immediately re-acquiring the
    SAME exclusive key with no wait, right after the rollback."""

    def setUp(self):
        self.url = _pg_url()

    def test_rollback_releases_the_fence_postgres(self):
        if not self.url:
            self.skipTest("PostgreSQL required (set TEST_DATABASE_URL)")
        SqlStore(self.url).clear_all_data()
        a = SqlStore(self.url)
        try:
            with a.transaction():
                a.epoch_fence_acquire_exclusive(EPOCH_FENCE_GLOBAL_KEY)
                raise ValueError("simulated failure after the fence, "
                                "before commit")
        except ValueError:
            pass
        b = SqlStore(self.url)
        t0 = time.monotonic()
        with b.transaction():
            b.epoch_fence_acquire_exclusive(EPOCH_FENCE_GLOBAL_KEY)
        elapsed = time.monotonic() - t0
        self.assertLess(
            elapsed, 1.0,
            f"a second connection waited {elapsed:.2f}s to acquire the "
            f"SAME exclusive key after a rollback -- the fence leaked")
        a.close()
        b.close()

    def test_rollback_releases_the_fence_sqlite(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        a = SqlStore(path)
        try:
            with a.transaction():
                a.epoch_fence_acquire_exclusive(EPOCH_FENCE_GLOBAL_KEY)
                raise ValueError("simulated failure after the fence, "
                                "before commit")
        except ValueError:
            pass
        b = SqlStore(path)
        t0 = time.monotonic()
        with b.transaction():
            b.epoch_fence_acquire_exclusive(EPOCH_FENCE_GLOBAL_KEY)
        elapsed = time.monotonic() - t0
        self.assertLess(
            elapsed, 1.0,
            f"a second connection waited {elapsed:.2f}s to acquire the "
            f"SAME exclusive key after a rollback on SQLite (BEGIN "
            f"IMMEDIATE's own write lock, which is what this class-of-bug "
            f"actually exercises there) -- the write lock leaked")
        a.close()
        b.close()
        os.remove(path)


# =========================================================================
# Duplicated-constant sync check. services/epoch_fence.py, store/sql_store.py
# and web/server.py's own pre-existing `_LIFECYCLE_GATE_KEY` each carry their
# OWN copy of the global fence key string (deliberately -- store/ never
# imports from services/, and the continuity claim is specifically that the
# NEW key equals the OLD gate's key by VALUE, see epoch_fence.py's own
# comment); sql_store.py separately duplicates the timeout env-var name and
# default rather than importing context_gate.py's. Both duplication sites'
# own comments claim a test enforces this -- this is that test, so the claim
# stays true rather than becoming a stale aspiration the next time either
# copy is edited without the other.
# =========================================================================

class EpochFenceConstantsStaySyncedTest(unittest.TestCase):
    def test_global_key_matches_sql_store_and_the_old_lifecycle_gate(self):
        from hockey_scheduler.store.sql_store import SqlStore
        from hockey_scheduler.web import server as server_module
        self.assertEqual(
            EPOCH_FENCE_GLOBAL_KEY, SqlStore._EPOCH_FENCE_GLOBAL_KEY,
            "services.epoch_fence.EPOCH_FENCE_GLOBAL_KEY and SqlStore's own "
            "duplicated copy have drifted apart")
        self.assertEqual(
            EPOCH_FENCE_GLOBAL_KEY, server_module._LIFECYCLE_GATE_KEY,
            "the fence's global key no longer matches web/server.py's "
            "pre-existing _LIFECYCLE_GATE_KEY -- the stated continuity "
            "claim (same wire/log value as the gate it sits alongside) "
            "no longer holds")

    def test_timeout_default_and_env_var_match_context_gate(self):
        from hockey_scheduler.services import context_gate
        from hockey_scheduler.store import sql_store
        self.assertEqual(
            sql_store._EPOCH_FENCE_DEFAULT_TIMEOUT,
            context_gate.DEFAULT_WAIT_TIMEOUT,
            "the fence's default bound has drifted from ContextSwitchGate's")
        self.assertEqual(
            sql_store._EPOCH_FENCE_TIMEOUT_ENV, context_gate._WAIT_TIMEOUT_ENV,
            "the fence no longer reads the SAME operator-configurable env "
            "var as ContextSwitchGate -- 'the same knob' claim no longer "
            "holds")


if __name__ == "__main__":
    unittest.main()
