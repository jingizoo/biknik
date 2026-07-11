import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain.errors import NotFoundError, ValidationError
from hockey_scheduler.api import ApiService
from hockey_scheduler.services import SetupService
from hockey_scheduler.store import InMemoryStore

UTC = timezone.utc


def dt(h, m=0):
    return datetime(2026, 9, 1, h, m, tzinfo=UTC)


class ResultsTest(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore()
        self.svc = SetupService(self.store)
        self.api = ApiService(self.store)
        league = self.svc.create_league("L")
        self.season = self.svc.create_season(league.id, "S")
        self.div = self.svc.create_division(self.season.id, "U16")
        self.lions = self.svc.create_team(self.svc.create_club("Lions").id, self.div.id, "Lions")
        self.falcons = self.svc.create_team(self.svc.create_club("Falcons").id, self.div.id, "Falcons")
        self.bears = self.svc.create_team(self.svc.create_club("Bears").id, self.div.id, "Bears")
        # Participation (scheduling + standings roster) comes from the season
        # registration (#180), so register each team into the season+division.
        for _t in (self.lions, self.falcons, self.bears):
            self.svc.register_team_for_season(self.season.id, _t.id, self.div.id)
        venue = self.svc.create_venue("Arena", league_id=league.id)
        self.rink = self.svc.create_rink(venue.id, "Rink 1")

    def _game(self, home, away, h=18):
        slot = self.svc.create_ice_slot(self.rink.id, dt(h), dt(h + 2))
        return self.svc.create_game(self.season.id, self.div.id, home.id, away.id, slot.id)

    # -- score entry / approval -------------------------------------------
    def test_record_creates_draft(self):
        g = self._game(self.lions, self.falcons)
        r = self.svc.record_result(g.id, 3, 1)
        self.assertEqual(r.status.value, "draft")
        self.assertEqual((r.home_score, r.away_score), (3, 1))

    def test_rerecord_updates_draft(self):
        g = self._game(self.lions, self.falcons)
        self.svc.record_result(g.id, 3, 1)
        r = self.svc.record_result(g.id, 5, 5)
        self.assertEqual((r.home_score, r.away_score), (5, 5))
        self.assertEqual(len(self.store.all_game_results()), 1)

    def test_approve_finalizes(self):
        g = self._game(self.lions, self.falcons)
        self.svc.record_result(g.id, 3, 1)
        r = self.svc.approve_result(g.id)
        self.assertEqual(r.status.value, "final")

    def test_cannot_edit_final(self):
        g = self._game(self.lions, self.falcons)
        self.svc.record_result(g.id, 3, 1)
        self.svc.approve_result(g.id)
        with self.assertRaises(ValidationError):
            self.svc.record_result(g.id, 4, 4)

    def test_approve_without_result_errors(self):
        g = self._game(self.lions, self.falcons)
        with self.assertRaises(NotFoundError):
            self.svc.approve_result(g.id)

    def test_negative_score_rejected(self):
        g = self._game(self.lions, self.falcons)
        with self.assertRaises(ValidationError):
            self.svc.record_result(g.id, -1, 2)

    def test_cancelled_game_result_rejected(self):
        g = self._game(self.lions, self.falcons)
        g.cancelled = True
        self.store.save_game(g)
        with self.assertRaises(ValidationError):
            self.svc.record_result(g.id, 1, 0)

    def test_approve_cancelled_game_result_rejected(self):
        # A draft recorded before cancellation cannot be finalized afterwards.
        g = self._game(self.lions, self.falcons)
        self.svc.record_result(g.id, 2, 1)
        g.cancelled = True
        self.store.save_game(g)
        with self.assertRaises(ValidationError):
            self.svc.approve_result(g.id)

    # -- standings ---------------------------------------------------------
    def _standings(self):
        return {r["team_name"]: r
                for r in self.api.get_standings(self.div.id)["standings"]}

    def test_draft_result_does_not_affect_standings(self):
        g = self._game(self.lions, self.falcons)
        self.svc.record_result(g.id, 4, 2)  # draft only
        s = self._standings()
        self.assertEqual(s["Lions"]["gp"], 0)
        self.assertEqual(s["Lions"]["pts"], 0)

    def test_final_result_affects_standings(self):
        g = self._game(self.lions, self.falcons)
        self.svc.record_result(g.id, 4, 2)
        self.svc.approve_result(g.id)
        s = self._standings()
        self.assertEqual(s["Lions"]["w"], 1)
        self.assertEqual(s["Lions"]["pts"], 2)
        self.assertEqual(s["Lions"]["gd"], 2)
        self.assertEqual(s["Falcons"]["l"], 1)
        self.assertEqual(s["Falcons"]["pts"], 0)

    def test_tie_awards_one_point_each(self):
        g = self._game(self.lions, self.falcons)
        self.svc.record_result(g.id, 3, 3)
        self.svc.approve_result(g.id)
        s = self._standings()
        self.assertEqual(s["Lions"]["t"], 1)
        self.assertEqual(s["Lions"]["pts"], 1)
        self.assertEqual(s["Falcons"]["pts"], 1)

    def test_standings_ranked_by_points_then_gd(self):
        # Lions beat Falcons 5-0; Bears beat Falcons 1-0. Lions and Bears both
        # 2 pts, but Lions have the better goal difference → ranked first.
        g1 = self._game(self.lions, self.falcons, h=8)
        self.svc.record_result(g1.id, 5, 0); self.svc.approve_result(g1.id)
        g2 = self._game(self.bears, self.falcons, h=12)
        self.svc.record_result(g2.id, 1, 0); self.svc.approve_result(g2.id)
        order = [r["team_name"] for r in self.api.get_standings(self.div.id)["standings"]]
        self.assertEqual(order[0], "Lions")   # +5 GD beats Bears +1
        self.assertEqual(order[1], "Bears")
        self.assertEqual(order[2], "Falcons")

    def test_get_result_via_facade(self):
        g = self._game(self.lions, self.falcons)
        self.api.record_result(g.id, 2, 1)
        res = self.api.get_result(g.id)
        self.assertEqual(res["status"], "draft")
        appr = self.api.approve_result(g.id)
        self.assertEqual(appr["status"], "final")


if __name__ == "__main__":
    unittest.main()
