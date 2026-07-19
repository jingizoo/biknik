"""Regression coverage for #136: a client-supplied body actor_id must never
reach the audit trail on /api/setup/* or /api/games/{id}/{action} POST
routes. Each test forges a body actor_id and asserts the resulting audit
entry carries the real, server-resolved session user id instead — one
representative test per affected route family, mirroring
test_user_accounts.py::AccountCreationHttpTest from #135.
"""
import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.web import server as srv

FORGED_ACTOR = "spoofed_someone_else"


class ActorAttributionHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.admin_id = srv.STATE.api.store.get_user_account_by_username("admin").id

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _login(self, username, password="demo"):
        opener = self._client()
        self._req(opener, "POST", "/api/auth/login",
                  {"username": username, "password": password})
        return opener

    def _req(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _setup_audit_entry(self, action, entity_id):
        entries = srv.STATE.api.store.all_setup_audit()
        return next(e for e in reversed(entries)
                   if e.action == action and e.entity_id == entity_id)

    def _game_audit_entry(self, game_id, action):
        entries = srv.STATE.api.store.audit_for_game(game_id)
        return next(e for e in reversed(entries) if e.action == action)

    _next_slot_offset_days = 30

    def _fresh_game(self, admin):
        """A brand-new, unrostered ice slot + game between the demo teams,
        independent of any other test's mutations (locking/cancelling/
        selecting a roster on a shared game would make tests order-dependent).
        Each call gets its own non-overlapping slot on the rink — ice slots
        may not overlap, and same-second calls would otherwise collide.
        """
        type(self)._next_slot_offset_days += 1
        rink_id = srv.STATE.ids["main_rink_id"]
        start = (datetime.now(timezone.utc).replace(microsecond=0)
                 + timedelta(days=type(self)._next_slot_offset_days))
        end = start + timedelta(hours=2)
        status, slot = self._req(
            admin, "POST", "/api/setup/ice-slot",
            {"rink_id": rink_id, "start_time": start.isoformat(),
             "end_time": end.isoformat(), "slot_type": "game"})
        self.assertEqual(status, 200)
        status, game = self._req(
            admin, "POST", "/api/setup/game",
            {"season_id": srv.STATE.ids["season_id"],
             "division_id": srv.STATE.ids["division_id"],
             "home_team_id": srv.STATE.ids["home_team_id"],
             "away_team_id": srv.STATE.ids["away_team_id"],
             "ice_slot_id": slot["id"]})
        self.assertEqual(status, 200)
        return game["id"]

    # -- /api/setup/* -----------------------------------------------------

    def test_setup_league_actor_is_server_resolved(self):
        admin = self._login("admin")
        status, body = self._req(
            admin, "POST", "/api/setup/league",
            {"name": "Forged League", "actor_id": FORGED_ACTOR})
        self.assertEqual(status, 200)
        entry = self._setup_audit_entry("league_created", body["id"])
        self.assertEqual(entry.actor_id, self.admin_id)
        self.assertNotEqual(entry.actor_id, FORGED_ACTOR)

    def test_setup_player_actor_is_server_resolved(self):
        # Player create now carries a strict write schema (#271): a forged
        # top-level actor_id is an unknown field and is rejected outright (400)
        # rather than silently ignored — an even stronger guarantee that it can
        # never reach the audit trail, and no player is written at all.
        admin = self._login("admin")
        before = len(srv.STATE.api.store.all_players())
        status, body = self._req(
            admin, "POST", "/api/setup/player",
            {"team_id": srv.STATE.ids["home_team_id"], "name": "Forged Test Player",
             "position": "forward", "actor_id": FORGED_ACTOR})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"]["reason"], "unknown_field")
        self.assertIn("actor_id", body["error"]["details"]["fields"])
        self.assertEqual(len(srv.STATE.api.store.all_players()), before)

    # -- /api/games/{id}/{action} ------------------------------------------

    def test_roster_select_actor_is_server_resolved(self):
        admin = self._login("admin")
        gid = self._fresh_game(admin)
        status, _ = self._req(
            admin, "POST", f"/api/games/{gid}/roster/select",
            {"player_ids": [srv.STATE.ids["selected_player_id"]],
             "actor_id": FORGED_ACTOR})
        self.assertEqual(status, 200)
        entry = self._game_audit_entry(gid, "roster_selected")
        self.assertEqual(entry.actor_id, self.admin_id)
        self.assertNotEqual(entry.actor_id, FORGED_ACTOR)

    def test_roster_lock_actor_is_server_resolved(self):
        admin = self._login("admin")
        gid = self._fresh_game(admin)
        status, _ = self._req(
            admin, "POST", f"/api/games/{gid}/roster/lock",
            {"actor_id": FORGED_ACTOR})
        self.assertEqual(status, 200)
        entry = self._game_audit_entry(gid, "roster_locked")
        self.assertEqual(entry.actor_id, self.admin_id)
        self.assertNotEqual(entry.actor_id, FORGED_ACTOR)

    def test_officials_assign_actor_is_server_resolved(self):
        admin = self._login("admin")
        gid = self._fresh_game(admin)
        status, body = self._req(
            admin, "POST", f"/api/games/{gid}/officials/assign",
            {"official_id": srv.STATE.ids["referee_id"], "role": "referee",
             "actor_id": FORGED_ACTOR})
        self.assertEqual(status, 200)
        entry = self._setup_audit_entry("official_assigned", body["id"])
        self.assertEqual(entry.actor_id, self.admin_id)
        self.assertNotEqual(entry.actor_id, FORGED_ACTOR)
        self.assertEqual(body["assigned_by"], self.admin_id)

    def test_substitutes_enroll_actor_is_server_resolved(self):
        admin = self._login("admin")
        gid = self._fresh_game(admin)
        status, _ = self._req(
            admin, "POST", f"/api/games/{gid}/substitutes/enroll",
            {"player_id": srv.STATE.ids["substitute_player_id"],
             "actor_id": FORGED_ACTOR})
        self.assertEqual(status, 200)
        entry = self._game_audit_entry(gid, "substitute_enrolled")
        self.assertEqual(entry.actor_id, self.admin_id)
        self.assertNotEqual(entry.actor_id, FORGED_ACTOR)


if __name__ == "__main__":
    unittest.main()
