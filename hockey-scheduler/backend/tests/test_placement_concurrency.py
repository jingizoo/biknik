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

from helpers import BACKEND, commit_fresh_draft  # noqa: F401  (BACKEND: sys.path)

from hockey_scheduler.api import ApiService
from hockey_scheduler.api.service import ApiService as BaseApiService
from hockey_scheduler.domain import (
    Organization, Program, Season, League, LeagueSeason, Division, Venue,
    SeasonVenueAccess, Rink, Team, SeasonTeamRegistration, IceSlot,
    IceSlotType, IceSlotStatus, Game)
from hockey_scheduler.domain.errors import DomainError
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
        res = commit_fresh_draft(self.api, "d1", slot_ids=["mB"])
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
        res = commit_fresh_draft(self.api, "d1", slot_ids=["mC"])
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


class _ForcedRaceHarnessMixin:
    """Shared two-session PostgreSQL race harness (#277/#314): a barrier
    releases every session at once, each its own connection/ApiService
    instance. ``_api_cls`` defaults to the production league-scoped facade;
    override it to exercise the base facade's own implementation instead
    (#314 review — both must be race-safe, not just the one production
    resolves to)."""

    def _api_cls(self):
        return ApiService

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
        api_cls = self._api_cls()

        def wrap(i, fn):
            api = api_cls(SqlStore(self.url))
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


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresPlacementConcurrencyTest(_ForcedRaceHarnessMixin, unittest.TestCase):
    """Forced two-session placement races on real PostgreSQL (#277 / #316). A
    barrier releases both sessions at once; each is its own connection. The races
    are CROSS-season (se1 vs se2) so the Season lock does NOT serialize them — the
    DB slot index and the Team lock must, guaranteeing at most one placement per
    contested slot/team, a stable structured loser error, and zero partial
    writes."""

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
            lambda a: commit_fresh_draft(a, "d1"),
            lambda a: commit_fresh_draft(a, "d1"),
        ])
        self._assert_no_crash(out)
        # Exactly one full round-robin exists; the other batch created nothing —
        # rolled back on a slot conflict, or regenerated an empty proposal.
        self.assertEqual(sum(_created(r) for r in out), 6, repr(out))
        for r in out:
            if _reason(r) is not None:
                # #328 review round 4: the loser's first row is the SAME
                # pairing the winner already committed onto the identical
                # slot, so the pairing-identity guard (checked first) now
                # correctly wins with the more specific terminal reason
                # instead of a generic physical-conflict one. #328 review
                # round 5: each side's own preview-then-commit (both now
                # required) can likewise straddle the other's write, so the
                # loser may instead see its OWN external preview go stale
                # relative to its internal regeneration -- an equally valid
                # terminal outcome.
                self.assertIn(_reason(r),
                              ("ice_slot_taken", "slot_unavailable",
                               "slot_already_filled", "team_overlap",
                               "pairing_already_scheduled", "preview_stale"))
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
            lambda a: commit_fresh_draft(a, "d1"),
        ])
        self._assert_no_crash(out)
        store = self._store()
        self.assertLessEqual(_slot_game_count(store, "s0"), 1)   # never two
        for r in out:
            if _reason(r) is not None:
                # #328 review round 7: d1 has exactly 6 slots (s0-s5) for its 6
                # round-robin pairings, so once the move lands on s0 the
                # draft's OWN internal regeneration shifts every later
                # pairing's slot down by one instead of just the s0 pairing's.
                # If that regeneration runs after the move but the draft's
                # outer preview captured the pre-move (all-on-original-slots)
                # layout, the widened draft_fingerprint (round 7) correctly
                # sees a changed placement and refuses as stale rather than
                # silently committing a schedule the operator never reviewed.
                self.assertIn(_reason(r),
                              ("ice_slot_taken", "slot_unavailable",
                               "slot_already_filled", "team_overlap",
                               "preview_stale"))
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
        # Two-sided (#318 round 2): Postgres may victimize EITHER side of a
        # lock-order inversion; a deadlock surfacing on the placement must
        # fail this race just as loudly as one on the builder.
        self.assertNotEqual(_reason(out[1]), "deadlock_detected", out)
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
        self._builder_vs("se2", lambda a: commit_fresh_draft(a, "d1"))

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
            lambda a: commit_fresh_draft(a, "d1", slot_ids=["mB"]),
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
            lambda a: commit_fresh_draft(a, "d1", slot_ids=["mC"]),
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


# -- draft-commit must also revalidate TEAM PARTICIPATION under the lock
# (#314 review, follow-up) ---------------------------------------------------
#
# draft_season_schedule (used to GENERATE a proposal) already excludes an
# unregistered or league-transferred team from any FRESH pairing — verified
# empirically, not assumed. So a purely sequential "unregister/transfer, THEN
# call commit_draft_schedule" never reaches the new recheck at all: the
# regenerated proposal is already correct, and the commit either succeeds with
# a substitute pairing or (if no valid pairing remains) creates nothing — no
# error, nothing for SetupService._require_batch_team_participation to catch.
# The recheck's OWN value is closing the FORCED race: a proposal generated
# WHILE the team was still valid, raced against a participation change that
# commits before commit_draft_schedule's OWN Team/Rink/Season locks (and this
# recheck) run. Only a forced two-thread PostgreSQL race can land in that
# window, so it — not a sequential rewrite — is this fix's real regression
# test; see PostgresDraftParticipationRaceMixin below. Memory/SQLite parity is
# the OUTCOME-level invariant a sequential run CAN prove (never a Game for an
# excluded team, whichever mechanism catches it) plus a direct unit test of
# the new helper in isolation.
def _active_games(store):
    return [g for g in store.all_games() if not g.cancelled]


class RequireBatchTeamParticipationUnitTest(unittest.TestCase):
    """Direct, backend-agnostic coverage of the new shared helper: it must
    raise the identical DivisionMismatchError create_game's own registration
    check would, for a team that is unregistered or has since been
    transferred to another league — and pass cleanly for a fully-valid batch.
    Exercised directly (not only through commit_draft_schedule) so its
    correctness doesn't depend on whether draft_season_schedule would ever
    itself hand it a stale row (verified above: it wouldn't)."""

    def setUp(self):
        self.store = InMemoryStore()
        _seed(self.store)
        self.api = ApiService(self.store)
        self.rows = [{"home_team_id": "t0", "away_team_id": "t3",
                     "division_id": "d1"}]

    def test_passes_for_a_fully_valid_batch(self):
        self.api.setup._require_batch_team_participation(
            "se1", "ls1", self.rows)

    def test_raises_for_an_unregistered_team(self):
        self.api.unregister_team_from_season("reg1_t0")
        with self.assertRaises(DomainError) as ctx:
            self.api.setup._require_batch_team_participation(
                "se1", "ls1", self.rows)
        self.assertEqual(ctx.exception.details["reason"], "team_not_registered")

    def test_raises_for_a_transferred_team(self):
        self.store.add_league(League(id="lg2", program_id="pg", name="League 2"))
        self.api.transfer_team_to_league("t0", "lg2")
        with self.assertRaises(DomainError) as ctx:
            self.api.setup._require_batch_team_participation(
                "se1", "ls1", self.rows)
        self.assertEqual(
            ctx.exception.details["reason"], "team_not_in_league_season")

    def test_division_less_row_relaxes_to_season_only(self):
        # A League-wide draft row can carry no division (#233 Slice G) — the
        # check must still require an ACTIVE season registration, just not a
        # division match.
        self.api.unregister_team_from_season("reg1_t0")
        rows = [{"home_team_id": "t0", "away_team_id": "t3", "division_id": None}]
        with self.assertRaises(DomainError) as ctx:
            self.api.setup._require_batch_team_participation(
                "se1", "ls1", rows)
        self.assertEqual(ctx.exception.details["reason"], "team_not_registered")

    def test_division_less_row_still_requires_the_exact_league_season(self):
        self.store.add_league(League(
            id="lg2", program_id="pg", name="League 2"))
        self.api.transfer_team_to_league("t0", "lg2")
        rows = [{"home_team_id": "t0", "away_team_id": "t3",
                 "division_id": None}]
        with self.assertRaises(DomainError) as ctx:
            self.api.setup._require_batch_team_participation(
                "se1", "ls1", rows)
        self.assertEqual(
            ctx.exception.details["reason"], "team_not_in_league_season")


class _DraftParticipationParityMixin:
    """Sequential Memory/SQLite/Postgres coverage of the OUTCOME-level
    invariant: whichever of {unregister, transfer} runs before a
    commit_draft_schedule call, the commit NEVER creates a Game for the
    now-excluded team — proven here via draft_season_schedule's own fresh
    exclusion (not this fix's new under-lock recheck; see the race mixin
    below for that)."""

    def _api_cls(self):
        raise NotImplementedError

    def _make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self._make_store()
        _seed(self.store)
        self.api = self._api_cls()(self.store)

    def test_draft_commit_excludes_a_team_unregistered_first(self):
        r = self.api.unregister_team_from_season("reg1_t0")
        self.assertNotIn("error", r)
        res = commit_fresh_draft(self.api, "d1", slot_ids=["s0"])
        self.assertNotIn("error", res, repr(res))
        self.assertEqual(len(res["created"]), 1, repr(res))
        row = res["created"][0]
        self.assertNotIn("T0", (row["home_team_name"], row["away_team_name"]))
        self.assertTrue(all(
            "t0" not in (g.home_team_id, g.away_team_id) for g in
            _active_games(self.store)))

    def test_draft_commit_excludes_a_team_transferred_first(self):
        self.store.add_league(League(id="lg2", program_id="pg", name="League 2"))
        r = self.api.transfer_team_to_league("t0", "lg2")
        self.assertNotIn("error", r)
        res = commit_fresh_draft(self.api, "d1", slot_ids=["s0"])
        self.assertNotIn("error", res, repr(res))
        self.assertEqual(len(res["created"]), 1, repr(res))
        row = res["created"][0]
        self.assertNotIn("T0", (row["home_team_name"], row["away_team_name"]))

    def test_committed_draft_blocks_unregister_that_would_strand_it(self):
        res = commit_fresh_draft(self.api, "d1", slot_ids=["s0"])
        self.assertNotIn("error", res, repr(res))
        game = _active_games(self.store)[0]
        team_id = game.home_team_id
        result = self.api.unregister_team_from_season(f"reg1_{team_id}")
        self.assertEqual(_reason(result), "team_has_scheduled_games", repr(result))
        self.assertTrue(
            self.store.get_season_team_registration(
                f"reg1_{team_id}").active)
        self.assertFalse(any(
            a.action == "season_team_unregistered"
            and a.entity_id == f"reg1_{team_id}"
            for a in self.store.all_setup_audit()))

    def test_committed_draft_blocks_transfer_that_would_strand_it(self):
        res = commit_fresh_draft(self.api, "d1", slot_ids=["s0"])
        self.assertNotIn("error", res, repr(res))
        game = _active_games(self.store)[0]
        team_id = game.home_team_id
        self.store.add_league(League(
            id="lg2", program_id="pg", name="League 2"))
        result = self.api.transfer_team_to_league(team_id, "lg2")
        self.assertEqual(
            _reason(result), "team_transfer_strands_games", repr(result))
        self.assertEqual(self.store.get_team(team_id).league_id, "lg")
        self.assertEqual(
            self.store.get_season_team_registration(
                f"reg1_{team_id}").league_season_id,
            "ls1")
        self.assertFalse(any(
            a.action == "team_league_transferred" and a.entity_id == team_id
            for a in self.store.all_setup_audit()))

    def test_committed_draft_blocks_division_reassign_that_would_strand_it(self):
        # assign_season_team_division shares _games_scheduled_for_team_in_
        # season, so the same committed-draft-counts rule applies (#314
        # review) — clearing the division out from under a committed draft
        # is refused with zero writes.
        res = commit_fresh_draft(self.api, "d1", slot_ids=["s0"])
        self.assertNotIn("error", res, repr(res))
        game = _active_games(self.store)[0]
        team_id = game.home_team_id
        result = self.api.assign_season_team_division(
            f"reg1_{team_id}", division_id=None)
        self.assertEqual(_reason(result), "team_has_scheduled_games",
                         repr(result))
        self.assertEqual(
            self.store.get_season_team_registration(
                f"reg1_{team_id}").division_id, "d1")

    def test_committed_draft_blocks_league_reassign_that_would_strand_it(self):
        # assign_season_team_league keeps its own inline duplicate of the
        # stranding filter (league-scoped, not the shared helper) — the #314
        # committed-draft rule was applied there too. Uses a dedicated
        # permanent-League-less team, since a team WITH a permanent League
        # fails the rule-7 mismatch check for any other target League before
        # ever reaching the stranding guard.
        self.store.add_team(Team(id="tL", name="TL", program_id="pg"))
        self.store.add_season_team_registration(SeasonTeamRegistration(
            id="regL_tL", league_season_id="ls1", team_id="tL", active=True))
        self.store.add_league(League(id="lg2", program_id="pg", name="League 2"))
        game = Game(
            id=self.store.next_id("game"), home_team_id="tL",
            away_team_id="t1", start_time=BASE, season_id="se1",
            league_id="lg", league_season_id="ls1", is_draft=True,
            published=False)
        self.store.add_game(game)
        result = self.api.assign_season_team_league("regL_tL", league_id="lg2")
        self.assertEqual(_reason(result),
                         "registration_league_change_strands_games",
                         repr(result))
        self.assertIn(game.id,
                      result["error"]["details"]["affected_game_ids"])
        self.assertEqual(
            self.store.get_season_team_registration(
                "regL_tL").league_season_id, "ls1")


class MemoryDraftParticipationParityTest(_DraftParticipationParityMixin,
                                         unittest.TestCase):
    def _api_cls(self):
        return ApiService

    def _make_store(self):
        return InMemoryStore()


class SqliteDraftParticipationParityTest(_DraftParticipationParityMixin,
                                         unittest.TestCase):
    def _api_cls(self):
        return ApiService

    def _make_store(self):
        return SqlStore(":memory:")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresDraftParticipationParityTest(_DraftParticipationParityMixin,
                                           unittest.TestCase):
    def _api_cls(self):
        return ApiService

    def _make_store(self):
        store = SqlStore(os.environ["TEST_DATABASE_URL"])
        store.clear_all_data()
        return store


class _DraftParticipationRaceMixin(_ForcedRaceHarnessMixin):
    """Forced two-session race: commit_draft_schedule (its proposal generated
    while the team was still valid) vs. a concurrent unregister/transfer for
    that same team (#314 review). Whichever transaction acquires the Season
    lock first wins. If draft commit wins, the later participation change is
    rejected because a committed draft owns allocated ice and must retain valid
    participants. If unregister/transfer wins, the draft either regenerates
    without that team or its under-lock exact-LeagueSeason check rejects the
    stale proposal. Every successful Game must match both teams' current active
    registration and permanent League."""

    def _assert_participation_race(self, participation_call,
                                   expected_draft_reasons,
                                   expected_participation_reason):
        out = self._run([
            lambda a: commit_fresh_draft(a, "d1", slot_ids=["s0"]),
            participation_call,
        ])
        self._assert_no_crash(out)
        draft_res, part_res = out
        store = self._store()
        active = _active_games(store)
        draft_ok = isinstance(draft_res, dict) and "error" not in draft_res
        part_ok = isinstance(part_res, dict) and "error" not in part_res
        if draft_ok:
            self.assertEqual(len(draft_res["created"]), 1, repr(out))
            self.assertEqual(len(active), 1)
        else:
            self.assertIn(
                _reason(draft_res), expected_draft_reasons, repr(out))
            self.assertEqual(len(active), 0)
            self.assertEqual(store.get_ice_slot("s0").status.value, "available")
            self.assertFalse(any(a.action == "draft_schedule_committed"
                                for a in store.all_setup_audit()))

        if part_ok:
            # Two valid transfer/unregister-first outcomes exist. A fresh
            # proposal can exclude t0 and commit another pairing, while a
            # proposal captured before the participation change is rejected
            # by the locked exact-LeagueSeason recheck with zero Games.
            if active:
                self.assertTrue(draft_ok, repr(out))
                self.assertNotIn(
                    "t0",
                    (active[0].home_team_id, active[0].away_team_id),
                    repr(out))
            else:
                self.assertFalse(draft_ok, repr(out))
        else:
            self.assertTrue(draft_ok, repr(out))
            self.assertEqual(
                _reason(part_res), expected_participation_reason, repr(out))
            self.assertIn(
                "t0",
                (active[0].home_team_id, active[0].away_team_id),
                repr(out))

        for game in active:
            self.assertEqual(game.league_season_id, "ls1")
            league_season = store.get_league_season(game.league_season_id)
            for team_id in (game.home_team_id, game.away_team_id):
                team = store.get_team(team_id)
                registration = next((
                    r for r in store.registrations_for_league_season("ls1")
                    if r.team_id == team_id and r.active), None)
                self.assertIsNotNone(registration, repr(out))
                self.assertEqual(team.league_id, league_season.league_id)
        self._assert_schedule_consistent(store)

    def test_unregister_vs_draft_commit(self):
        self._assert_participation_race(
            lambda a: a.unregister_team_from_season("reg1_t0"),
            # #328 review round 5: an unregister that lands between this
            # call's own external preview and its internal regeneration
            # changes which pairings are even eligible, which can trip the
            # new preview-fingerprint gate before ever reaching the
            # participation recheck below -- an equally valid terminal
            # outcome (zero writes either way).
            ("team_not_registered", "preview_stale"),
            "team_has_scheduled_games")

    def test_transfer_vs_draft_commit(self):
        self._store().add_league(
            League(id="lg2", program_id="pg", name="League 2"))
        self._assert_participation_race(
            lambda a: a.transfer_team_to_league("t0", "lg2"),
            ("team_not_registered", "team_not_in_league_season",
             "team_wrong_division", "registration_cross_league",
             "preview_stale"),
            "team_transfer_strands_games")

    def test_divisionless_league_draft_vs_transfer_uses_exact_league_season(self):
        """Force the stale-proposal interleaving that a Division check cannot
        catch: every source registration is Division-less, and the proposal is
        captured while its selected Team still belongs to League A. The write
        then races that Team's transfer to League B in the same Season."""
        seed = self._store()
        for registration in seed.registrations_for_league_season("ls1"):
            registration.division_id = None
            seed.save_season_team_registration(registration)
        seed.add_league(League(
            id="lg2", program_id="pg", name="League 2"))
        proposal = self._api_cls()(seed).draft_season_schedule(
            season_id="se1", league_id="lg", slot_ids=["s0"])
        self.assertNotIn("error", proposal, repr(proposal))
        self.assertEqual(len(proposal["draft_games"]), 1)
        self.assertIsNone(proposal["draft_games"][0]["division_id"])
        selected_team = proposal["draft_games"][0]["home_team_id"]

        def commit_stale_proposal(api):
            api.draft_season_schedule = (
                lambda *args, **kwargs: proposal)
            return commit_fresh_draft(
                api, season_id="se1", league_id="lg", slot_ids=["s0"])

        out = self._run([
            commit_stale_proposal,
            lambda a: a.transfer_team_to_league(selected_team, "lg2"),
        ])
        self._assert_no_crash(out)
        draft_res, transfer_res = out
        store = self._store()
        games = _active_games(store)
        if "error" not in draft_res:
            self.assertEqual(_reason(transfer_res),
                             "team_transfer_strands_games", repr(out))
            self.assertEqual(len(games), 1)
            self.assertEqual(games[0].league_season_id, "ls1")
            self.assertIsNone(games[0].division_id)
            self.assertEqual(store.get_team(selected_team).league_id, "lg")
        else:
            self.assertIn(
                _reason(draft_res),
                ("team_not_in_league_season", "registration_cross_league",
                 "team_not_registered"),
                repr(out))
            self.assertNotIn("error", transfer_res, repr(out))
            self.assertEqual(games, [])
            self.assertEqual(store.get_ice_slot("s0").status.value, "available")
            self.assertEqual(store.get_team(selected_team).league_id, "lg2")
            self.assertFalse(any(
                audit.action == "draft_schedule_committed"
                for audit in store.all_setup_audit()))


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresLeagueScopedDraftParticipationRaceTest(
        _DraftParticipationRaceMixin, unittest.TestCase):
    """Exercises the LEAGUE-SCOPED commit_draft_schedule (api/
    league_scoped_service.py) — the implementation production actually runs."""
    # _api_cls() inherited default (the league-scoped ApiService).


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresBaseApiDraftParticipationRaceTest(
        _DraftParticipationRaceMixin, unittest.TestCase):
    """Exercises the BASE facade's OWN commit_draft_schedule (api/service.py)
    — not reached in production (the league-scoped override always wins
    method resolution), but directly instantiable/testable, and the review
    asked for BOTH implementations to be race-safe, not just the one
    production resolves to."""

    def _api_cls(self):
        return BaseApiService


def _add_extra_teams(store):
    """Two more teams for d1 (#328 review round 6): the multi-row races
    below need each racing side's own team-disjoint "earlier" pairing
    alongside the pairing both sides actually contest, and d1's original 4
    teams supply only ONE pairing ((t1, t2)) disjoint from the contested
    (t0, t3) -- not the two independent ones each side needs."""
    store.add_team(Team(id="t4", name="T4", division="D1", division_id="d1",
                        program_id="pg", league_id="lg"))
    store.add_season_team_registration(SeasonTeamRegistration(
        id="reg1_t4", league_season_id="ls1", team_id="t4",
        division_id="d1", active=True))
    store.add_team(Team(id="t5", name="T5", division="D1", division_id="d1",
                        program_id="pg", league_id="lg"))
    store.add_season_team_registration(SeasonTeamRegistration(
        id="reg1_t5", league_season_id="ls1", team_id="t5",
        division_id="d1", active=True))


def _hand_row(store, home, away, division_id, slot_id):
    """A draft_games row built directly from CURRENT store state, matching
    ``_assign_ice``'s own shape exactly (#328 review round 6) -- lets the
    races below hand-construct a specific pairing onto a specific slot
    without depending on round-robin generation order, which always tries
    pairing[0] first regardless of which slot is targeted."""
    slot = store.get_ice_slot(slot_id)
    rink = store.get_rink(slot.rink_id) if slot.rink_id else None
    return {
        "home_team_id": home, "away_team_id": away,
        "home_team_name": store.get_team(home).name,
        "away_team_name": store.get_team(away).name,
        "division_id": division_id, "ice_slot_id": slot_id,
        "rink_id": slot.rink_id, "rink_name": rink.name if rink else None,
        "start_time": slot.start_time.isoformat(),
        "end_time": slot.end_time.isoformat(),
    }


def _multi_row_proposal(store, unique_pairing, unique_slot, contested_slot,
                        token):
    """Two-row frozen proposal (#328 review round 6): row[0] a pairing
    UNIQUE to this side (never touched by the other side), row[1] the
    pairing BOTH sides contest, on ``contested_slot``. Whichever side loses
    the Team-lock race for the contested pairing has therefore ALREADY
    tentatively created row[0]'s Game within the SAME (about-to-roll-back)
    transaction -- proving the loser's rollback covers the whole batch, not
    merely the contested row -- symmetrically, regardless of which side
    actually loses. ``token`` is an arbitrary fingerprint: both the
    monkeypatched ``draft_season_schedule`` and the ``commit_draft_schedule``
    call below use the SAME frozen object, so the round-5 preview-binding
    check trivially matches without needing a real hash."""
    row0 = _hand_row(store, unique_pairing[0], unique_pairing[1], "d1",
                     unique_slot)
    row1 = _hand_row(store, "t0", "t3", "d1", contested_slot)
    return {
        "division_id": "d1", "season_id": "se1", "league_id": "lg",
        "team_count": 6, "draft_games": [row0, row1], "unscheduled": [],
        "already_scheduled": [], "unschedulable_teams": [],
        "draft_fingerprint": token,
    }


def _run_multi_row_pairing_race(testcase, side_a_slot, side_b_slot):
    """Forced two-session race (#328 review round 6): BOTH racing sides
    commit a hand-constructed, PRE-CAPTURED two-row batch (their own
    team-disjoint earlier pairing, then the shared contested t0-vs-t3
    pairing on ``side_a_slot`` / ``side_b_slot``) -- captured and frozen
    BEFORE the barrier releases either thread. Unlike each side calling its
    own real ``commit_draft_schedule()`` (whose internal
    ``draft_season_schedule()`` regeneration is NOT itself synchronized by
    the barrier), neither side's reviewed batch can be affected by which
    side happens to reach its own regeneration first — the only remaining
    race is exactly the one under test: which side's transaction acquires
    the shared Team lock for (t0, t3) first."""
    store = testcase._store()
    _add_extra_teams(store)
    proposal_a = _multi_row_proposal(
        store, ("t1", "t2"), "s1", side_a_slot, "token-a")
    proposal_b = _multi_row_proposal(
        store, ("t4", "t5"), "s4", side_b_slot, "token-b")

    def attempt(proposal):
        def run(a):
            a.draft_season_schedule = lambda *args, **kw: proposal
            return a.commit_draft_schedule(
                division_id="d1",
                draft_fingerprint=proposal["draft_fingerprint"])
        return run

    out = testcase._run([attempt(proposal_a), attempt(proposal_b)])
    return out, proposal_a, proposal_b


def _assert_multi_row_pairing_race_terminal(testcase, out, proposal_a,
                                            proposal_b):
    """Shared assertion (#328 review round 4, hardened round 6): exactly
    one side wins (BOTH its rows land), the other is refused with the
    terminal ``pairing_already_scheduled`` reason (never
    ``slot_unavailable``/``team_overlap``, which the SAME row would ALSO
    trip physically), naming the winning Game. Because each side's own
    earlier row was tentatively created within its own transaction before
    the shared row was reached, the LOSER's rows -- both of them, not just
    the contested one -- must roll back to AVAILABLE with no audit,
    proving the whole batch, not merely the bad row, rolls back."""
    testcase._assert_no_crash(out)
    oks = [r for r in out if isinstance(r, dict) and "error" not in r]
    errs = [r for r in out if isinstance(r, dict) and "error" in r]
    testcase.assertEqual(len(oks), 1,
                         f"exactly one side should win: {out!r}")
    testcase.assertEqual(len(errs), 1,
                         f"the loser must be refused with the terminal "
                         f"pairing reason, not a physical-conflict one: "
                         f"{out!r}")
    testcase.assertEqual(len(oks[0]["created"]), 2, repr(out))
    testcase.assertEqual(errs[0]["error"]["details"]["reason"],
                         "pairing_already_scheduled", repr(out))
    winning_contested = next(
        g for g in oks[0]["created"]
        if {g["home_team_name"], g["away_team_name"]} == {"T0", "T3"})
    testcase.assertEqual(errs[0]["error"]["details"]["existing_game_id"],
                         winning_contested["game_id"], repr(out))
    store = testcase._store()
    games = [g for g in store.all_games()
             if not g.cancelled and g.division_id == "d1"]
    testcase.assertEqual(len(games), 2,
                         f"only the winner's two Games may exist: {out!r}")
    committed_audits = [a for a in store.all_setup_audit()
                        if a.action == "draft_schedule_committed"]
    testcase.assertEqual(len(committed_audits), 1,
                         f"only the winner's commit may audit-write: {out!r}")
    # out[i] pairs with the i-th proposal by construction (_run preserves
    # index order regardless of completion order) -- so this identifies the
    # LOSING side's own two-row batch, not just "whichever error happened".
    lost_proposal = proposal_a if "error" in out[0] else proposal_b
    # Only the loser's OWN unique row (row[0]) is checked here: it is
    # NEVER touched by the winner in any scenario, so it must always be
    # back to AVAILABLE, proving the earlier row's tentative Game/slot-flip
    # rolled back with the rest of the loser's batch. The loser's contested
    # row (row[1]) is deliberately NOT checked the same way -- in the
    # same-slot scenario its slot is the WINNER's own slot and is correctly
    # left ALLOCATED, so "available" would be the wrong assertion there;
    # the "only 2 Games total, both the winner's" check above already rules
    # out any orphaned write from the loser's contested row.
    unique_row = lost_proposal["draft_games"][0]
    testcase.assertEqual(
        store.get_ice_slot(unique_row["ice_slot_id"]).status.value,
        "available",
        f"loser's own earlier row {unique_row['ice_slot_id']} must roll "
        f"back: {out!r}")
    testcase._assert_schedule_consistent(store)


class _PairingDuplicationRaceMixin(_ForcedRaceHarnessMixin):
    """Forced two-session race (#206 slice 1, terminal contract per #328
    review round 2): two draft commits for the SAME Division, each
    targeting the SAME contested pairing on its own single ice slot, the
    two slots far enough apart in time that they never conflict — proving
    the pairing-identity recheck alone (not any physical check) catches
    this case. Whichever session acquires the shared Team lock first
    commits normally; the loser's per-row physical check
    (``_assert_slot_free_for_game``) would see a genuinely free,
    non-conflicting slot and silently persist a SECOND Game for the
    identical pairing were it not for the fresh commit-time recheck added
    by #206 — that recheck raises the TERMINAL ``pairing_already_scheduled``
    (not ``placement_raced``, so the retry shell does not retry it): the
    loser's whole batch (both its own earlier, uncontested row AND the
    contested one) rolls back atomically and the caller must regenerate
    and re-review, rather than the commit silently substituting a
    different pairing into the batch the operator already reviewed. See
    ``_run_multi_row_pairing_race`` for how both sides' batches are
    pre-captured and frozen before the race (#328 review round 6), and why
    each is two rows (proving whole-batch rollback, not just the
    contested row).

    Falsifiable both ways: drop the commit-time recheck in
    ``_commit_draft_schedule_attempt`` and the loser silently duplicates
    the winner's pairing on its own slot (this test's "exactly one commit
    succeeds" and "exactly one winning pair of Games exists" assertions
    fail); revert the reason to ``placement_raced`` instead of the terminal
    ``pairing_already_scheduled`` and the loser is silently retried to a
    DIFFERENT success instead of a named terminal error (this test's
    "exactly one error" assertion fails)."""

    def test_two_single_slot_draft_commits_never_duplicate_a_pairing(self):
        out, proposal_a, proposal_b = _run_multi_row_pairing_race(
            self, "s0", "s3")
        _assert_multi_row_pairing_race_terminal(
            self, out, proposal_a, proposal_b)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresLeagueScopedPairingDuplicationRaceTest(
        _PairingDuplicationRaceMixin, unittest.TestCase):
    """Exercises the LEAGUE-SCOPED commit_draft_schedule (api/
    league_scoped_service.py) — the implementation production actually
    runs."""
    # _api_cls() inherited default (the league-scoped ApiService).


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresBaseApiPairingDuplicationRaceTest(
        _PairingDuplicationRaceMixin, unittest.TestCase):
    """Exercises the BASE facade's OWN commit_draft_schedule (api/
    service.py) — not reached in production (the league-scoped override
    always wins method resolution), but both implementations fully
    reimplement the commit body, so both must independently enforce the
    same #206 slice 1 invariant."""

    def _api_cls(self):
        return BaseApiService


class _PairingRaceSameSlotMixin(_ForcedRaceHarnessMixin):
    """Forced two-session race (#328 review round 4, hardened round 6):
    two draft commits for the SAME Division, BOTH targeting the SAME
    contested pairing on the IDENTICAL single ice slot. The loser's per-row
    physical check (``_assert_slot_free_for_game``) would find the slot
    itself already ALLOCATED — a genuine ``slot_unavailable`` case — were
    the pairing-identity check not now checked FIRST: the loser must get
    the terminal ``pairing_already_scheduled`` naming the winning Game, not
    ``slot_unavailable``. See ``_run_multi_row_pairing_race`` for how both
    sides' batches are pre-captured and frozen before the race, and why
    each is two rows (proving whole-batch rollback, not just the
    contested row).

    Falsifiable: revert the check order (physical gate before the pairing
    check, as both facades had before round 4) and the loser gets
    ``slot_unavailable`` instead (this test's "exactly one error, reason
    pairing_already_scheduled" assertion fails)."""

    def test_same_slot_pairing_race_wins_with_terminal_reason(self):
        out, proposal_a, proposal_b = _run_multi_row_pairing_race(
            self, "s0", "s0")
        _assert_multi_row_pairing_race_terminal(
            self, out, proposal_a, proposal_b)


class _PairingRaceOverlappingSlotMixin(_ForcedRaceHarnessMixin):
    """Forced two-session race (#328 review round 4, hardened round 6):
    two draft commits for the SAME Division, targeting the SAME contested
    pairing on DIFFERENT single ice slots that physically OVERLAP in time
    (``s0`` on ``r1``, ``sB`` on ``r2`` — the same instant per ``_seed``).
    The loser's per-row physical check would find ``team_overlap`` — a
    genuine diagnosis, but for the SAME pairing that raced, not an
    unrelated one — were the pairing-identity check not now checked FIRST:
    the loser must get the terminal ``pairing_already_scheduled`` naming
    the winning Game, not ``team_overlap``. See
    ``_run_multi_row_pairing_race`` for how both sides' batches are
    pre-captured and frozen before the race, and why each is two rows.

    Falsifiable: revert the check order and the loser gets ``team_overlap``
    instead (this test's "exactly one error, reason
    pairing_already_scheduled" assertion fails)."""

    def test_overlapping_slot_pairing_race_wins_with_terminal_reason(self):
        out, proposal_a, proposal_b = _run_multi_row_pairing_race(
            self, "s0", "sB")
        _assert_multi_row_pairing_race_terminal(
            self, out, proposal_a, proposal_b)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresLeagueScopedPairingRaceSameSlotTest(
        _PairingRaceSameSlotMixin, unittest.TestCase):
    """Exercises the LEAGUE-SCOPED commit_draft_schedule."""
    # _api_cls() inherited default (the league-scoped ApiService).


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresBaseApiPairingRaceSameSlotTest(
        _PairingRaceSameSlotMixin, unittest.TestCase):
    """Exercises the BASE facade's OWN commit_draft_schedule — not reached
    in production, but both implementations fully reimplement the commit
    body, so both must independently enforce the same priority."""

    def _api_cls(self):
        return BaseApiService


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresLeagueScopedPairingRaceOverlappingSlotTest(
        _PairingRaceOverlappingSlotMixin, unittest.TestCase):
    """Exercises the LEAGUE-SCOPED commit_draft_schedule."""
    # _api_cls() inherited default (the league-scoped ApiService).


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresBaseApiPairingRaceOverlappingSlotTest(
        _PairingRaceOverlappingSlotMixin, unittest.TestCase):
    """Exercises the BASE facade's OWN commit_draft_schedule — not reached
    in production, but both implementations fully reimplement the commit
    body, so both must independently enforce the same priority."""

    def _api_cls(self):
        return BaseApiService


if __name__ == "__main__":
    unittest.main()
