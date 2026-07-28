"""Concurrency for the #364 server-owned canonical League context.

`ContextService.set_with_league` no longer commits what the browser sent: when a
Season AND a League are both supplied, `_canonical_league_season` resolves the
canonical `(Program, Season, League)` tuple — keep the current Season when it
genuinely binds, else move to the League's single valid authorized ACTIVE
binding, else refuse with zero mutation. That resolution reads bindings and
authorization and then WRITES, all inside one serializable snapshot. This file
proves the window between "resolved" and "written" is not observable:

* **unbind-vs-set** — a `LeagueSeason` is deleted while a selection is resolving
  through it. Every interleaving lands on one of exactly two consistent states:
  a clean refusal with the prior context byte-identical, or a commit of the
  canonical pair that genuinely had a binding behind it. The requested-but-never
  -bound pair is the failure this exists to catch — it is what a "persist what
  was asked for" implementation would leave behind.
* **revocation-vs-set** — a scoped Coach's entitlement is revoked mid-flight
  (Team transfers to another League, and to another Program). Whatever commits,
  what the caller can then RESOLVE is wholly inside their post-change scope; the
  saved row is never rewritten, so restoring the entitlement restores the choice.
* **competing context writes** — two concurrent `set_with_league` calls for one
  user, whose canonical tuples differ in BOTH axes, leave exactly one row that
  is one whole canonical tuple — never one call's Season with the other's League.

Interleavings that are genuinely non-deterministic are asserted as INVARIANTS
(the set of consistent outcomes, plus `_RaceAssertions._assert_consistent` on
every path) rather than as a specific winner, so nothing here is timing-flaky.
The one interleaving that matters most — a selection whose authorization
snapshot is already fixed, racing a commit — is pinned deterministically with
`_pause_after_league_snapshot`, the League-axis twin of
`test_active_context._pause_after_program_snapshot`.

Memory/SQLite serialize every transaction on a process-wide lock, so both
threads share one store there; PostgreSQL must not share a connection across
threads, so `_Backend.connect()` hands each thread its own. PostgreSQL is
SKIPPED entirely unless TEST_DATABASE_URL is set, exactly as the existing
`_backends()` helpers do.
"""

import os
import threading
import time
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api.service import ApiService
from hockey_scheduler.domain import Role
from hockey_scheduler.domain.errors import ConcurrencyConflictError
from hockey_scheduler.services import context_scope
from hockey_scheduler.services.context_service import ContextService
from hockey_scheduler.store import InMemoryStore, SqlStore

ADMIN = (Role.LEAGUE_ADMIN, {})


class _Backend:
    """One backend under test, plus the right way for a THREAD to reach it.

    Memory/SQLite ARE their concurrency model: a process-wide transaction lock
    fully serializes every transaction, and an in-memory SQLite database only
    exists on its own connection — so both threads share the single store and
    ``connect()`` hands back that same object. PostgreSQL is genuinely
    multi-connection and a psycopg connection is not safe to drive from two
    threads at once, so ``connect()`` opens a fresh one per caller and
    ``close()`` disposes of everything this backend handed out.
    """

    def __init__(self, label, store, url=None):
        self.label = label
        self.store = store          # the main thread's seed/read store
        self.url = url
        self._opened = []

    def connect(self):
        if self.url is None:
            return self.store
        extra = SqlStore(self.url)
        self._opened.append(extra)
        return extra

    def close(self):
        for store in [*self._opened, self.store]:
            if isinstance(store, SqlStore):
                store.close()


def _backends():
    yield _Backend("memory", InMemoryStore())
    yield _Backend("sqlite", SqlStore(":memory:"))
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        SqlStore(url).clear_all_data()
        yield _Backend("postgres", SqlStore(url), url)


# -- scenario builders (same conventions as test_active_context_league) ------
def _program_season_league(api, pname="P1", sname="S1", lname="Gold"):
    """A Program + Season + a League BOUND to that Season. Returns ids."""
    pid = api.create_program(pname, "US", "UTC")["id"]
    sid = api.create_season(pid, sname)["id"]
    lid = api.create_league(sid, lname)["id"]
    return pid, sid, lid


def _registered_team(api, sid, lid, tname="Alpha"):
    """A Club+Team permanently in League ``lid`` and registered in Season ``sid``."""
    did = api.create_division(sid, "D1", league_id=lid)["id"]
    club = api.create_club("Club")["id"]
    team = api.create_team(club_id=club, name=tname, league_id=lid,
                           division_id=did)["id"]
    api.setup.register_team_for_season(sid, team, did)
    return team


def _unbind(store, league_id, season_id):
    """Remove the LeagueSeason binding directly (the store-level effect of an
    authorized unbind), leaving the permanent League itself intact."""
    ls = store.league_season_for(league_id, season_id)
    if ls is not None:
        with store.transaction():
            store.delete_league_season(ls.id)


def _transfer_team(store, team_id, league_id, program_id=None):
    """Move a Team to another permanent League (and optionally another Program)
    in ONE transaction — the scope-revoking write a scoped Coach races against."""
    with store.transaction():
        team = store.get_team(team_id)
        team.league_id = league_id
        if program_id is not None:
            team.program_id = program_id
        store.save_team(team)


def _row_count(store):
    if isinstance(store, SqlStore):
        cur = store.conn.cursor()   # sqlite3 cursor isn't a context manager
        cur.execute("SELECT COUNT(*) AS c FROM user_active_context")
        return cur.fetchone()["c"]
    return len(store.user_active_context)


def _reason(exc):
    return getattr(exc, "details", {}).get("reason")


def _pause_after_league_snapshot():
    """Patch ``context_scope.authorized_league_ids`` so the FIRST caller pauses
    right after it returns — i.e. inside ``_canonical_league_season``, with the
    League authorization snapshot already fixed, no binding read yet and nothing
    written. The League-axis twin of
    ``test_active_context._pause_after_program_snapshot``.

    Returns ``(paused, release, restore)``; the patch is armed exactly once, so
    a snapshot RETRY (PostgreSQL) re-reads normally instead of deadlocking."""
    cs = context_scope
    orig = cs.authorized_league_ids
    paused = threading.Event()
    release = threading.Event()
    armed = {"v": True}

    def barrier(*a, **k):
        result = orig(*a, **k)
        if armed["v"]:
            armed["v"] = False
            paused.set()
            release.wait(20)
        return result

    cs.authorized_league_ids = barrier
    return paused, release, (lambda: setattr(cs, "authorized_league_ids", orig))


class _RaceAssertions:
    """The consistency check EVERY race in this file makes, on every path."""

    def _run_pinned(self, setter, mutator, label):
        """Run ``setter`` until its League authorization snapshot is fixed, then
        commit ``mutator`` against it and let the setter finish. On Memory/SQLite
        the mutator blocks on the process lock the setter still holds, so the
        setter commits first and the mutation applies strictly after; on
        PostgreSQL the mutator commits immediately and the setter either still
        commits or retries into the post-change snapshot. Both are legitimate,
        which is why callers assert the outcome SET, not a winner."""
        paused, release, restore = _pause_after_league_snapshot()
        out = {}

        def run_setter():
            try:
                out["result"] = setter()
            except Exception as exc:                       # noqa: BLE001
                out["error"] = exc

        ts = threading.Thread(target=run_setter)
        tm = threading.Thread(target=mutator)
        try:
            ts.start()
            self.assertTrue(paused.wait(20),
                            f"{label}: the setter never fixed its snapshot")
            tm.start()          # contends with the in-flight selection
            release.set()
            ts.join(30)
            tm.join(30)
        finally:
            restore()
        self.assertFalse(ts.is_alive(), f"{label}: setter thread hung")
        self.assertFalse(tm.is_alive(), f"{label}: mutator thread hung")
        return out

    def _run_barrier(self, setter, mutator, label, mutator_delay=0.0):
        """The uncontrolled form: both threads released together. The ordering is
        genuinely unspecified, so only INVARIANTS may be asserted about it.

        ``mutator_delay`` nudges (never guarantees) the mutator behind the
        setter. Released together, the mutator reliably reaches the lock first
        on Memory/SQLite — so callers alternate the delay across iterations to
        sample the other side of the race as well. It is a sampling knob, not a
        synchronization primitive: no assertion may depend on it, which is why
        the deterministic interleaving lives in ``_run_pinned`` instead."""
        barrier = threading.Barrier(2)
        out = {}

        def run_setter():
            try:
                barrier.wait(20)
                out["result"] = setter()
            except Exception as exc:                       # noqa: BLE001
                out["error"] = exc

        def run_mutator():
            barrier.wait(20)
            if mutator_delay:
                time.sleep(mutator_delay)
            mutator()

        threads = [threading.Thread(target=run_setter),
                   threading.Thread(target=run_mutator)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        for t in threads:
            self.assertFalse(t.is_alive(), f"{label}: race thread hung")
        return out

    def _assert_consistent(self, store, svc, user, principal, label):
        """The persisted row and what it RESOLVES to never contradict each other.

        Read back through the real boundary (`store.get_active_context` +
        `resolve_with_league`), because "the call returned a good tuple" is not
        the property under test — "the row left behind is a good tuple" is.
        A resolved League must be the persisted one, sit in the resolved
        Program, be inside the caller's CURRENT scope, and — when a Season is
        resolved with it — still have a real binding behind it.
        """
        row = store.get_active_context(user)
        program, season, league = svc.resolve_with_league(user, *principal)
        if row is not None and row.league_id is not None:
            self.assertIsNotNone(
                row.program_id,
                f"{label}: a League persisted without a Program")
        if season is not None:
            self.assertIsNotNone(program, f"{label}: Season without Program")
            self.assertEqual(season.program_id, program.id,
                             f"{label}: resolved a cross-Program Season")
        if league is not None:
            self.assertIsNotNone(program, f"{label}: League without Program")
            self.assertEqual(league.program_id, program.id,
                             f"{label}: resolved a cross-Program League")
            self.assertEqual(row.league_id, league.id,
                             f"{label}: resolved a League the row does not name")
            self.assertEqual(
                row.season_id, season.id if season is not None else None,
                f"{label}: resolved a League against another Season")
            if season is not None:
                self.assertIsNotNone(
                    store.league_season_for(league.id, season.id),
                    f"{label}: resolved a League with no binding behind it")
            self.assertIn(
                league.id,
                context_scope.authorized_league_ids(
                    store, *principal, program.id, user,
                    season_id=season.id if season is not None else None),
                f"{label}: resolved a League outside the caller's scope")
        return row, (program, season, league)

    def _pair(self, row):
        return (row.season_id, row.league_id) if row is not None else None

    def _triple(self, row):
        return ((row.program_id, row.season_id, row.league_id)
                if row is not None else None)


class LeagueContextUnbindRaceTest(_RaceAssertions, unittest.TestCase):
    """A LeagueSeason deleted concurrently with a selection resolving THROUGH it.

    World: League ``Gold`` is bound only to ``S1``; the operator is working in
    ``S2`` (active, no binding) and selects Gold, so `_canonical_league_season`
    rule 2 must move the context to ``S1``. The unbind removes the one binding
    that move depends on. Only two states are consistent afterwards:

    * ``(S2, None)`` — the untouched prior context, i.e. the selection refused
      and mutated nothing; or
    * ``(S1, Gold)`` — the canonical tuple, which had a real binding behind it
      when it committed.

    ``(S2, Gold)`` — the pair the browser actually asked for — is the failure
    mode: a persisted Season+League with no binding that ever existed.
    """

    def _world(self, api, suffix=""):
        pid, s1, gold = _program_season_league(
            api, f"P{suffix}", f"S1{suffix}", f"Gold{suffix}")
        s2 = api.create_season(pid, f"S2{suffix}")["id"]
        return pid, s1, s2, gold

    def _assert_outcome(self, store, svc, user, out, ids, label):
        pid, s1, s2, gold = ids
        row, (_program, season, league) = self._assert_consistent(
            store, svc, user, ADMIN, label)
        if "error" in out:
            self.assertEqual(_reason(out["error"]), "league_not_accessible",
                             f"{label}: {out['error']!r}")
        self.assertIn(
            self._triple(row), ((pid, s2, None), (pid, s1, gold)),
            f"{label}: persisted a tuple that was never canonical: "
            f"{self._triple(row)}")
        if self._pair(row) == (s1, gold):
            # It committed, so the pair it committed is the one the binding
            # existed for -- and it may only still RESOLVE while that binding
            # is genuinely there.
            if store.league_season_for(gold, s1) is None:
                self.assertIsNone(
                    league, f"{label}: resolved an unbound League")
            else:
                self.assertEqual((season.id, league.id), (s1, gold), label)

    def test_pinned_unbind_against_a_fixed_snapshot_stays_canonical(self):
        """The hard interleaving, forced: the selection has already fixed its
        League authorization snapshot when the unbind commits."""
        for backend in _backends():
            with self.subTest(backend=backend.label):
                store = backend.store
                api = ApiService(store)
                ids = self._world(api)
                pid, s1, s2, gold = ids
                svc = ContextService(store)
                svc.set("u1", *ADMIN, pid, s2)       # known-good prior context
                unbind_store = backend.connect()

                out = self._run_pinned(
                    lambda: svc.set_with_league("u1", *ADMIN, pid, s2, gold),
                    lambda: _unbind(unbind_store, gold, s1),
                    backend.label)

                self._assert_outcome(store, svc, "u1", out, ids, backend.label)
                # Whoever won, the unbind is applied by now: the binding the
                # selection needed is gone, and resolution must not hand back a
                # League that no longer has one.
                self.assertIsNone(store.league_season_for(gold, s1),
                                  f"{backend.label}: the unbind never applied")
                self.assertIsNone(svc.resolve_with_league("u1", *ADMIN)[2],
                                  f"{backend.label}: resolved an unbound League")
                backend.close()

    def test_unbind_committed_first_refuses_and_leaves_the_prior_context(self):
        """The other ordering, controlled: the unbind lands before the selection
        resolves. Rule 2 then has ZERO valid bindings, so it must refuse
        generically and mutate nothing — the prior context survives intact."""
        for backend in _backends():
            with self.subTest(backend=backend.label):
                store = backend.store
                api = ApiService(store)
                pid, s1, s2, gold = self._world(api)
                svc = ContextService(store)
                svc.set("u1", *ADMIN, pid, s2)
                before = store.get_active_context("u1")
                unbind_store = backend.connect()
                unbound = threading.Event()
                out = {}

                def unbinder():
                    _unbind(unbind_store, gold, s1)
                    unbound.set()

                def setter():
                    unbound.wait(20)
                    try:
                        out["result"] = svc.set_with_league(
                            "u1", *ADMIN, pid, s2, gold)
                    except Exception as exc:                   # noqa: BLE001
                        out["error"] = exc

                threads = [threading.Thread(target=unbinder),
                           threading.Thread(target=setter)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(30)

                self.assertIn("error", out,
                              f"{backend.label}: an unbound pair committed: "
                              f"{out.get('result')}")
                self.assertEqual(_reason(out["error"]), "league_not_accessible",
                                 (backend.label, repr(out["error"])))
                after = store.get_active_context("u1")
                self.assertEqual(self._triple(after), self._triple(before),
                                 f"{backend.label}: a refused selection mutated "
                                 f"the saved context")
                self._assert_consistent(store, svc, "u1", ADMIN, backend.label)
                backend.close()

    def test_free_race_unbind_versus_set_never_persists_a_dangling_pair(self):
        """Uncontrolled, repeated: whichever thread wins the lock, the row is one
        of the two consistent states and never the requested-but-unbound pair.

        The iterations alternate which side is nudged ahead so BOTH branches
        (refuse / commit) get sampled, but every iteration asserts only the
        invariant — a scheduler that picks the same side every time still makes
        this meaningful and can never make it flaky."""
        for backend in _backends():
            with self.subTest(backend=backend.label):
                store = backend.store
                api = ApiService(store)
                svc = ContextService(store)
                for i in range(6):
                    ids = self._world(api, suffix=f"_{i}")
                    pid, s1, s2, gold = ids
                    user = f"u{i}"
                    svc.set(user, *ADMIN, pid, s2)
                    race_store = backend.connect()

                    out = self._run_barrier(
                        lambda: svc.set_with_league(
                            user, *ADMIN, pid, s2, gold),
                        lambda: _unbind(race_store, gold, s1),
                        f"{backend.label}/{i}",
                        mutator_delay=0.01 if i % 2 else 0.0)

                    self._assert_outcome(store, svc, user, out, ids,
                                         f"{backend.label}/{i}")
                backend.close()


class LeagueContextRevocationRaceTest(_RaceAssertions, unittest.TestCase):
    """A scoped Coach's entitlement revoked WHILE their selection resolves.

    The Coach's authorized Leagues come from their Team's permanent League, so
    transferring the Team revokes the League they are selecting. Whatever
    commits, the invariant is about what they can then SEE: resolution is
    filtered through their CURRENT scope, so the revoked League (and, for a
    cross-Program transfer, the former Program) can never come back out. The
    saved row is deliberately NOT rewritten — restoring the entitlement restores
    the choice — which is why the row and the resolution are asserted separately.
    """

    def _coach_world(self, api):
        pid, sid, gold = _program_season_league(api, "P", "S1", "Gold")
        silver = api.create_league(sid, "Silver")["id"]
        team = _registered_team(api, sid, gold)
        return {"pid": pid, "sid": sid, "gold": gold, "silver": silver,
                "team": team}

    def test_league_revocation_vs_set_never_leaves_a_league_in_scope(self):
        """Program+League (no Season), deliberately: with a Season selected the
        same transfer ALSO revokes Season authorization, and the League would
        drop out through the season path — green for the wrong reason. Isolating
        the Season out makes League authorization the only variable."""
        for backend in _backends():
            with self.subTest(backend=backend.label):
                store = backend.store
                api = ApiService(store)
                w = self._coach_world(api)
                coach = (Role.COACH, {"team_id": w["team"]})
                svc = ContextService(store)
                svc.set_with_league("u1", *coach, w["pid"], None, None)
                mutator_store = backend.connect()

                out = self._run_pinned(
                    lambda: svc.set_with_league(
                        "u1", *coach, w["pid"], None, w["gold"]),
                    lambda: _transfer_team(
                        mutator_store, w["team"], w["silver"]),
                    backend.label)

                row, (_p, _s, league) = self._assert_consistent(
                    store, svc, "u1", coach, backend.label)
                if "error" in out:
                    self.assertEqual(_reason(out["error"]),
                                     "league_not_accessible",
                                     (backend.label, repr(out["error"])))
                # Exactly two consistent rows: the untouched prior selection, or
                # the League that WAS authorized when it committed.
                self.assertIn(
                    self._triple(row),
                    ((w["pid"], None, None), (w["pid"], None, w["gold"])),
                    f"{backend.label}: persisted {self._triple(row)}")
                # Gold is out of scope by read time, so it must not resolve —
                # whether or not it is still sitting in the saved row.
                self.assertNotIn(
                    w["gold"],
                    context_scope.authorized_league_ids(
                        store, *coach, w["pid"], "u1"),
                    f"{backend.label}: the transfer never applied")
                self.assertIsNone(
                    league,
                    f"{backend.label}: resolved a revoked League")
                backend.close()

    def test_season_and_league_revocation_vs_set_stays_inside_scope(self):
        """The full triple racing the same revocation: the Team transfer strips
        the Coach's Season authorization too, so nothing of the committed
        context may survive resolution — and every axis that DOES resolve is
        checked against the post-change scope, not against a hardcoded value."""
        for backend in _backends():
            with self.subTest(backend=backend.label):
                store = backend.store
                api = ApiService(store)
                w = self._coach_world(api)
                coach = (Role.COACH, {"team_id": w["team"]})
                svc = ContextService(store)
                svc.set_with_league("u1", *coach, w["pid"], None, None)
                mutator_store = backend.connect()

                out = self._run_pinned(
                    lambda: svc.set_with_league(
                        "u1", *coach, w["pid"], w["sid"], w["gold"]),
                    lambda: _transfer_team(
                        mutator_store, w["team"], w["silver"]),
                    backend.label)

                row, (program, season, league) = self._assert_consistent(
                    store, svc, "u1", coach, backend.label)
                if "error" in out:
                    self.assertIn(_reason(out["error"]),
                                  ("league_not_accessible",
                                   "season_not_accessible"),
                                  (backend.label, repr(out["error"])))
                self.assertIn(
                    self._triple(row),
                    ((w["pid"], None, None),
                     (w["pid"], w["sid"], w["gold"])),
                    f"{backend.label}: persisted {self._triple(row)}")
                # Everything that resolves is inside the CURRENT scope.
                programs = context_scope.authorized_program_ids(
                    store, *coach, "u1")
                if program is not None:
                    self.assertIn(program.id, programs,
                                  f"{backend.label}: resolved an unscoped Program")
                    seasons = context_scope.authorized_season_ids(
                        store, *coach, program.id, "u1")
                    if season is not None:
                        self.assertIn(
                            season.id, seasons,
                            f"{backend.label}: resolved an unscoped Season")
                self.assertIsNone(league,
                                  f"{backend.label}: resolved a revoked League")
                backend.close()

    def test_cross_program_transfer_vs_set_never_returns_the_former_program(self):
        """The transfer moves the Team to another Program entirely, so the whole
        saved context — Program included — falls outside scope. The row keeps
        naming the former Program (ignore-don't-rewrite); resolution must not."""
        for backend in _backends():
            with self.subTest(backend=backend.label):
                store = backend.store
                api = ApiService(store)
                w = self._coach_world(api)
                p2, s2, bronze = _program_season_league(
                    api, "P2", "S2", "Bronze")
                coach = (Role.COACH, {"team_id": w["team"]})
                svc = ContextService(store)
                svc.set_with_league("u1", *coach, w["pid"], None, None)
                mutator_store = backend.connect()

                out = self._run_pinned(
                    lambda: svc.set_with_league(
                        "u1", *coach, w["pid"], w["sid"], w["gold"]),
                    lambda: _transfer_team(
                        mutator_store, w["team"], bronze, program_id=p2),
                    backend.label)

                row, (program, _s, league) = self._assert_consistent(
                    store, svc, "u1", coach, backend.label)
                self.assertEqual(row.program_id, w["pid"],
                                 f"{backend.label}: the saved row was rewritten")
                self.assertIn(
                    self._triple(row),
                    ((w["pid"], None, None),
                     (w["pid"], w["sid"], w["gold"])),
                    f"{backend.label}: persisted {self._triple(row)}")
                if "error" in out:
                    self.assertIn(_reason(out["error"]),
                                  ("league_not_accessible",
                                   "season_not_accessible",
                                   "program_not_accessible"),
                                  (backend.label, repr(out["error"])))
                self.assertEqual(
                    context_scope.authorized_program_ids(store, *coach, "u1"),
                    {p2}, f"{backend.label}: the transfer never applied")
                self.assertIsNotNone(program, backend.label)
                self.assertEqual(program.id, p2,
                                 f"{backend.label}: resolved the former Program")
                self.assertIsNone(league,
                                  f"{backend.label}: resolved a revoked League")
                backend.close()


class LeagueContextCompetingWriteTest(_RaceAssertions, unittest.TestCase):
    """Two concurrent `set_with_league` calls for the SAME user.

    Both are submitted from the same unbound Season ``S3``, so BOTH go through
    rule 2 and both MOVE: Gold resolves to ``S1``, Silver to ``S2``. The two
    canonical tuples therefore differ in both axes, which is what makes a
    half-applied result detectable — ``(S1, Silver)`` or ``(S2, Gold)`` would be
    one call's Season carrying the other call's League, the exact shape the
    single-snapshot single-write contract exists to rule out.
    """

    def _world(self, api):
        pid, s1, gold = _program_season_league(api, "P", "S1", "Gold")
        s2 = api.create_season(pid, "S2")["id"]
        silver = api.create_league(s2, "Silver")["id"]
        s3 = api.create_season(pid, "S3")["id"]
        return pid, s1, s2, s3, gold, silver

    def test_competing_writes_leave_one_whole_canonical_tuple(self):
        for backend in _backends():
            with self.subTest(backend=backend.label):
                store = backend.store
                api = ApiService(store)
                pid, s1, s2, s3, gold, silver = self._world(api)
                # Each writer gets its own connection on PostgreSQL; on
                # Memory/SQLite this is the one shared store.
                writers = {gold: ContextService(backend.connect()),
                           silver: ContextService(backend.connect())}
                barrier = threading.Barrier(2)
                errs = []

                def w(league_id):
                    try:
                        barrier.wait(20)
                        writers[league_id].set_with_league(
                            "u1", *ADMIN, pid, s3, league_id)
                    except Exception as exc:                   # noqa: BLE001
                        errs.append(exc)

                threads = [threading.Thread(target=w, args=(lg,))
                           for lg in (gold, silver)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(30)
                for t in threads:
                    self.assertFalse(t.is_alive(), backend.label)

                if backend.label == "postgres":
                    # A serialization conflict that outlives the bounded retry is
                    # a legitimate outcome there; a wrong ANSWER is not.
                    self.assertLess(len(errs), 2, (backend.label, errs))
                    for exc in errs:
                        self.assertIsInstance(exc, ConcurrencyConflictError,
                                              repr(exc))
                else:
                    self.assertEqual(errs, [],
                                     (backend.label, [repr(e) for e in errs]))

                self.assertEqual(_row_count(store), 1,
                                 f"{backend.label}: not exactly one row")
                svc = ContextService(store)
                row, (program, season, league) = self._assert_consistent(
                    store, svc, "u1", ADMIN, backend.label)
                self.assertIn(
                    self._triple(row),
                    ((pid, s1, gold), (pid, s2, silver)),
                    f"{backend.label}: persisted a MIX of the two canonical "
                    f"tuples: {self._triple(row)}")
                # ...and the surviving row resolves to that same whole tuple
                # through the real read path, binding included.
                self.assertEqual(
                    (program.id, season.id, league.id), self._triple(row),
                    f"{backend.label}: the row does not resolve to itself")
                self.assertIsNotNone(store.league_season_for(league.id, season.id),
                                     backend.label)
                backend.close()

    def test_competing_writes_are_idempotent_under_repetition(self):
        """The same race run repeatedly never accumulates rows and never lands
        outside the two canonical tuples — a single lucky pass is not the claim."""
        for backend in _backends():
            with self.subTest(backend=backend.label):
                store = backend.store
                api = ApiService(store)
                pid, s1, s2, s3, gold, silver = self._world(api)
                svc = ContextService(store)
                a = ContextService(backend.connect())
                b = ContextService(backend.connect())
                for i in range(5):
                    barrier = threading.Barrier(2)

                    def w(svc_for_thread, league_id):
                        barrier.wait(20)
                        try:
                            svc_for_thread.set_with_league(
                                "u1", *ADMIN, pid, s3, league_id)
                        except ConcurrencyConflictError:
                            pass          # PostgreSQL only; asserted above
                    threads = [threading.Thread(target=w, args=args)
                               for args in ((a, gold), (b, silver))]
                    for t in threads:
                        t.start()
                    for t in threads:
                        t.join(30)

                    self.assertEqual(_row_count(store), 1,
                                     f"{backend.label}/{i}")
                    row, _resolved = self._assert_consistent(
                        store, svc, "u1", ADMIN, f"{backend.label}/{i}")
                    self.assertIn(self._triple(row),
                                  ((pid, s1, gold), (pid, s2, silver)),
                                  f"{backend.label}/{i}: {self._triple(row)}")
                backend.close()


if __name__ == "__main__":
    unittest.main()
