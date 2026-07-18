"""#283 Slice E re-review regressions — runtime/service data-integrity blockers.

These pin the fixes for the second-review blockers that live in the service
layer (the migration-037 gate lives in test_competition_reset_migration.py and
the import blockers in test_import_permanent_league.py), on both stores:

  * Blocker 1 (fail-closed): a REGULAR game whose ``league_season_id`` is
    missing can be neither PUBLISHED nor MOVED — both refuse with
    ``regular_game_missing_league_season`` and mutate nothing.
  * Blocker 2 (history): transferring a Team never touches an ENDED Season's
    active registration — it is history and stays in its old League, while the
    Team's permanent League still changes.
  * Blocker 3 (no League-less Teams): ``create_team`` requires a resolvable
    permanent League — it refuses a Program with several Leagues and no other
    signal (``team_league_required``); and registering a (legacy) League-less
    Team stamps the Team's permanent League from the LeagueSeason.
"""

import os
import unittest
from datetime import datetime, timedelta, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import GameType, IceSlotStatus
from hockey_scheduler.domain.setup_models import SeasonTeamRegistration
from hockey_scheduler.services.league_scope import (
    registered_team_ids_in_division,
    registered_teams_by_division_in_league,
)
from hockey_scheduler.store import InMemoryStore, SqlStore

ADMIN = "admin"
UTC = timezone.utc


def _at(hour):
    return datetime(2026, 9, 1, hour, tzinfo=UTC)


class _Contract:
    """Shared body; subclasses supply ``make_store`` (Memory or SQLite)."""

    def setUp(self):
        self.api = ApiService(self.make_store())
        self.org = self.api.create_organization("Org", "O", actor_id=ADMIN)
        self.program = self.api.create_program(
            "Prog", operator_organization_id=self.org["id"], actor_id=ADMIN)
        self.season = self.api.create_season(
            self.program["id"], "Fall", actor_id=ADMIN)
        self.elite = self.api.create_league(
            self.season["id"], "Elite", actor_id=ADMIN)
        self.rec = self.api.create_league(
            self.season["id"], "Rec", actor_id=ADMIN)
        self.club = self.api.create_club("C", actor_id=ADMIN)
        self.venue = self.api.create_venue(
            "V", organization_id=self.org["id"], league_id=self.program["id"],
            actor_id=ADMIN)
        self.api.grant_season_venue_access(
            self.season["id"], self.venue["id"], actor_id=ADMIN)
        self.rink = self.api.create_rink(self.venue["id"], "R", actor_id=ADMIN)

    def tearDown(self):
        conn = getattr(self.api.store, "conn", None)
        if conn is not None:
            conn.close()

    # -- helpers ---------------------------------------------------------
    def _team(self, name, league_id):
        t = self.api.create_team(self.club["id"], None, name, actor_id=ADMIN,
                                 league_id=league_id)
        self.api.register_team_for_season(
            self.season["id"], t["id"], actor_id=ADMIN, league_id=league_id)
        return t

    def _slot(self, hour):
        return self.api.create_ice_slot(
            self.rink["id"], _at(hour).isoformat(), _at(hour + 1).isoformat(),
            "game", actor_id=ADMIN)

    def _audit_count(self):
        return len(self.api.store.all_setup_audit())

    def _active_regs(self, team_id):
        return [r for r in self.api.store.registrations_for_season(self.season["id"])
                if r.team_id == team_id and r.active]

    # -- Blocker 1: runtime fail-closed on a null league_season_id -------
    def _regular_game_then_strip_league_season(self):
        """Create a valid REGULAR game, then simulate a legacy/unscoped row by
        nulling its ``league_season_id`` in the store directly."""
        a = self._team("A", self.elite["id"])
        b = self._team("B", self.elite["id"])
        g = self.api.create_game(
            self.season["id"], None, a["id"], b["id"], self._slot(18)["id"],
            actor_id=ADMIN, league_id=self.elite["id"])
        self.assertNotIn("error", g, g)
        stored = self.api.store.get_game(g["id"])
        stored.league_season_id = None
        self.api.store.save_game(stored)
        return g

    def test_publish_regular_game_without_league_season_fails_closed(self):
        g = self._regular_game_then_strip_league_season()
        before = self._audit_count()
        res = self.api.publish_game(g["id"], actor_id=ADMIN)
        self.assertEqual(res["error"]["details"]["reason"],
                         "regular_game_missing_league_season", res)
        # Zero mutation: still unpublished, no audit entry.
        self.assertFalse(self.api.store.get_game(g["id"]).published)
        self.assertEqual(self._audit_count(), before)

    def test_move_regular_game_without_league_season_fails_closed(self):
        g = self._regular_game_then_strip_league_season()
        dest = self._slot(20)
        before = self._audit_count()
        res = self.api.move_game(g["id"], dest["id"], actor_id=ADMIN)
        self.assertEqual(res["error"]["details"]["reason"],
                         "regular_game_missing_league_season", res)
        # Zero mutation: game stays on its original slot; destination untouched.
        self.assertEqual(self.api.store.get_ice_slot(dest["id"]).status,
                         IceSlotStatus.AVAILABLE)
        self.assertEqual(self._audit_count(), before)

    # -- Blocker 2: transfer preserves an ENDED-Season active registration --
    def test_transfer_never_moves_ended_season_active_registration(self):
        t = self._team("Historic", self.elite["id"])
        reg = self._active_regs(t["id"])[0]
        # The Season has DEFINITELY ended: mark its end_date in the past while
        # the registration is still active (history that must not be rewritten).
        season = self.api.store.get_season(self.season["id"])
        season.end_date = datetime.now(UTC) - timedelta(days=30)
        self.api.store.save_season(season)
        ls_elite = self.api.store.league_season_for(
            self.elite["id"], self.season["id"])

        moved = self.api.transfer_team_to_league(
            t["id"], self.rec["id"], actor_id=ADMIN)
        self.assertNotIn("error", moved, moved)
        # The permanent League changed, but the ended Season's active
        # registration stayed exactly where it was (Elite) — untouched history.
        self.assertEqual(self.api.store.get_team(t["id"]).league_id,
                         self.rec["id"])
        stored = self.api.store.get_season_team_registration(reg.id)
        self.assertTrue(stored.active)
        self.assertEqual(stored.league_season_id, ls_elite.id)

    # -- Blocker 3: no League-less Team is ever created ------------------
    def test_create_team_program_only_multiple_leagues_is_rejected(self):
        # The Program has two Leagues (Elite, Rec) and no other signal → the
        # service can't pick one, so it refuses rather than persist a
        # League-less Team.
        before = self._audit_count()
        res = self.api.create_team(
            self.club["id"], None, "Ambiguous", actor_id=ADMIN,
            program_id=self.program["id"])
        self.assertEqual(res["error"]["details"]["reason"],
                         "team_league_required", res)
        self.assertEqual(self._audit_count(), before)

    def test_register_stamps_permanent_league_on_league_less_team(self):
        # A legacy League-less Team injected directly into the store (bypassing
        # the service guard) gets its permanent League stamped the moment it is
        # registered into a LeagueSeason.
        from hockey_scheduler.domain import Team
        t = Team(id="team_legacy", name="Legacy", club_id=self.club["id"],
                 program_id=self.program["id"], league_id=None)
        self.api.store.add_team(t)
        self.assertIsNone(self.api.store.get_team(t.id).league_id)
        res = self.api.register_team_for_season(
            self.season["id"], t.id, actor_id=ADMIN, league_id=self.elite["id"])
        self.assertNotIn("error", res, res)
        self.assertEqual(self.api.store.get_team(t.id).league_id,
                         self.elite["id"])


    # -- Blocker: standings key on league_season_id, fail closed on drift ---
    def _final(self, game_id, home_score, away_score):
        self.api.record_result(game_id, home_score, away_score, actor_id=ADMIN)
        self.api.approve_result(game_id, actor_id=ADMIN)

    def _elite_regular_final_game(self):
        a = self._team("A", self.elite["id"])
        b = self._team("B", self.elite["id"])
        g = self.api.create_game(
            self.season["id"], None, a["id"], b["id"], self._slot(18)["id"],
            actor_id=ADMIN, league_id=self.elite["id"])
        self.assertNotIn("error", g, g)
        self._final(g["id"], 3, 1)
        return g

    def test_standings_fail_closed_on_missing_league_season(self):
        g = self._elite_regular_final_game()
        stored = self.api.store.get_game(g["id"])
        stored.league_season_id = None  # legacy pair still says Elite → drift
        self.api.store.save_game(stored)
        res = self.api.get_league_season_standings(
            self.elite["id"], self.season["id"])
        self.assertEqual(res["error"]["details"]["reason"],
                         "game_league_season_mismatch", res)

    def test_standings_fail_closed_on_mismatched_league_season(self):
        g = self._elite_regular_final_game()
        # Point the game at Rec's LeagueSeason while its legacy fields say Elite.
        rec_ls = self.api.store.league_season_for(self.rec["id"], self.season["id"])
        stored = self.api.store.get_game(g["id"])
        stored.league_season_id = rec_ls.id
        self.api.store.save_game(stored)
        res = self.api.get_league_season_standings(
            self.elite["id"], self.season["id"])
        self.assertEqual(res["error"]["details"]["reason"],
                         "game_league_season_mismatch", res)

    def test_public_standings_fail_closed_on_drift(self):
        g = self._elite_regular_final_game()
        stored = self.api.store.get_game(g["id"])
        stored.league_season_id = None
        self.api.store.save_game(stored)
        res = self.api.get_public_league_season_standings(
            self.elite["id"], self.season["id"])
        self.assertEqual(res["error"]["details"]["reason"],
                         "game_league_season_mismatch", res)

    def test_standings_count_only_the_exact_league_season(self):
        # A correctly-scoped Elite game counts; an identical Rec-scoped game in
        # the same Season never leaks into Elite's table (keyed on
        # league_season_id, not the shared Season).
        g = self._elite_regular_final_game()
        home = self.api.store.get_game(g["id"]).home_team_id
        res = self.api.get_league_season_standings(
            self.elite["id"], self.season["id"])
        self.assertNotIn("error", res, res)
        by_team = {r["team_id"]: r for r in res["standings"]}
        self.assertEqual(by_team[home]["pts"], 2)
        # Rec's standings are empty — Elite's game did not bleed across.
        rec = self.api.get_league_season_standings(
            self.rec["id"], self.season["id"])
        self.assertNotIn("error", rec, rec)
        self.assertTrue(all(r["pts"] == 0 for r in rec["standings"]))


    # -- history: an ended-Season transfer never changes historical standings --
    def _end_the_season(self):
        season = self.api.store.get_season(self.season["id"])
        season.end_date = datetime.now(UTC) - timedelta(days=30)
        self.api.store.save_season(season)

    def _ls_standings(self, public):
        fn = (self.api.get_public_league_season_standings if public
              else self.api.get_league_season_standings)
        res = fn(self.elite["id"], self.season["id"])
        self.assertNotIn("error", res, res)
        return res["standings"]

    def _assert_ended_season_history_unchanged(self, public):
        # A completed Elite Season: A beats B 3-1 (FINAL). For the public table
        # the Game is published; for the private table it stays unpublished.
        a = self._team("A", self.elite["id"])
        b = self._team("B", self.elite["id"])
        g = self.api.create_game(
            self.season["id"], None, a["id"], b["id"], self._slot(18)["id"],
            actor_id=ADMIN, league_id=self.elite["id"])
        self.assertNotIn("error", g, g)
        if public:
            self.assertNotIn(
                "error", self.api.publish_game(g["id"], actor_id=ADMIN))
        self._final(g["id"], 3, 1)
        self._end_the_season()

        before = self._ls_standings(public)
        reg = self._active_regs(a["id"])[0]
        reg_before = (reg.id, reg.team_id, reg.league_season_id,
                      reg.division_id, reg.active)
        gs = self.api.store.get_game(g["id"])
        game_before = (gs.home_team_id, gs.away_team_id, gs.league_id,
                       gs.season_id, gs.league_season_id, gs.game_type,
                       gs.published)
        rs = self.api.store.result_for_game(g["id"])
        result_before = (rs.home_score, rs.away_score, rs.status)

        moved = self.api.transfer_team_to_league(
            a["id"], self.rec["id"], actor_id=ADMIN)
        self.assertNotIn("error", moved, moved)

        # The completed Season is byte-identical: standings, registration,
        # Game, and result all unchanged — the transferred Team still appears
        # in its own historical table with its 2 points.
        after = self._ls_standings(public)
        self.assertEqual(after, before)
        by_team = {r["team_id"]: r for r in after}
        self.assertIn(a["id"], by_team)
        self.assertEqual(by_team[a["id"]]["pts"], 2)
        ra = self.api.store.get_season_team_registration(reg.id)
        self.assertEqual((ra.id, ra.team_id, ra.league_season_id,
                          ra.division_id, ra.active), reg_before)
        ga = self.api.store.get_game(g["id"])
        self.assertEqual((ga.home_team_id, ga.away_team_id, ga.league_id,
                          ga.season_id, ga.league_season_id, ga.game_type,
                          ga.published), game_before)
        rra = self.api.store.result_for_game(g["id"])
        self.assertEqual((rra.home_score, rra.away_score, rra.status),
                         result_before)
        # Current/future eligibility follows the NEW permanent League.
        self.assertEqual(self.api.store.get_team(a["id"]).league_id,
                         self.rec["id"])

    def test_transfer_preserves_ended_season_private_standings(self):
        self._assert_ended_season_history_unchanged(public=False)

    def test_transfer_preserves_ended_season_public_standings(self):
        self._assert_ended_season_history_unchanged(public=True)

    def test_live_season_league_mismatch_excluded_from_standings(self):
        # A CURRENT (undated) Season: A is registered in Elite and plays a FINAL
        # game, but a data-integrity drift leaves A's permanent League as Rec
        # (the migration-preserved-current-registration case). On a live Season
        # A must NOT enter Elite's standings (rule 7); B (still consistent) does.
        a = self._team("A", self.elite["id"])
        b = self._team("B", self.elite["id"])
        g = self.api.create_game(
            self.season["id"], None, a["id"], b["id"], self._slot(18)["id"],
            actor_id=ADMIN, league_id=self.elite["id"])
        self.assertNotIn("error", g, g)
        self._final(g["id"], 3, 1)
        team = self.api.store.get_team(a["id"])
        team.league_id = self.rec["id"]  # drift: current League now differs
        self.api.store.save_team(team)
        res = self.api.get_league_season_standings(
            self.elite["id"], self.season["id"])
        self.assertNotIn("error", res, res)
        ids = {r["team_id"] for r in res["standings"]}
        self.assertNotIn(a["id"], ids)
        self.assertIn(b["id"], ids)

    def test_valid_current_transfer_appears_only_in_target_league_season(self):
        # A game-free CURRENT registration: transferring A from Elite to Rec
        # moves its registration, so A leaves Elite's standings and appears in
        # Rec's — never both.
        a = self._team("A", self.elite["id"])
        moved = self.api.transfer_team_to_league(
            a["id"], self.rec["id"], actor_id=ADMIN)
        self.assertNotIn("error", moved, moved)
        elite = self.api.get_league_season_standings(
            self.elite["id"], self.season["id"])
        rec = self.api.get_league_season_standings(
            self.rec["id"], self.season["id"])
        self.assertNotIn("error", elite, elite)
        self.assertNotIn("error", rec, rec)
        self.assertNotIn(a["id"], {r["team_id"] for r in elite["standings"]})
        self.assertIn(a["id"], {r["team_id"] for r in rec["standings"]})


    # -- Blocker: same-Program cross-League registration drift is not trusted --
    def _cross_league_drift(self):
        """Elite (League A) Division dA with a legit Elite team X registered in
        it, plus a Rec (League B) team B injected with an ACTIVE registration
        into Elite's LeagueSeason/dA — a same-Program cross-League drift. Returns
        (dA, x, b)."""
        dA = self.api.create_division_v2(self.elite["id"], "DA", actor_id=ADMIN)
        x = self.api.create_team(self.club["id"], None, "X", actor_id=ADMIN,
                                 league_id=self.elite["id"])
        self.api.register_team_for_season(
            self.season["id"], x["id"], dA["id"], actor_id=ADMIN,
            league_id=self.elite["id"])
        b = self.api.create_team(self.club["id"], None, "B", actor_id=ADMIN,
                                 league_id=self.rec["id"])
        elite_ls = self.api.store.league_season_for(
            self.elite["id"], self.season["id"])
        self.api.store.add_season_team_registration(SeasonTeamRegistration(
            id=self.api.store.next_id("reg"), league_season_id=elite_ls.id,
            team_id=b["id"], division_id=dA["id"], active=True))
        return dA, x, b

    def test_cross_league_drift_excluded_from_rosters(self):
        dA, x, b = self._cross_league_drift()
        # The shared roster both draft generation and standings read excludes B.
        self.assertEqual(
            registered_team_ids_in_division(self.api.store, dA["id"]), {x["id"]})
        groups = registered_teams_by_division_in_league(
            self.api.store, self.season["id"], self.elite["id"])
        self.assertEqual(groups.get(dA["id"], set()), {x["id"]})

    def test_cross_league_drift_rejected_by_manual_regular_create(self):
        dA, x, b = self._cross_league_drift()
        slot = self._slot(18)
        before = self._audit_count()
        res = self.api.create_game(
            self.season["id"], dA["id"], x["id"], b["id"], slot["id"],
            actor_id=ADMIN, league_id=self.elite["id"])
        self.assertIn("error", res, res)
        # Zero mutation: no Game, slot still available, no audit.
        self.assertEqual(self.api.store.all_games(), [])
        self.assertEqual(self.api.store.get_ice_slot(slot["id"]).status,
                         IceSlotStatus.AVAILABLE)
        self.assertEqual(self._audit_count(), before)

    def test_cross_league_drift_excluded_from_standings(self):
        dA, x, b = self._cross_league_drift()
        div = self.api.get_standings(dA["id"])
        self.assertNotIn("error", div, div)
        self.assertNotIn(b["id"], {r["team_id"] for r in div["standings"]})
        ls = self.api.get_league_season_standings(
            self.elite["id"], self.season["id"])
        self.assertNotIn("error", ls, ls)
        self.assertNotIn(b["id"], {r["team_id"] for r in ls["standings"]})

    def test_legit_cross_league_exhibition_still_allowed(self):
        # An Exhibition between two teams each validly registered in their OWN
        # League (Elite X, Rec Y) is unaffected by the drift guard.
        _dA, x, _b = self._cross_league_drift()
        y = self._team("Y", self.rec["id"])
        exh = self.api.create_game(
            self.season["id"], None, x["id"], y["id"], self._slot(20)["id"],
            actor_id=ADMIN, game_type=GameType.EXHIBITION.value)
        self.assertNotIn("error", exh, exh)
        self.assertEqual(exh["game_type"], "exhibition")


class MemoryBlockerRegressionTest(_Contract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


class DurableBlockerRegressionTest(_Contract, unittest.TestCase):
    def make_store(self):
        url = os.environ.get("TEST_DATABASE_URL") or ":memory:"
        store = SqlStore(url)
        store.reset_schema()
        return store


if __name__ == "__main__":
    unittest.main()
