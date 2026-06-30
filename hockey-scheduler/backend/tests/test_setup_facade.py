import json
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService


class SetupFacadeTest(unittest.TestCase):
    def setUp(self):
        self.api = ApiService()

    def _build(self):
        league = self.api.create_league("EU Premier", country="DE")
        season = self.api.create_season(league["id"], "2026/27")
        division = self.api.create_division(season["id"], "U16", age_group="U16")
        club_a = self.api.create_club("Lions Club")
        club_b = self.api.create_club("Falcons Club")
        home = self.api.create_team(club_a["id"], division["id"], "U16 Lions")
        away = self.api.create_team(club_b["id"], division["id"], "U16 Falcons")
        venue = self.api.create_venue("Ice Palace")
        rink = self.api.create_rink(venue["id"], "Rink 2")
        slot = self.api.create_ice_slot(
            rink["id"], "2026-09-01T18:30:00+00:00", "2026-09-01T20:00:00+00:00"
        )
        return season, division, home, away, slot

    def test_full_setup_through_facade_is_json_safe(self):
        season, division, home, away, slot = self._build()
        game = self.api.create_game(
            season["id"], division["id"], home["id"], away["id"], slot["id"]
        )
        self.assertNotIn("error", game)
        self.assertEqual(game["ice_slot_id"], slot["id"])
        json.dumps(game)  # must round-trip with no custom encoder

    def test_duplicate_slot_returns_schedule_conflict(self):
        season, division, home, away, slot = self._build()
        self.api.create_game(season["id"], division["id"], home["id"], away["id"], slot["id"])
        result = self.api.create_game(
            season["id"], division["id"], home["id"], away["id"], slot["id"]
        )
        self.assertEqual(result["error"]["code"], "schedule_conflict")

    def test_missing_league_returns_not_found(self):
        result = self.api.create_season("league_missing", "S")
        self.assertEqual(result["error"]["code"], "not_found")

    def test_naive_ice_slot_time_returns_validation_error(self):
        venue = self.api.create_venue("V")
        rink = self.api.create_rink(venue["id"], "R")
        result = self.api.create_ice_slot(
            rink["id"], "2026-09-01T18:30:00", "2026-09-01T20:00:00"
        )
        self.assertEqual(result["error"]["code"], "validation_error")

    def test_created_game_drives_roster_endpoints(self):
        season, division, home, away, slot = self._build()
        game = self.api.create_game(
            season["id"], division["id"], home["id"], away["id"], slot["id"]
        )
        g1 = self.api.create_player(home["id"], "Goalie One", "goalie")
        self.api.create_player(home["id"], "Skater One", "forward")
        sel = self.api.select_roster(game["id"], [g1["id"]], actor_id="coach_1")
        self.assertNotIn("error", sel)
        status = self.api.get_roster_status(game["id"])
        self.assertEqual(status["game_id"], game["id"])


if __name__ == "__main__":
    unittest.main()
