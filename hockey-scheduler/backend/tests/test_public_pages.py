"""Public schedule / standings / result surface (#83).

A clean public web surface built from public-safe DTOs only — team names,
scores, rink, date/time, division — reachable unauthenticated (including in
production). No player names, rosters, availability, or officials leak, and the
private per-game endpoints stay protected.
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Game, GameResult, ResultStatus
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.web import server as srv

UTC = timezone.utc


class _Base(unittest.TestCase):
    def _get(self, path):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _player_names(self):
        return [p.name for p in srv.STATE.api.store.all_players()] \
            if hasattr(srv.STATE.api.store, "all_players") else \
            [p.name for p in srv.STATE.api.store.players.values()]


class PublicPagesProductionTest(_Base):
    """Anonymous production requests can read the public surface, nothing more."""

    @classmethod
    def setUpClass(cls):
        os.environ["APP_MODE"] = "production"
        os.environ["BOOTSTRAP_ADMIN_USER"] = "pubadmin"
        os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "pubadmin-pw"
        srv.STATE.reset()
        build_full_demo_store(srv.STATE.api.store)  # give the store data
        cls.game_id = next(iter(srv.STATE.api.store.games)) \
            if hasattr(srv.STATE.api.store, "games") else \
            srv.STATE.api.store.all_games()[0].id
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()
        os.environ.pop("APP_MODE", None)
        os.environ.pop("BOOTSTRAP_ADMIN_USER", None)
        os.environ.pop("BOOTSTRAP_ADMIN_PASSWORD", None)
        srv.STATE.reset()

    def test_public_schedule_is_readable_and_leaks_no_names(self):
        status, body = self._get("/api/public/schedule")
        self.assertEqual(status, 200)
        self.assertIn("fixtures", body)
        blob = json.dumps(body)
        for name in self._player_names():
            self.assertNotIn(name, blob)

    def test_public_standings_readable_and_clean(self):
        _, sched = self._get("/api/public/schedule")
        div = sched["divisions"][0]["id"]
        status, body = self._get(f"/api/public/standings/{div}")
        self.assertEqual(status, 200)
        self.assertIn("standings", body)
        blob = json.dumps(body)
        for name in self._player_names():
            self.assertNotIn(name, blob)

    def test_public_game_detail_is_fixture_only(self):
        status, body = self._get(f"/api/public/games/{self.game_id}")
        self.assertEqual(status, 200)
        # Only public-safe keys; nothing roster-ish.
        self.assertEqual(
            set(body),
            {"game_id", "division_id", "division_name", "home_team_name",
             "away_team_name", "rink_name", "venue_name", "start_time",
             "status", "home_score", "away_score"})

    def test_unknown_public_game_is_404(self):
        status, body = self._get("/api/public/games/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_private_game_endpoints_still_protected(self):
        # The public surface must not open the private per-game routes (#73).
        for sub in ("roster", "lineups", "board", "officials"):
            status, _ = self._get(f"/api/games/{self.game_id}/{sub}")
            self.assertEqual(status, 401, sub)


class PublicStandingsPublishLeakTest(unittest.TestCase):
    """An unpublished game's final result must not leak into public standings
    by aggregation, even though the public schedule/detail already hide it."""

    def setUp(self):
        self.store, _gid, _ids = build_full_demo_store()
        self.api = ApiService(self.store)
        # #180: teams participate in a division via registration, not the legacy
        # Team.division_id — find a division with two registered teams.
        by_div = defaultdict(list)
        teams_by_id = {t.id: t for t in self.store.all_teams()}
        for r in self.store.all_season_team_registrations():
            if r.active and r.division_id and r.team_id in teams_by_id:
                by_div[r.division_id].append(teams_by_id[r.team_id])
        self.div_id, teams = next(
            (d, ts) for d, ts in by_div.items() if len(ts) >= 2)
        self.home, self.away = teams[0], teams[1]

    def _row(self, standings, team_id):
        return next(r for r in standings["standings"] if r["team_id"] == team_id)

    def test_unpublished_final_result_excluded_from_public_standings(self):
        before_pub = self.api.get_public_standings(self.div_id)
        before_int = self.api.get_standings(self.div_id)
        p_home0 = self._row(before_pub, self.home.id)
        i_home0 = self._row(before_int, self.home.id)

        # An unpublished (draft) game with a decisive FINAL result. #283: a
        # regular game carries its Division's exact LeagueSeason (league_id,
        # season_id, league_season_id) — standings now fail closed on a Game
        # whose LeagueSeason disagrees with its Division, so seed a consistent
        # one; this test is about the published/unpublished distinction.
        _division = self.store.get_division(self.div_id)
        _ls = self.store.get_league_season(_division.league_season_id)
        g = Game(id=self.store.next_id("game"), home_team_id=self.home.id,
                 away_team_id=self.away.id,
                 start_time=datetime(2026, 3, 1, tzinfo=UTC),
                 division_id=self.div_id, published=False,
                 league_id=_ls.league_id, season_id=_ls.season_id,
                 league_season_id=_ls.id)
        self.store.add_game(g)
        self.store.add_game_result(GameResult(
            id=self.store.next_id("result"), game_id=g.id,
            home_score=10, away_score=0, status=ResultStatus.FINAL))

        after_pub = self.api.get_public_standings(self.div_id)
        after_int = self.api.get_standings(self.div_id)
        p_home1 = self._row(after_pub, self.home.id)
        i_home1 = self._row(after_int, self.home.id)

        # Public standings are unchanged — the hidden game is not counted.
        self.assertEqual(p_home1["gp"], p_home0["gp"])
        self.assertEqual(p_home1["pts"], p_home0["pts"])
        self.assertEqual(p_home1["gf"], p_home0["gf"])
        self.assertEqual(p_home1["ga"], p_home0["ga"])
        # Internal (operator) standings DO count it — behavior unchanged there.
        self.assertEqual(i_home1["gp"], i_home0["gp"] + 1)
        self.assertEqual(i_home1["pts"], i_home0["pts"] + 2)
        self.assertEqual(i_home1["gf"], i_home0["gf"] + 10)

        # The hidden game is also absent from the public schedule...
        sched = self.api.get_public_schedule()
        self.assertNotIn(g.id, [f["game_id"] for f in sched["fixtures"]])
        # ...and its public detail is a 404 (not_found).
        detail = self.api.get_public_game(g.id)
        self.assertEqual(detail["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
