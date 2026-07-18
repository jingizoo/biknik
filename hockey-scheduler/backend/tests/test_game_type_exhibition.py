"""Exhibition game type + LeagueSeason standings (#283 Slice D).

A Game is REGULAR (counts toward standings, bound to one LeagueSeason — both
teams registered in it, so cross-League/cross-Program pairings are rejected:
rules 8/9) or EXHIBITION (a friendly that MAY cross League lines within a Season
and NEVER affects standings; it carries no owning League and no Division).

These pin, on both the in-memory and SQLite stores:
  * A REGULAR game across two Leagues is rejected (rule 9) — the only way to
    pair cross-League teams is an EXHIBITION.
  * An EXHIBITION game across two Leagues of the same Program is created, with
    game_type="exhibition", no owning league_id and no division_id.
  * An EXHIBITION's FINAL result NEVER moves the standings, while an otherwise
    identical REGULAR game does.
  * The LeagueSeason-wide standings aggregate every Division (and division-less
    teams) of the LeagueSeason, from REGULAR games only.
  * An unknown game_type is a stable validation error (zero mutation).
  * An EXHIBITION still requires both teams to be active season participants.
"""

import os
import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import GameType, IceSlotStatus, ResultStatus
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
        # Two permanent Leagues in this Season (each its own LeagueSeason).
        self.elite = self.api.create_league(
            self.season["id"], "Elite", actor_id=ADMIN)
        self.rec = self.api.create_league(
            self.season["id"], "Rec", actor_id=ADMIN)
        self.club = self.api.create_club("C", actor_id=ADMIN)
        # One venue with season access so game ice is eligible.
        self.venue = self.api.create_venue(
            "V", organization_id=self.org["id"], league_id=self.program["id"],
            actor_id=ADMIN)
        self.api.grant_season_venue_access(
            self.season["id"], self.venue["id"], actor_id=ADMIN)
        self.rink = self.api.create_rink(self.venue["id"], "R", actor_id=ADMIN)

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

    def _final(self, game_id, home_score, away_score):
        self.api.record_result(game_id, home_score, away_score, actor_id=ADMIN)
        self.api.approve_result(game_id, actor_id=ADMIN)

    # -- tests -----------------------------------------------------------
    def test_regular_cross_league_game_is_rejected_zero_mutation(self):
        a = self._team("A", self.elite["id"])
        b = self._team("B", self.rec["id"])
        slot = self._slot(18)
        before = self._audit_count()
        # A REGULAR game scoped to Elite while B is a Rec team → rejected.
        res = self.api.create_game(
            self.season["id"], None, a["id"], b["id"], slot["id"],
            actor_id=ADMIN, league_id=self.elite["id"])
        self.assertEqual(res["error"]["code"], "validation_error", res)
        self.assertEqual(self.api.store.all_games(), [])
        self.assertEqual(self.api.store.get_ice_slot(slot["id"]).status,
                         IceSlotStatus.AVAILABLE)
        self.assertEqual(self._audit_count(), before)

    def test_exhibition_across_leagues_is_allowed(self):
        a = self._team("A", self.elite["id"])
        b = self._team("B", self.rec["id"])
        slot = self._slot(18)
        res = self.api.create_game(
            self.season["id"], None, a["id"], b["id"], slot["id"],
            actor_id=ADMIN, league_id=self.elite["id"],
            game_type=GameType.EXHIBITION.value)
        self.assertNotIn("error", res, res)
        self.assertEqual(res["game_type"], "exhibition")
        # A friendly carries no owning League, Division, or LeagueSeason.
        self.assertIsNone(res["league_id"])
        self.assertIsNone(res["division_id"])
        self.assertIsNone(res["league_season_id"])
        self.assertEqual(res["season_id"], self.season["id"])

    def test_regular_game_references_its_league_season(self):
        a = self._team("A", self.elite["id"])
        b = self._team("B", self.elite["id"])
        res = self.api.create_game(
            self.season["id"], None, a["id"], b["id"], self._slot(18)["id"],
            actor_id=ADMIN, league_id=self.elite["id"])
        self.assertNotIn("error", res, res)
        # #283 Slice E: a regular game references the exact LeagueSeason for its
        # (league, season) pair — its single competition identity.
        ls = self.api.store.league_season_for(self.elite["id"], self.season["id"])
        self.assertIsNotNone(ls)
        self.assertEqual(res["league_season_id"], ls.id)

    def test_unknown_game_type_is_rejected_zero_mutation(self):
        a = self._team("A", self.elite["id"])
        b = self._team("B", self.elite["id"])
        slot = self._slot(18)
        before = self._audit_count()
        res = self.api.create_game(
            self.season["id"], None, a["id"], b["id"], slot["id"],
            actor_id=ADMIN, league_id=self.elite["id"], game_type="scrimmage")
        self.assertEqual(res["error"]["code"], "validation_error", res)
        self.assertEqual(res["error"]["details"]["reason"], "unknown_game_type")
        self.assertEqual(self.api.store.all_games(), [])
        self.assertEqual(self._audit_count(), before)

    def test_exhibition_requires_both_teams_registered_in_season(self):
        a = self._team("A", self.elite["id"])
        # b is a real permanent team but NOT registered this season.
        b = self.api.create_team(self.club["id"], None, "B", actor_id=ADMIN,
                                 league_id=self.rec["id"])
        slot = self._slot(18)
        res = self.api.create_game(
            self.season["id"], None, a["id"], b["id"], slot["id"],
            actor_id=ADMIN, game_type=GameType.EXHIBITION.value)
        self.assertIn("error", res, res)
        self.assertEqual(self.api.store.all_games(), [])

    def test_exhibition_result_never_moves_standings(self):
        # Two Elite teams (same division-less League), one REGULAR + one
        # EXHIBITION game between them, both FINAL.
        a = self._team("A", self.elite["id"])
        b = self._team("B", self.elite["id"])
        reg = self.api.create_game(
            self.season["id"], None, a["id"], b["id"], self._slot(18)["id"],
            actor_id=ADMIN, league_id=self.elite["id"])
        self.assertNotIn("error", reg, reg)
        self._final(reg["id"], 3, 1)  # A beats B in the REGULAR game

        exh = self.api.create_game(
            self.season["id"], None, a["id"], b["id"], self._slot(20)["id"],
            actor_id=ADMIN, game_type=GameType.EXHIBITION.value)
        self.assertNotIn("error", exh, exh)
        self._final(exh["id"], 0, 9)  # B thrashes A — but it's a friendly

        # LeagueSeason standings reflect ONLY the regular game: A 2 pts, B 0.
        st = self.api.get_league_season_standings(
            self.elite["id"], self.season["id"])
        by_team = {r["team_id"]: r for r in st["standings"]}
        self.assertEqual(by_team[a["id"]]["pts"], 2)
        self.assertEqual(by_team[a["id"]]["gf"], 3)
        self.assertEqual(by_team[a["id"]]["ga"], 1)
        self.assertEqual(by_team[b["id"]]["pts"], 0)
        # The exhibition's 9 goals never entered the table.
        self.assertEqual(by_team[b["id"]]["gf"], 1)

    def test_league_season_standings_aggregate_across_divisions(self):
        # Two Divisions inside the Elite LeagueSeason; a regular game in each.
        d1 = self.api.create_division_v2(self.elite["id"], "North", actor_id=ADMIN)
        d2 = self.api.create_division_v2(self.elite["id"], "South", actor_id=ADMIN)
        teams = {}
        for name, div in (("N1", d1), ("N2", d1), ("S1", d2), ("S2", d2)):
            t = self.api.create_team(self.club["id"], None, name, actor_id=ADMIN,
                                     league_id=self.elite["id"])
            self.api.register_team_for_season(
                self.season["id"], t["id"], div["id"], actor_id=ADMIN,
                league_id=self.elite["id"])
            teams[name] = t
        g1 = self.api.create_game(
            self.season["id"], d1["id"], teams["N1"]["id"], teams["N2"]["id"],
            self._slot(18)["id"], actor_id=ADMIN, league_id=self.elite["id"])
        g2 = self.api.create_game(
            self.season["id"], d2["id"], teams["S1"]["id"], teams["S2"]["id"],
            self._slot(20)["id"], actor_id=ADMIN, league_id=self.elite["id"])
        self._final(g1["id"], 2, 0)
        self._final(g2["id"], 5, 4)

        st = self.api.get_league_season_standings(
            self.elite["id"], self.season["id"])
        ids = {r["team_id"] for r in st["standings"]}
        # All four teams — from BOTH divisions — appear in one table.
        for name in ("N1", "N2", "S1", "S2"):
            self.assertIn(teams[name]["id"], ids)
        by_team = {r["team_id"]: r for r in st["standings"]}
        self.assertEqual(by_team[teams["N1"]["id"]]["pts"], 2)
        self.assertEqual(by_team[teams["S1"]["id"]]["pts"], 2)
        self.assertEqual(by_team[teams["N2"]["id"]]["pts"], 0)

    # -- E2 lifecycle integrity -----------------------------------------
    def _active_regs(self, team_id):
        return [r for r in self.api.store.registrations_for_season(self.season["id"])
                if r.team_id == team_id and r.active]

    def test_transfer_moves_game_free_active_registration(self):
        t = self._team("Movers", self.elite["id"])
        moved = self.api.transfer_team_to_league(
            t["id"], self.rec["id"], actor_id=ADMIN)
        self.assertNotIn("error", moved, moved)
        # The permanent League changed, and the active registration was moved to
        # Rec's LeagueSeason for this Season (its old Division cleared).
        self.assertEqual(self.api.store.get_team(t["id"]).league_id, self.rec["id"])
        ls_rec = self.api.store.league_season_for(self.rec["id"], self.season["id"])
        regs = self._active_regs(t["id"])
        self.assertEqual(len(regs), 1)
        self.assertEqual(regs[0].league_season_id, ls_rec.id)
        self.assertIsNone(regs[0].division_id)

    def test_transfer_blocked_by_committed_game_zero_mutation(self):
        a = self._team("A", self.elite["id"])
        b = self._team("B", self.elite["id"])
        g = self.api.create_game(
            self.season["id"], None, a["id"], b["id"], self._slot(18)["id"],
            actor_id=ADMIN, league_id=self.elite["id"])
        self.assertNotIn("error", g, g)
        before = self._audit_count()
        res = self.api.transfer_team_to_league(
            a["id"], self.rec["id"], actor_id=ADMIN)
        self.assertEqual(res["error"]["details"]["reason"],
                         "team_transfer_strands_games", res)
        # Zero mutation: still in Elite, registration untouched, no audit.
        self.assertEqual(self.api.store.get_team(a["id"]).league_id,
                         self.elite["id"])
        self.assertEqual(self._active_regs(a["id"])[0].league_season_id,
                         self.api.store.league_season_for(
                             self.elite["id"], self.season["id"]).id)
        self.assertEqual(self._audit_count(), before)

    def test_transfer_preserves_inactive_history(self):
        t = self._team("Hist", self.elite["id"])
        # Deactivate the current registration (simulate a past, inactive row).
        reg = self._active_regs(t["id"])[0]
        reg.active = False
        self.api.store.save_season_team_registration(reg)
        moved = self.api.transfer_team_to_league(
            t["id"], self.rec["id"], actor_id=ADMIN)
        self.assertNotIn("error", moved, moved)
        # The inactive historical row is left exactly where it was (in Elite).
        stored = self.api.store.get_season_team_registration(reg.id)
        self.assertFalse(stored.active)
        self.assertEqual(
            stored.league_season_id,
            self.api.store.league_season_for(self.elite["id"], self.season["id"]).id)

    def test_exhibition_publish_requires_active_season_participation(self):
        a = self._team("A", self.elite["id"])
        b = self._team("B", self.rec["id"])
        exh = self.api.create_game(
            self.season["id"], None, a["id"], b["id"], self._slot(18)["id"],
            actor_id=ADMIN, game_type=GameType.EXHIBITION.value)
        self.assertNotIn("error", exh, exh)
        # a leaves the season → the friendly can no longer be published.
        reg = self._active_regs(a["id"])[0]
        reg.active = False
        self.api.store.save_season_team_registration(reg)
        before = self._audit_count()
        res = self.api.publish_game(exh["id"], actor_id=ADMIN)
        self.assertEqual(res["error"]["details"]["reason"],
                         "team_not_season_participant", res)
        self.assertFalse(self.api.store.get_game(exh["id"]).published)
        self.assertEqual(self._audit_count(), before)

    def test_league_season_standings_unknown_pair_is_not_found(self):
        # A League that isn't in this Season → not_found, never a crash.
        other = self.api.create_season(
            self.program["id"], "Spring", actor_id=ADMIN)
        res = self.api.get_league_season_standings(
            self.elite["id"], other["id"])
        self.assertEqual(res["error"]["code"], "not_found", res)


class MemoryGameTypeTest(_Contract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


class DurableGameTypeTest(_Contract, unittest.TestCase):
    def make_store(self):
        url = os.environ.get("TEST_DATABASE_URL") or ":memory:"
        store = SqlStore(url)
        store.reset_schema()
        return store


if __name__ == "__main__":
    unittest.main()
