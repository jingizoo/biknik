import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.full_demo import build_full_demo_store


class LineupsTest(unittest.TestCase):
    """Home and away lineups are managed independently (#25)."""

    def setUp(self):
        self.store, self.game_id, self.ids = build_full_demo_store()
        self.api = ApiService(self.store)
        self.home = self.ids["home_team_id"]
        self.away = self.ids["away_team_id"]

    def test_get_lineups_returns_both_sides(self):
        L = self.api.get_lineups(self.game_id)
        self.assertEqual(L["home"]["team_id"], self.home)
        self.assertEqual(L["away"]["team_id"], self.away)
        self.assertEqual(L["home"]["team_name"], "U16 Lions")
        self.assertEqual(L["away"]["team_name"], "U16 Falcons")
        # Seeded game: home is confirmed, away has no roster yet.
        self.assertEqual(L["home"]["status"]["status"], "roster_confirmed")
        self.assertEqual(L["away"]["status"]["status"], "draft")

    def test_away_players_grouped_available(self):
        L = self.api.get_lineups(self.game_id)
        away = L["away"]["players"]
        self.assertTrue(away)
        self.assertTrue(all(p["group"] == "available" for p in away))

    def test_building_away_does_not_touch_home(self):
        home_before = self.api.get_roster_status(self.game_id)  # defaults to home
        away_avail = [p for p in self.api.get_lineups(self.game_id)["away"]["players"]]
        g = next(p for p in away_avail if p["slot_type"] == "goalie")
        self.api.select_roster(self.game_id, [g["id"]], actor_id="coach_away")
        L = self.api.get_lineups(self.game_id)
        # Away goalie slot closed; home side unchanged.
        self.assertEqual(L["away"]["status"]["open_goalie_slots"], 0)
        self.assertEqual(L["home"]["status"], home_before)

    def test_status_defaults_to_home(self):
        # Backward compat: compute_roster_status / get_board still mean home.
        st = self.api.get_roster_status(self.game_id)
        self.assertEqual(st["team_id"], self.home)
        self.assertEqual(self.api.get_board(self.game_id)["status"]["team_id"],
                         self.home)

    def test_copy_previous_for_away_side(self):
        # The future draft game can copy the away team's previous roster too,
        # once the away team has a prior rostered game. Build away on the seeded
        # game first, then copy onto the next game for the away side.
        nxt = self.ids["next_game_id"]
        away_avail = [p for p in self.api.get_lineups(self.game_id)["away"]["players"]]
        picks = [away_avail[0]["id"], away_avail[1]["id"]]
        self.api.select_roster(self.game_id, picks, actor_id="coach_away")
        res = self.api.copy_previous_roster(nxt, team_id=self.away, actor_id="coach")
        self.assertNotIn("error", res)
        self.assertEqual(res["team_id"], self.away)
        self.assertEqual(res["from_game_id"], self.game_id)
        # Those away players are now on the next game's away lineup.
        away_next = [p for p in self.api.get_lineups(nxt)["away"]["players"]
                     if p["group"] == "selected"]
        self.assertEqual({p["id"] for p in away_next}, set(picks))


    def test_auto_build_away_does_not_touch_home(self):
        home_before = self.api.get_roster_status(self.game_id)
        res = self.api.auto_build_roster(
            self.game_id, team_id=self.away, actor_id="coach_away")
        self.assertNotIn("error", res)
        self.assertEqual(res["team_id"], self.away)
        lineups = self.api.get_lineups(self.game_id)
        self.assertEqual(lineups["home"]["status"], home_before)
        self.assertNotEqual(lineups["away"]["status"]["status"], "draft")

    def test_auto_build_third_team_rejected(self):
        club = self.api.create_club("Outsiders HC")
        team = self.api.create_team(
            club["id"], self.ids["division_id"], "Outsiders")
        res = self.api.auto_build_roster(
            self.game_id, team_id=team["id"], actor_id="coach")
        self.assertEqual(res["error"]["code"], "validation_error")


if __name__ == "__main__":
    unittest.main()
