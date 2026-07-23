"""Placement atomicity: create / move / draft-commit are linearizable (#277 / #316
/ #313). Every placement path takes a canonical Team -> Rink -> Season lock set
before its final check + write:

* the target Rink ``FOR UPDATE`` lock serializes the physical one-game-per-slot
  half AND — critically — serializes placement with the ice-availability BUILDER,
  which revalidates its preview token and reconciles slots under the same per-rink
  lock. Without it a cross-Season placement could allocate a slot between the
  builder's under-lock token check and its writes; with it the loser blocks and
  re-reads the slot as ``slot_unavailable`` (the ``ux_games_active_ice_slot`` index
  remains a backstop for any non-locking insert, see test_iceslot_venue_fks);
* the Team ``FOR UPDATE`` lock closes the team-overlap half (no DB backstop) ->
  ``team_overlap``.

Rink is locked BEFORE Season to match the builder's Program -> Rink -> Season order
(locking Season first would deadlock it). Same-season placements also serialize on
the Season lock, which would mask the Rink/Team locks, so the forced PostgreSQL
races below are deliberately CROSS-season (a rink-scoped ``IceSlot`` shared via two
seasons' venue access, or a team registered in two seasons) — the interleaving
where the Rink/Team locks, not the Season lock, keep the outcome linearizable.

Memory and SQLite get the sequential parity (their ``transaction()`` serializes,
so the pre-check refuses the second placement without threads).
"""
import os
import threading
import unittest
from datetime import datetime, timedelta, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    Organization, Program, Season, League, LeagueSeason, Division, Venue,
    SeasonVenueAccess, Rink, Team, SeasonTeamRegistration, IceSlot,
    IceSlotType, IceSlotStatus)
from hockey_scheduler.store import InMemoryStore, SqlStore

UTC = timezone.utc
BASE = datetime(2026, 1, 5, 18, tzinfo=UTC)


def _seed(s):
    """One venue (rinks r1/r2) shared by TWO seasons:

    * se1 / division d1 — teams t0..t3, the round-robin slots s0..s5 on r1 (the
      draft uses these) and the spare sX on r1;
    * se2 / division d2 — teams u0/u1, PLUS t0 cross-registered so a single team
      can be double-booked across seasons; slot sB on r2 at s0's time.

    Both seasons have venue access to v, and ``IceSlot`` is rink-scoped, so a slot
    can host games from either season — the cross-season contention the DB index
    (for the slot) and the Team lock (for a shared team) guard."""
    s.add_organization(Organization(id="org", name="Owner"))
    s.add_program(Program(id="pg", name="Program",
                          operator_organization_id="org"))
    s.add_league(League(id="lg", program_id="pg", name="League"))
    s.add_venue(Venue(id="v", name="Arena", organization_id="org",
                      league_id="pg"))
    s.add_rink(Rink(id="r1", venue_id="v", name="Main"))
    s.add_rink(Rink(id="r2", venue_id="v", name="Aux"))
    s.add_rink(Rink(id="r3", venue_id="v", name="Annex"))
    for sid in ("se1", "se2"):
        s.add_season(Season(id=sid, program_id="pg", name=sid.upper()))
        s.add_season_venue_access(SeasonVenueAccess(
            id=f"sva_{sid}", season_id=sid, venue_id="v", active=True))
    s.add_league_season(LeagueSeason(id="ls1", league_id="lg", season_id="se1"))
    s.add_league_season(LeagueSeason(id="ls2", league_id="lg", season_id="se2"))
    s.add_division(Division(id="d1", league_season_id="ls1", name="D1"))
    s.add_division(Division(id="d2", league_season_id="ls2", name="D2"))
    # se1 / d1 — the draftable division.
    for i in range(4):
        s.add_team(Team(id=f"t{i}", name=f"T{i}", division="D1", division_id="d1",
                        program_id="pg", league_id="lg"))
        s.add_season_team_registration(SeasonTeamRegistration(
            id=f"reg1_t{i}", league_season_id="ls1", team_id=f"t{i}",
            division_id="d1", active=True))
    # se2 / d2 — two of its own teams, plus t0 cross-registered.
    for i in range(2):
        s.add_team(Team(id=f"u{i}", name=f"U{i}", division="D2", division_id="d2",
                        program_id="pg", league_id="lg"))
        s.add_season_team_registration(SeasonTeamRegistration(
            id=f"reg2_u{i}", league_season_id="ls2", team_id=f"u{i}",
            division_id="d2", active=True))
    s.add_season_team_registration(SeasonTeamRegistration(
        id="reg2_t0", league_season_id="ls2", team_id="t0", division_id="d2",
        active=True))

    def gslot(sid, rink, day):
        s.add_ice_slot(IceSlot(
            id=sid, rink_id=rink, start_time=BASE + timedelta(days=day),
            end_time=BASE + timedelta(days=day, hours=1),
            slot_type=IceSlotType.GAME, status=IceSlotStatus.AVAILABLE))

    for i in range(6):
        gslot(f"s{i}", "r1", i)
    gslot("sB", "r2", 0)                     # same time as s0, different rink
    gslot("sX", "r1", 9)                     # spare, unused by the draft
    # #314 — move-race fixtures: a game starts on mA (r1) and is moved through
    # mB (r2) to mC (r3), one slot per rink so "which Rink is locked" is
    # unambiguous. Same wall-clock time on all three, far from s0-s5/sX/sB.
    gslot("mA", "r1", 100)
    gslot("mB", "r2", 100)
    gslot("mC", "r3", 100)


def _slot_game_count(store, slot_id):
    return sum(1 for g in store.all_games()
               if not g.cancelled and g.ice_slot_id == slot_id)


def _reason(r):
    return (r.get("error", {}).get("details", {}).get("reason")
            if isinstance(r, dict) else None)


def _created(r):
    return len(r.get("created", [])) if isinstance(r, dict) else 0


def _builder_template(season_id, rink_id):
    """An ice-availability-builder template whose single Monday 18:00-19:00 UTC
    window is exactly the seeded slot s0's tuple (the Program has no timezone, so
    local == UTC), so the builder classifies s0 as a 'duplicate' — the reviewed row
    a concurrent cross-Season placement can turn into an allocated-Game conflict."""
    return dict(season_id=season_id, rink_ids=[rink_id], weekdays=[0],
                start_local="18:00", end_local="19:00",
                start_date="2026-01-05", end_date="2026-01-05",
                playable_minutes=60, turnover_minutes=0)


def _move_race_builder_template(season_id):
    """A builder template whose single Wednesday 18:00-19:00 UTC window is
    exactly slot mB's tuple (rink r2, day+100 => 2026-04-15), so the builder
    classifies mB as a 'duplicate' — the reviewed row a queued move racing
    move_game's own Team-lock queue must never turn into a corrupted read or an
    unprotected write on rink r2 (#314 review)."""
    return dict(season_id=season_id, rink_ids=["r2"], weekdays=[2],
                start_local="18:00", end_local="19:00",
                start_date="2026-04-15", end_date="2026-04-15",
                playable_minutes=60, turnover_minutes=0)


class _PlacementParityMixin:
    """Sequential parity over a seeded store (Memory / SQLite): the store's
    transaction() serializes, so the SECOND placement onto a contested slot/team
    surfaces the SAME structured error the PostgreSQL race asserts."""

    def _make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self._make_store()
        _seed(self.store)
        self.api = ApiService(self.store)

    def test_second_create_on_the_same_slot_is_refused(self):
        a = self.api.create_game("se1", "d1", "t0", "t1", "s0", league_id="lg")
        self.assertNotIn("error", a)
        # A second season reusing the same rink-scoped slot is refused.
        b = self.api.create_game("se2", "d2", "u0", "u1", "s0", league_id="lg")
        self.assertIn(_reason(b), ("slot_unavailable", "slot_already_filled"))
        self.assertEqual(_slot_game_count(self.store, "s0"), 1)

    def test_second_overlapping_game_for_a_team_is_refused(self):
        a = self.api.create_game("se1", "d1", "t0", "t1", "s0", league_id="lg")
        self.assertNotIn("error", a)
        # sB is a different rink but the SAME time as s0, in the OTHER season,
        # sharing the cross-registered t0.
        b = self.api.create_game("se2", "d2", "t0", "u0", "sB", league_id="lg")
        self.assertEqual(_reason(b), "team_overlap")
        self.assertEqual(_slot_game_count(self.store, "sB"), 0)

    def test_builder_commit_rejects_a_slot_a_cross_season_game_took(self):
        # #313: the ice-availability builder reviews s0 as a "duplicate" (existing
        # AVAILABLE Game ice); a cross-Season create_game then allocates s0. The
        # builder recomputes its token under the per-rink lock, sees s0 is now an
        # allocated-Game conflict, and refuses with preview_mismatch — zero new
        # slots — while a re-preview surfaces the exact Game conflict. (Sequential
        # here; the PostgreSQL race class runs the two concurrently.)
        tmpl = _builder_template("se1", "r1")
        pv = self.api.preview_ice_availability(actor_id="b", **tmpl)
        self.assertEqual(pv["slots"][0]["status"], "duplicate")
        g = self.api.create_game("se2", "d2", "u0", "u1", "s0", league_id="lg")
        self.assertNotIn("error", g)
        before = len(list(self.store.all_ice_slots()))
        res = self.api.commit_ice_availability(
            actor_id="b", template_fingerprint=pv["template_fingerprint"], **tmpl)
        self.assertEqual(res["error"]["details"]["reason"], "preview_mismatch")
        self.assertEqual(len(list(self.store.all_ice_slots())), before)  # zero new ice
        pv2 = self.api.preview_ice_availability(actor_id="b", **tmpl)
        self.assertEqual(pv2["slots"][0]["status"], "conflict")
        self.assertEqual(pv2["slots"][0]["conflict_game_id"], g["id"])

    def test_two_sequential_moves_then_builder_commit_on_b(self):
        # #314 review parity: a game moves mA -> mB -> mC (two moves, matching
        # the forced PostgreSQL race's shape), then the builder commits a
        # template matching mB's exact tuple. Sequential here (no staleness is
        # even possible — each move fully completes before the next reads), so
        # the game passes through mB and back out, leaving it exactly as
        # previewed: the builder's ORIGINAL fingerprint still matches, and it
        # commits cleanly with zero new ice.
        g = self.api.create_game("se1", "d1", "t0", "t1", "mA", league_id="lg")
        self.assertNotIn("error", g)
        gid = g["id"]
        tmpl = _move_race_builder_template("se1")
        pv = self.api.preview_ice_availability(actor_id="b", **tmpl)
        self.assertEqual(pv["slots"][0]["status"], "duplicate")

        m1 = self.api.move_game(gid, "mB")
        self.assertNotIn("error", m1)
        m2 = self.api.move_game(gid, "mC")
        self.assertNotIn("error", m2)

        res = self.api.commit_ice_availability(
            actor_id="b", template_fingerprint=pv["template_fingerprint"], **tmpl)
        self.assertNotIn("error", res)
        self.assertEqual(res["totals"]["created"], 0)
        self.assertEqual(_slot_game_count(self.store, "mA"), 0)
        self.assertEqual(_slot_game_count(self.store, "mB"), 0)
        self.assertEqual(_slot_game_count(self.store, "mC"), 1)

    # -- slot/Season eligibility is re-validated under the lock, not from a
    # stale pre-lock read (#314 review) ------------------------------------
    def test_move_refused_after_venue_access_revoked(self):
        # Sequential parity: revoke se1's access to venue "v" (hosting mB's
        # rink r2), THEN attempt to move a game onto mB. The recheck must run
        # under the lock this commits acquires (not a stale pre-lock read
        # taken before any lock existed) — a stable venue_access_missing
        # refusal, the game untouched on its original slot, and ZERO partial
        # writes (no slot flip, no audit for this move).
        g = self.api.create_game("se1", "d1", "t0", "t1", "sX", league_id="lg")
        self.assertNotIn("error", g)
        gid = g["id"]
        r = self.api.revoke_season_venue_access("sva_se1")
        self.assertNotIn("error", r)
        res = self.api.move_game(gid, "mB")
        self.assertEqual(_reason(res), "venue_access_missing", repr(res))
        game_now = next(gm for gm in self.store.all_games() if gm.id == gid)
        self.assertEqual(game_now.ice_slot_id, "sX")
        self.assertEqual(_slot_game_count(self.store, "mB"), 0)
        self.assertEqual(self.store.get_ice_slot("mB").status.value, "available")
        self.assertFalse(any(
            au.action == "game_moved" and au.entity_id == gid
            for au in self.store.all_setup_audit()))

    def test_move_refused_after_rink_reparented(self):
        # Same shape, the OTHER race trigger: reassign mC's rink (r3) to a
        # venue with no access for se1, then attempt to move onto mC.
        self.store.add_venue(Venue(id="v2", name="No Access",
                                   organization_id="org", league_id="pg"))
        g = self.api.create_game("se1", "d1", "t0", "t1", "sX", league_id="lg")
        self.assertNotIn("error", g)
        gid = g["id"]
        reassigned = self.api.assign_rink_venue("r3", "v2")
        self.assertNotIn("error", reassigned)
        res = self.api.move_game(gid, "mC")
        self.assertEqual(_reason(res), "venue_access_missing", repr(res))
        game_now = next(gm for gm in self.store.all_games() if gm.id == gid)
        self.assertEqual(game_now.ice_slot_id, "sX")
        self.assertEqual(_slot_game_count(self.store, "mC"), 0)
        self.assertEqual(self.store.get_ice_slot("mC").status.value, "available")

    def test_draft_commit_refused_after_venue_access_revoked(self):
        # Same shape for commit_draft_schedule: revoke first, then commit a
        # single-slot draft targeting mB — refused under the SAME locks the
        # batch acquires, zero Games/slot-flips/audit, not the stale
        # pre-transaction prevalidation this review found insufficient.
        r = self.api.revoke_season_venue_access("sva_se1")
        self.assertNotIn("error", r)
        res = self.api.commit_draft_schedule("d1", slot_ids=["mB"])
        self.assertEqual(_reason(res), "venue_access_missing", repr(res))
        self.assertEqual(
            len([g for g in self.store.all_games() if not g.cancelled]), 0)
        self.assertEqual(self.store.get_ice_slot("mB").status.value, "available")
        self.assertFalse(any(a.action == "draft_schedule_committed"
                            for a in self.store.all_setup_audit()))

    def test_draft_commit_refused_after_rink_reparented(self):
        self.store.add_venue(Venue(id="v2", name="No Access",
                                   organization_id="org", league_id="pg"))
        reassigned = self.api.assign_rink_venue("r3", "v2")
        self.assertNotIn("error", reassigned)
        res = self.api.commit_draft_schedule("d1", slot_ids=["mC"])
        self.assertEqual(_reason(res), "venue_access_missing", repr(res))
        self.assertEqual(
            len([g for g in self.store.all_games() if not g.cancelled]), 0)
        self.assertEqual(self.store.get_ice_slot("mC").status.value, "available")
        self.assertFalse(any(a.action == "draft_schedule_committed"
                            for a in self.store.all_setup_audit()))


class MemoryPlacementParityTest(_PlacementParityMixin, unittest.TestCase):
    def _make_store(self):
        return InMemoryStore()


class SqlitePlacementParityTest(_PlacementParityMixin, unittest.TestCase):
    def _make_store(self):
        return SqlStore(":memory:")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresPlacementParityTest(_PlacementParityMixin, unittest.TestCase):
    def _make_store(self):
        store = SqlStore(os.environ["TEST_DATABASE_URL"])
        store.clear_all_data()
        return store


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresPlacementConcurrencyTest(unittest.TestCase):
    """Forced two-session placement races on real PostgreSQL (#277 / #316). A
    barrier releases both sessions at once; each is its own connection. The races
    are CROSS-season (se1 vs se2) so the Season lock does NOT serialize them — the
    DB slot index and the Team lock must, guaranteeing at most one placement per
    contested slot/team, a stable structured loser error, and zero partial
    writes."""

    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        seed = SqlStore(self.url)
        seed.clear_all_data()
        _seed(seed)

    def _store(self):
        return SqlStore(self.url)

    def _run(self, targets):
        barrier = threading.Barrier(len(targets))
        out = [None] * len(targets)

        def wrap(i, fn):
            api = ApiService(SqlStore(self.url))
            barrier.wait()
            try:
                out[i] = fn(api)
            except Exception as exc:            # unexpected; asserted by caller
                out[i] = exc
        threads = [threading.Thread(target=wrap, args=(i, fn))
                   for i, fn in enumerate(targets)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return out

    def _assert_no_crash(self, out):
        for r in out:
            self.assertNotIsInstance(r, Exception, f"unexpected crash: {out!r}")

    def _assert_schedule_consistent(self, store):
        """No two active games share a slot, and no shared team overlaps in time —
        exactly the invariant the checker enforces, verified to have held across
        the race."""
        active = [g for g in store.all_games() if not g.cancelled]
        seen = set()
        for g in active:
            self.assertNotIn(g.ice_slot_id, seen,
                             f"slot {g.ice_slot_id} double-booked")
            seen.add(g.ice_slot_id)
        for i in range(len(active)):
            for j in range(i + 1, len(active)):
                gi, gj = active[i], active[j]
                si = store.get_ice_slot(gi.ice_slot_id)
                sj = store.get_ice_slot(gj.ice_slot_id)
                if si and sj and si.start_time < sj.end_time \
                        and sj.start_time < si.end_time:
                    shared = ({gi.home_team_id, gi.away_team_id}
                              & {gj.home_team_id, gj.away_team_id})
                    self.assertFalse(shared, f"team double-booked: {gi} vs {gj}")

    # (1) create-vs-create on ONE slot, DIFFERENT seasons -> physical slot race.
    # The Season locks differ (no serialization there), so the target Rink lock is
    # what serializes them: the loser blocks, then re-reads the ALLOCATED slot under
    # the lock and is refused with slot_unavailable at the pre-check (the DB index
    # never has to fire).
    def test_create_vs_create_same_slot_cross_season(self):
        out = self._run([
            lambda a: a.create_game("se1", "d1", "t0", "t1", "s0", league_id="lg"),
            lambda a: a.create_game("se2", "d2", "u0", "u1", "s0", league_id="lg"),
        ])
        self._assert_no_crash(out)
        oks = [r for r in out if isinstance(r, dict) and "error" not in r]
        errs = [r for r in out if isinstance(r, dict) and "error" in r]
        self.assertEqual(len(oks), 1, f"exactly one create should land: {out!r}")
        self.assertEqual(len(errs), 1, f"the loser should be refused: {out!r}")
        self.assertIn(_reason(errs[0]),
                      ("slot_unavailable", "ice_slot_taken", "slot_already_filled"))
        self.assertEqual(_slot_game_count(self._store(), "s0"), 1)

    # (2) overlapping same-Team creates on DIFFERENT rinks + seasons -> Team race.
    # Different Season locks + different Rinks, so ONLY the t0 Team lock serializes.
    def test_overlapping_same_team_creates_cross_season(self):
        out = self._run([
            lambda a: a.create_game("se1", "d1", "t0", "t1", "s0", league_id="lg"),
            lambda a: a.create_game("se2", "d2", "t0", "u0", "sB", league_id="lg"),
        ])
        self._assert_no_crash(out)
        oks = [r for r in out if isinstance(r, dict) and "error" not in r]
        errs = [r for r in out if isinstance(r, dict) and "error" in r]
        self.assertEqual(len(oks), 1, f"exactly one create should land: {out!r}")
        self.assertEqual(_reason(errs[0]), "team_overlap")
        store = self._store()
        self.assertEqual(_slot_game_count(store, "s0")
                         + _slot_game_count(store, "sB"), 1)   # only the winner
        self._assert_schedule_consistent(store)

    # (3) two identical draft commits (same season) -> all-or-nothing batch, no
    # double-booking (the Season lock serializes; the batch rolls back whole).
    def test_draft_vs_draft(self):
        out = self._run([
            lambda a: a.commit_draft_schedule("d1"),
            lambda a: a.commit_draft_schedule("d1"),
        ])
        self._assert_no_crash(out)
        # Exactly one full round-robin exists; the other batch created nothing —
        # rolled back on a slot conflict, or regenerated an empty proposal.
        self.assertEqual(sum(_created(r) for r in out), 6, repr(out))
        for r in out:
            if _reason(r) is not None:
                self.assertIn(_reason(r),
                              ("ice_slot_taken", "slot_unavailable",
                               "slot_already_filled", "team_overlap"))
        store = self._store()
        self.assertEqual(
            sum(1 for g in store.all_games() if not g.cancelled), 6)
        self._assert_schedule_consistent(store)

    # (4) move (se2 game) vs draft-commit (se1) contend for one slot across
    # seasons -> the DB slot index, not the Season lock, keeps s0 single-booked.
    def test_move_vs_draft_commit_cross_season(self):
        # A se2 game parked on the spare sX; move it onto s0, which the se1 draft
        # also targets (t0 vs t3 -> s0).
        g = ApiService(self._store()).create_game(
            "se2", "d2", "u0", "u1", "sX", league_id="lg")
        gid = g["id"]
        out = self._run([
            lambda a: a.move_game(gid, "s0"),
            lambda a: a.commit_draft_schedule("d1"),
        ])
        self._assert_no_crash(out)
        store = self._store()
        self.assertLessEqual(_slot_game_count(store, "s0"), 1)   # never two
        for r in out:
            if _reason(r) is not None:
                self.assertIn(_reason(r),
                              ("ice_slot_taken", "slot_unavailable",
                               "slot_already_filled", "team_overlap"))
        self._assert_schedule_consistent(store)

    # (5) ice-availability BUILDER preview->commit vs a cross-Season placement on
    # the SAME rink+slot (#313). The builder holds Program->Rink->Season; placement
    # holds Team->Rink->Season — both lock the Rink BEFORE the Season, so they
    # serialize on the rink lock with NO deadlock (a wrong order would hang -> a
    # deadlock error surfaced by _assert_no_crash). s0 is never double-booked, the
    # builder never partially writes, and whichever runs second under the lock sees
    # a consistent state: the builder either commits cleanly (its only window is the
    # s0 duplicate -> 0 new ice) or is refused preview_mismatch (its reviewed s0
    # became an allocated-Game conflict). Runs release both at once, so repeated
    # runs cover both orderings.
    def _builder_vs(self, builder_season, placement):
        tmpl = _builder_template(builder_season, "r1")
        fp = ApiService(self._store()).preview_ice_availability(
            actor_id="b", **tmpl)["template_fingerprint"]
        out = self._run([
            lambda a: a.commit_ice_availability(
                actor_id="b", template_fingerprint=fp, **tmpl),
            placement,
        ])
        self._assert_no_crash(out)
        store = self._store()
        self.assertLessEqual(_slot_game_count(store, "s0"), 1)   # never double-booked
        builder = out[0]
        if isinstance(builder, dict) and "error" not in builder:
            self.assertEqual(builder["totals"]["created"], 0)    # no partial write
        elif isinstance(builder, dict):
            self.assertEqual(_reason(builder), "preview_mismatch")
        self._assert_schedule_consistent(store)

    def test_builder_commit_vs_cross_season_create(self):
        self._builder_vs(
            "se1",
            lambda a: a.create_game("se2", "d2", "u0", "u1", "s0", league_id="lg"))

    def test_builder_commit_vs_cross_season_move(self):
        # A se2 game parked on the spare sX; move it onto s0 (the builder's slot).
        g = ApiService(self._store()).create_game(
            "se2", "d2", "u0", "u1", "sX", league_id="lg")
        gid = g["id"]
        self._builder_vs("se1", lambda a: a.move_game(gid, "s0"))

    def test_builder_commit_vs_cross_season_draft(self):
        # The se1/d1 draft allocates s0 (t0 vs t3 -> s0); the builder previews the
        # OTHER season (se2) on the same rink so the Season locks differ and only
        # the rink lock serializes them.
        self._builder_vs("se2", lambda a: a.commit_draft_schedule("d1"))

    def test_builder_commit_vs_same_season_create(self):
        # SAME season as the builder: builder and create both lock rink r1 AND
        # season se1. This is the case that would DEADLOCK if placement locked the
        # Season before the Rink (builder does Rink->Season) — the Team->Rink->Season
        # order keeps it deadlock-free, and a deadlock would surface as an aborted
        # transaction that _assert_no_crash catches.
        self._builder_vs(
            "se1",
            lambda a: a.create_game("se1", "d1", "t0", "t1", "s0", league_id="lg"))

    # (6) move A->B, a QUEUED move (its own pre-lock read still shows A) racing
    # for the SAME Team lock intending B->C, and the ice-availability BUILDER
    # committing a template matching slot mB's EXACT tuple on rink r2 — all
    # three released at once (#314 review). move_game must lock the game's
    # CURRENT source Rink, computed AFTER the Team lock is held, not from
    # whatever snapshot it read before waiting for that lock: otherwise the
    # queued move could mutate rink r2 (release/allocate slot mB) without ever
    # holding r2's lock, letting it race the builder's classify-then-write
    # cycle uncontrolled. Both moves always land (each targets a slot that is
    # never "where it already is" once its Team-lock turn comes, whichever
    # order that turn falls in); the builder either sees mB untouched (commits
    # cleanly, 0 new — its reviewed 'duplicate' still holds) or catches it
    # ALLOCATED mid-move (refused, preview_mismatch, zero writes) — never a
    # corrupted state, and never double-booked.
    def test_move_a_to_b_queued_move_b_to_c_vs_builder_commit_on_b(self):
        api0 = ApiService(self._store())
        g = api0.create_game("se1", "d1", "t0", "t1", "mA", league_id="lg")
        gid = g["id"]
        tmpl = _move_race_builder_template("se1")
        fp = api0.preview_ice_availability(
            actor_id="b", **tmpl)["template_fingerprint"]
        self.assertEqual(
            api0.preview_ice_availability(actor_id="b", **tmpl)["slots"][0]["status"],
            "duplicate")

        out = self._run([
            lambda a: a.move_game(gid, "mB"),
            lambda a: a.move_game(gid, "mC"),
            lambda a: a.commit_ice_availability(
                actor_id="b", template_fingerprint=fp, **tmpl),
        ])
        self._assert_no_crash(out)
        store = self._store()

        # Exactly one of {mB, mC} ends up hosting the game; mA is released;
        # never double-booked across any pair.
        self.assertEqual(_slot_game_count(store, "mA"), 0,
                         f"the original slot must be released: {out!r}")
        self.assertEqual(
            _slot_game_count(store, "mB") + _slot_game_count(store, "mC"), 1,
            f"exactly one of B/C should host the game: {out!r}")
        self._assert_schedule_consistent(store)

        # Both moves land: each one's FIXED target (mB / mC respectively) is
        # never equal to wherever the game truly is when its Team-lock turn
        # comes, in EITHER interleaving order, so neither can hit "same_slot",
        # and nothing else contends for their unique dedicated targets.
        move_a, move_b = out[0], out[1]
        self.assertNotIn("error", move_a, f"move A->B should land: {out!r}")
        self.assertNotIn("error", move_b, f"the queued move should land: {out!r}")

        # The builder either committed the reviewed snapshot cleanly (it saw
        # slot mB untouched) or was refused preview_mismatch with zero writes
        # (it caught mB mid-move) — the only two consistent outcomes; the
        # queued move never mutated mB outside its own Rink lock.
        builder = out[2]
        if isinstance(builder, dict) and "error" not in builder:
            self.assertEqual(builder["totals"]["created"], 0, repr(out))
        else:
            self.assertEqual(_reason(builder), "preview_mismatch", repr(out))

    # (7) A concurrent SeasonVenueAccess revoke or Rink→Venue reparent racing
    # move_game / commit_draft_schedule (#314 review). require_slot_belongs_to_
    # season is a pure read with no lock of its own; checking it before the
    # Team/Rink/Season locks (move_game) or entirely before the transaction
    # opens (commit_draft_schedule) leaves a window where the revoke/reparent
    # can commit in between, after which the write would land a Game onto ice
    # that no longer serves the Season. Both sides serialize via a lock they
    # ALREADY take for another reason — revoke via the Season lock
    # (_require_active_season, matching _guard_game_season/_guard_active_
    # seasons); reparent via the implicit row lock its own UPDATE needs on the
    # SAME Rink row _lock_rinks holds — so whichever commits first is seen by
    # the other's now-locked recheck, deterministically. The revoke/reassign
    # itself always succeeds (neither checks for existing Games); the
    # move/commit either lands cleanly (it ran first) or is refused with a
    # stable venue_access_missing and ZERO partial Games, slot flips, or
    # audits (it saw the now-ineligible ice) — never a corrupted state.
    def test_revoke_venue_access_vs_move(self):
        api0 = ApiService(self._store())
        g = api0.create_game("se1", "d1", "t0", "t1", "sX", league_id="lg")
        gid = g["id"]
        out = self._run([
            lambda a: a.move_game(gid, "mB"),
            lambda a: a.revoke_season_venue_access("sva_se1"),
        ])
        self._assert_no_crash(out)
        move_res, revoke_res = out
        self.assertNotIn("error", revoke_res,
                         f"the revoke should always succeed: {out!r}")
        store = self._store()
        game_now = next(gm for gm in store.all_games() if gm.id == gid)
        if isinstance(move_res, dict) and "error" not in move_res:
            self.assertEqual(game_now.ice_slot_id, "mB")
            self.assertEqual(_slot_game_count(store, "mB"), 1)
            self.assertEqual(_slot_game_count(store, "sX"), 0)
        else:
            self.assertEqual(_reason(move_res), "venue_access_missing", repr(out))
            self.assertEqual(game_now.ice_slot_id, "sX")
            self.assertEqual(_slot_game_count(store, "mB"), 0)
            self.assertEqual(store.get_ice_slot("mB").status.value, "available")
            self.assertFalse(any(
                au.action == "game_moved" and au.entity_id == gid
                for au in store.all_setup_audit()))
        self._assert_schedule_consistent(store)

    def test_reparent_rink_vs_move(self):
        self._store().add_venue(Venue(id="v2", name="No Access",
                                      organization_id="org", league_id="pg"))
        api0 = ApiService(self._store())
        g = api0.create_game("se1", "d1", "t0", "t1", "sX", league_id="lg")
        gid = g["id"]
        out = self._run([
            lambda a: a.move_game(gid, "mC"),
            lambda a: a.assign_rink_venue("r3", "v2"),
        ])
        self._assert_no_crash(out)
        move_res, reassign_res = out
        self.assertNotIn("error", reassign_res,
                         f"the reassignment should always succeed: {out!r}")
        store = self._store()
        game_now = next(gm for gm in store.all_games() if gm.id == gid)
        if isinstance(move_res, dict) and "error" not in move_res:
            self.assertEqual(game_now.ice_slot_id, "mC")
            self.assertEqual(_slot_game_count(store, "mC"), 1)
        else:
            self.assertEqual(_reason(move_res), "venue_access_missing", repr(out))
            self.assertEqual(game_now.ice_slot_id, "sX")
            self.assertEqual(_slot_game_count(store, "mC"), 0)
            self.assertEqual(store.get_ice_slot("mC").status.value, "available")
            self.assertFalse(any(
                au.action == "game_moved" and au.entity_id == gid
                for au in store.all_setup_audit()))
        self._assert_schedule_consistent(store)

    def test_revoke_venue_access_vs_draft_commit(self):
        out = self._run([
            lambda a: a.commit_draft_schedule("d1", slot_ids=["mB"]),
            lambda a: a.revoke_season_venue_access("sva_se1"),
        ])
        self._assert_no_crash(out)
        draft_res, revoke_res = out
        self.assertNotIn("error", revoke_res,
                         f"the revoke should always succeed: {out!r}")
        store = self._store()
        if isinstance(draft_res, dict) and "error" not in draft_res:
            self.assertEqual(len(draft_res["created"]), 1, repr(out))
            self.assertEqual(_slot_game_count(store, "mB"), 1)
            self.assertTrue(any(a.action == "draft_schedule_committed"
                               for a in store.all_setup_audit()))
        else:
            self.assertEqual(_reason(draft_res), "venue_access_missing", repr(out))
            self.assertEqual(
                len([g for g in store.all_games() if not g.cancelled]), 0)
            self.assertEqual(store.get_ice_slot("mB").status.value, "available")
            self.assertFalse(any(a.action == "draft_schedule_committed"
                                for a in store.all_setup_audit()))
        self._assert_schedule_consistent(store)

    def test_reparent_rink_vs_draft_commit(self):
        self._store().add_venue(Venue(id="v2", name="No Access",
                                      organization_id="org", league_id="pg"))
        out = self._run([
            lambda a: a.commit_draft_schedule("d1", slot_ids=["mC"]),
            lambda a: a.assign_rink_venue("r3", "v2"),
        ])
        self._assert_no_crash(out)
        draft_res, reassign_res = out
        self.assertNotIn("error", reassign_res,
                         f"the reassignment should always succeed: {out!r}")
        store = self._store()
        if isinstance(draft_res, dict) and "error" not in draft_res:
            self.assertEqual(len(draft_res["created"]), 1, repr(out))
            self.assertEqual(_slot_game_count(store, "mC"), 1)
            self.assertTrue(any(a.action == "draft_schedule_committed"
                               for a in store.all_setup_audit()))
        else:
            self.assertEqual(_reason(draft_res), "venue_access_missing", repr(out))
            self.assertEqual(
                len([g for g in store.all_games() if not g.cancelled]), 0)
            self.assertEqual(store.get_ice_slot("mC").status.value, "available")
            self.assertFalse(any(a.action == "draft_schedule_committed"
                                for a in store.all_setup_audit()))
        self._assert_schedule_consistent(store)


if __name__ == "__main__":
    unittest.main()
