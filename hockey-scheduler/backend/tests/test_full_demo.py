import json
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.full_demo import build_full_demo_store


class FullDemoTest(unittest.TestCase):
    def setUp(self):
        self.store, self.game_id, self.ids = build_full_demo_store()
        self.api = ApiService(self.store)

    def test_seed_builds_full_universe(self):
        ov = self.api.get_demo_overview()
        self.assertEqual(ov["league"]["name"], "Alpine Ice Hockey League")
        self.assertEqual({d["name"] for d in ov["divisions"]},
                         {"U16 Elite", "U18 Development", "Senior A"})
        # The pilot data pack (#97) grows this from the original 4-team demo
        # to 12 teams across the 3 divisions.
        self.assertEqual({t["name"] for t in ov["teams"]}, {
            "U16 Lions", "U16 Falcons", "U16 Wolves", "U16 Comets",
            "U16 Panthers", "U16 Sharks",
            "U18 Lions", "U18 Falcons", "U18 Wolves", "U18 Bears",
            "Senior Lions", "Senior Falcons",
        })
        # 3 rinks across 2 venues (#97 adds Lakeside Rink at a second venue).
        self.assertEqual({r["name"] for r in ov["rinks"]},
                         {"Main Rink", "Training Rink", "Lakeside Rink"})
        # The original seeded published game and its future draft game are
        # still exactly as before, plus ~18 more games (#97) drawn from two
        # weeks of ice inventory — one of which is left as another draft (the
        # core scenario's own "next game"), the rest published.
        allocated = [s for s in ov["ice_slots"] if s["status"] == "allocated"]
        self.assertEqual(len(allocated), 20)
        self.assertIn("U16 Lions vs U16 Falcons",
                      {s["game_label"] for s in allocated})
        published = [g for g in ov["schedule"] if g["published"]]
        self.assertEqual(len(published), 19)

    def test_opens_on_confirmed_roster(self):
        status = self.api.get_roster_status(self.game_id)
        self.assertEqual(status["status"], "roster_confirmed")

    def test_end_to_end_backout_substitute_lock(self):
        gid, sub = self.game_id, self.ids["substitute_player_id"]
        selected = self.ids["selected_player_id"]

        # 1. A selected skater backs out → Open Slot (no subs yet).
        self.api.set_availability(gid, selected, "unavailable")
        self.assertEqual(self.api.get_roster_status(gid)["status"], "open_slot")

        # 2. A non-selected player enrolls → Needs Substitute Decision.
        self.api.enroll_substitute(gid, sub)
        self.assertEqual(self.api.get_roster_status(gid)["status"], "needs_substitute")

        # 3. Coach adds the substitute → Roster Confirmed again.
        self.api.add_substitute_to_roster(gid, sub, actor_id="coach_lions")
        self.assertEqual(self.api.get_roster_status(gid)["status"], "roster_confirmed")

        # 4. Coach locks the roster.
        self.api.lock_roster(gid, actor_id="coach_lions")
        self.assertEqual(self.api.get_roster_status(gid)["status"], "locked")

    def test_public_overview_has_no_player_pii(self):
        ov = self.api.get_demo_overview()
        # The public fixtures payload must not contain any player names.
        blob = json.dumps(ov["public_fixtures"])
        for player in self.store.players.values():
            self.assertNotIn(player.name, blob)
        # And fixtures expose only safe fixture fields.
        for f in ov["public_fixtures"]:
            self.assertEqual(set(f.keys()), {
                "division_name", "home_team_name", "away_team_name",
                "venue_name", "rink_name", "start_time", "status", "is_junior",
            })

    def test_overview_is_json_serializable(self):
        json.dumps(self.api.get_demo_overview())

    def test_newly_scheduled_game_can_build_roster(self):
        # Schedule a second U16 game (Falcons home) on an available slot, then
        # build a roster for it — both U16 teams are seeded with players.
        ov = self.api.get_demo_overview()
        slot = next(s for s in ov["ice_slots"] if s["status"] == "available")
        u16 = [t for t in ov["teams"] if t["division_name"] == "U16 Elite"]
        falcons = next(t for t in u16 if t["name"] == "U16 Falcons")
        lions = next(t for t in u16 if t["name"] == "U16 Lions")
        game = self.api.create_game(ov["seasons"][0]["id"], falcons["division_id"],
                                    falcons["id"], lions["id"], slot["id"])
        self.assertNotIn("error", game)
        status = self.api.auto_build_roster(game["id"])
        self.assertEqual(status["status"], "roster_confirmed")

    def test_auto_build_short_roster_is_not_confirmed(self):
        # A team with fewer players than target builds a draft (open) roster,
        # never silently "confirmed".
        api = ApiService()
        lg = api.create_league("L")
        se = api.create_season(lg["id"], "S")
        dv = api.create_division(se["id"], "D")
        ta = api.create_team(api.create_club("CA")["id"], dv["id"], "TA")
        tb = api.create_team(api.create_club("CB")["id"], dv["id"], "TB")
        # Only 2 skaters, no goalie — well below the 1 goalie / 15 skater target.
        for n in ("Skater A", "Skater B"):
            api.create_player(ta["id"], n, "forward")
        rink = api.create_rink(api.create_venue("V", league_id=lg["id"])["id"], "R")
        slot = api.create_ice_slot(rink["id"], "2026-09-01T18:30:00+00:00",
                                   "2026-09-01T20:00:00+00:00")
        game = api.create_game(se["id"], dv["id"], ta["id"], tb["id"], slot["id"])
        status = api.auto_build_roster(game["id"])
        self.assertNotEqual(status["status"], "roster_confirmed")
        self.assertGreater(status["open_skater_slots"], 0)
        self.assertEqual(status["open_goalie_slots"], 1)
        self.assertTrue(status["short_roster"])
        self.assertEqual(status["missing_goalies"], 1)

    def test_scheduled_game_is_draft_until_published(self):
        ov = self.api.get_demo_overview()
        slot = next(s for s in ov["ice_slots"] if s["status"] == "available")
        u16 = [t for t in ov["teams"] if t["division_name"] == "U16 Elite"]
        before = len(ov["public_fixtures"])  # only the seeded (published) game
        game = self.api.create_game(ov["seasons"][0]["id"], u16[0]["division_id"],
                                    u16[0]["id"], u16[1]["id"], slot["id"])
        # A freshly scheduled game is a draft and is NOT public yet.
        ov2 = self.api.get_demo_overview()
        sched = next(g for g in ov2["schedule"] if g["game_id"] == game["id"])
        self.assertFalse(sched["published"])
        self.assertEqual(len(ov2["public_fixtures"]), before)
        # Publishing it makes it appear in the public fixtures.
        self.api.publish_game(game["id"], actor_id="league_admin")
        ov3 = self.api.get_demo_overview()
        self.assertEqual(len(ov3["public_fixtures"]), before + 1)

    def test_build_roster_without_players_errors(self):
        # A team with no players cannot be auto-rostered.
        api = ApiService()
        lg = api.create_league("L")
        se = api.create_season(lg["id"], "S")
        dv = api.create_division(se["id"], "D")
        ta = api.create_team(api.create_club("CA")["id"], dv["id"], "TA")
        tb = api.create_team(api.create_club("CB")["id"], dv["id"], "TB")
        rink = api.create_rink(api.create_venue("V", league_id=lg["id"])["id"], "R")
        slot = api.create_ice_slot(rink["id"], "2026-09-01T18:30:00+00:00",
                                   "2026-09-01T20:00:00+00:00")
        game = api.create_game(se["id"], dv["id"], ta["id"], tb["id"], slot["id"])
        result = api.auto_build_roster(game["id"])
        self.assertEqual(result["error"]["code"], "validation_error")


if __name__ == "__main__":
    unittest.main()
