"""#283 Slice E: promotion/relegation and rollover preserve history exactly.

Moving a Team to a new permanent League (a transfer) and rolling participation
into a new Season must NEVER disturb a prior Season's registrations, Games,
results, or standings. These snapshot a completed prior Season, perform a
transfer and a rollover, and assert the prior Season is byte-identical after —
on both the in-memory and SQLite stores.
"""

import os
import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.store import InMemoryStore, SqlStore

ADMIN = "admin"
UTC = timezone.utc


def _at(hour):
    return datetime(2026, 9, 1, hour, tzinfo=UTC)


class _Contract:
    def setUp(self):
        self.api = ApiService(self.make_store())
        self.org = self.api.create_organization("Org", "O", actor_id=ADMIN)
        self.program = self.api.create_program(
            "Prog", operator_organization_id=self.org["id"], actor_id=ADMIN)
        # A completed prior Season with one League + Division, two teams, a
        # FINAL-result game, and the resulting standings.
        self.s1 = self.api.create_season(self.program["id"], "S1", actor_id=ADMIN)
        self.la = self.api.create_league(self.s1["id"], "LA", actor_id=ADMIN)
        self.lb = self.api.create_league(self.s1["id"], "LB", actor_id=ADMIN)
        self.div = self.api.create_division_v2(self.la["id"], "D", actor_id=ADMIN)
        club = self.api.create_club("C", actor_id=ADMIN)
        self.a = self.api.create_team(club["id"], None, "A", actor_id=ADMIN,
                                      league_id=self.la["id"])
        self.b = self.api.create_team(club["id"], None, "B", actor_id=ADMIN,
                                      league_id=self.la["id"])
        for t in (self.a, self.b):
            self.api.register_team_for_season(
                self.s1["id"], t["id"], self.div["id"], actor_id=ADMIN,
                league_id=self.la["id"])
        venue = self.api.create_venue("V", organization_id=self.org["id"],
                                      league_id=self.program["id"], actor_id=ADMIN)
        self.api.grant_season_venue_access(self.s1["id"], venue["id"],
                                           actor_id=ADMIN)
        rink = self.api.create_rink(venue["id"], "R", actor_id=ADMIN)
        slot = self.api.create_ice_slot(
            rink["id"], _at(18).isoformat(), _at(20).isoformat(), "game",
            actor_id=ADMIN)
        self.game = self.api.create_game(
            self.s1["id"], self.div["id"], self.a["id"], self.b["id"],
            slot["id"], actor_id=ADMIN, league_id=self.la["id"])
        self.api.record_result(self.game["id"], 4, 1, actor_id=ADMIN)
        self.api.approve_result(self.game["id"], actor_id=ADMIN)

    def _snapshot(self):
        game = self.api.store.get_game(self.game["id"])
        result = self.api.store.result_for_game(self.game["id"])
        regs = sorted(
            ((r.id, r.team_id, r.league_season_id, r.division_id, r.active)
             for r in self.api.store.registrations_for_season(self.s1["id"])),
            key=lambda x: x[0])
        standings = self.api.get_standings(self.div["id"])["standings"]
        return {
            "game": (game.home_team_id, game.away_team_id, game.season_id,
                     game.division_id, game.league_id, game.league_season_id,
                     game.game_type, game.ice_slot_id, game.published),
            "result": (result.home_score, result.away_score, result.status),
            "regs": regs,
            "standings": [(r["team_id"], r["w"], r["l"], r["t"], r["gf"],
                           r["ga"], r["pts"]) for r in standings],
        }

    def test_transfer_preserves_prior_season(self):
        # The prior season is complete: its registrations become history.
        for r in self.api.store.registrations_for_season(self.s1["id"]):
            r.active = False
            self.api.store.save_season_team_registration(r)
        before = self._snapshot()
        moved = self.api.transfer_team_to_league(
            self.a["id"], self.lb["id"], actor_id=ADMIN)
        self.assertNotIn("error", moved, moved)
        # The team's permanent league changed, but S1's history is byte-identical.
        self.assertEqual(self.api.store.get_team(self.a["id"]).league_id,
                         self.lb["id"])
        self.assertEqual(self._snapshot(), before)

    def test_rollover_preserves_prior_season(self):
        before = self._snapshot()
        s2 = self.api.create_season(self.program["id"], "S2", actor_id=ADMIN)
        res = self.api.roll_forward_registrations_v2(
            self.s1["id"], s2["id"],
            selections=[{"team_id": self.a["id"], "league_id": self.la["id"]},
                        {"team_id": self.b["id"], "league_id": self.la["id"]}],
            actor_id=ADMIN)
        self.assertEqual(res["rolled_forward"], 2, res)
        # New S2 registrations exist, but S1's history is byte-identical.
        self.assertEqual(self._snapshot(), before)


class MemoryHistoryTest(_Contract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


class DurableHistoryTest(_Contract, unittest.TestCase):
    def make_store(self):
        url = os.environ.get("TEST_DATABASE_URL") or ":memory:"
        store = SqlStore(url)
        store.reset_schema()
        return store


if __name__ == "__main__":
    unittest.main()
