"""Real HTTP coverage for #331 review round 22/23: move_game/publish_game
must reject a Game whose own league_season_id doesn't resolve to a real,
correctly-scoped LeagueSeason (round 22), or whose legacy league_id
disagrees with that LeagueSeason's own league_id (round 23) -- over the
actual /api/games/{id}/move and /api/games/{id}/publish routes the UI
itself posts to, not just the ApiService facade every other regression for
this fix uses (test_exact_registration_identity.py) or the real SQL
backends (test_v2_reassignment_integrity_sql.py). The HTTP handler is a
thin, uniform pass-through (_send_api(api.move_game(...))/_send_api(api.
publish_game(...))) with no game-specific logic of its own; this proves
that pass-through correctly relays the new rejection as a structured JSON
error, not a 500 or a silent 200. Both actions write an audited Game
mutation, so (unlike simpler role-only checks) they require a genuine
authenticated session -- the X-Demo-Role header alone is not enough here.
The demo's own seeded Game (STATE.game_id) is divisioned, so this also
covers the divisioned boundary both rounds' fixes must protect (proven
directly, division-less, at the facade/SQL layers).
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.web.server import STATE, Handler


class GameLeagueSeasonIdentityHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def setUp(self):
        # #331 review round 23: STATE.reset() swaps STATE.api/game_id in
        # place, and Handler always reads STATE fresh per-request, so a
        # per-TEST reset (not just once per class) is what actually gives
        # each test its own clean demo Game -- otherwise one test's
        # corruption of the shared demo Game leaks into whichever test runs
        # next (alphabetical order, not declaration order), masking its
        # intended rejection reason with a stale one.
        STATE.reset()  # clean-slate + full demo seed, including a real Game.

    def _req(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _login(self, username):
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        status, body = self._req(opener, "POST", "/api/auth/login",
                                 {"username": username, "password": "demo"})
        self.assertEqual(status, 200, body)
        return opener

    def _corrupt_demo_game_league_season(self):
        game = STATE.api.store.get_game(STATE.game_id)
        game.league_season_id = "leagueseason_does_not_exist"
        STATE.api.store.save_game(game)

    def test_move_over_real_http_rejects_a_dangling_league_season_id(self):
        admin = self._login("admin")
        self._corrupt_demo_game_league_season()
        # Any real, available slot works as the destination -- the rejection
        # must fire before the destination is even considered.
        slots = [s for s in STATE.api.store.all_ice_slots()
                 if s.status.value == "available"]
        self.assertTrue(slots, "demo seed must include an available slot")
        status, body = self._req(admin, "POST",
                                 f"/api/games/{STATE.game_id}/move",
                                 {"ice_slot_id": slots[0].id})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["details"]["reason"],
                         "regular_game_missing_league_season", body)

    def test_publish_over_real_http_rejects_a_dangling_league_season_id(self):
        admin = self._login("admin")
        self._corrupt_demo_game_league_season()
        status, body = self._req(admin, "POST",
                                 f"/api/games/{STATE.game_id}/publish")
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["details"]["reason"],
                         "regular_game_missing_league_season", body)

    def _corrupt_demo_game_league_id(self):
        # #331 review round 23: leave league_season_id VALID and untouched --
        # only the redundant legacy league_id drifts to a real, different
        # League already in the demo seed.
        game = STATE.api.store.get_game(STATE.game_id)
        other = next(l for l in STATE.api.store.all_leagues()
                     if l.id != game.league_id)
        game.league_id = other.id
        STATE.api.store.save_game(game)
        return other.id

    def test_move_over_real_http_rejects_a_disagreeing_league_id(self):
        admin = self._login("admin")
        corrupted_to = self._corrupt_demo_game_league_id()
        slots = [s for s in STATE.api.store.all_ice_slots()
                 if s.status.value == "available"]
        self.assertTrue(slots, "demo seed must include an available slot")
        status, body = self._req(admin, "POST",
                                 f"/api/games/{STATE.game_id}/move",
                                 {"ice_slot_id": slots[0].id})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["details"]["reason"],
                         "game_league_season_mismatch", body)
        self.assertEqual(body["error"]["details"]["league_id"], corrupted_to, body)

    def test_publish_over_real_http_rejects_a_disagreeing_league_id(self):
        admin = self._login("admin")
        self._corrupt_demo_game_league_id()
        status, body = self._req(admin, "POST",
                                 f"/api/games/{STATE.game_id}/publish")
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["details"]["reason"],
                         "game_league_season_mismatch", body)


if __name__ == "__main__":
    unittest.main()
