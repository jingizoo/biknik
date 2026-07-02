"""iCal calendar feeds (#82).

A revocable bearer token scopes a public iCal feed to one actor. Team and
player feeds are fixtures only (no roster/player names); the official feed
covers only that official's assigned games. Only the token hash is stored.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Player
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.services import build_ics, games_for_actor, hash_feed_token

UTC = timezone.utc


class CalendarServiceTest(unittest.TestCase):
    def setUp(self):
        self.store, self.gid, self.ids = build_full_demo_store()
        self.api = ApiService(self.store)
        self.home = self.ids["home_team_id"]
        self.away = self.ids["away_team_id"]
        self.ref = self.ids["referee_id"]
        self.player = self.ids["selected_player_id"]

    def test_team_feed_only_includes_that_teams_games(self):
        games = games_for_actor(self.store, "team", self.home)
        self.assertTrue(games)
        for g in games:
            self.assertIn(self.home, (g.home_team_id, g.away_team_id))

    def test_official_feed_only_assigned_games(self):
        games = games_for_actor(self.store, "official", self.ref)
        assigned = {a.game_id
                    for a in self.store.assignments_for_official(self.ref)
                    if a.status.is_active}
        self.assertTrue(assigned)
        self.assertEqual({g.id for g in games}, assigned)

    def test_player_feed_uses_players_team(self):
        p = self.store.get_player(self.player)
        games = games_for_actor(self.store, "player", self.player)
        for g in games:
            self.assertIn(p.team_id, (g.home_team_id, g.away_team_id))

    def test_ics_is_wellformed_and_leaks_no_player_names(self):
        now = datetime(2026, 3, 1, tzinfo=UTC)
        ics = build_ics(self.store, "team", self.home, now)
        self.assertTrue(ics.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertIn("END:VCALENDAR", ics)
        self.assertIn("BEGIN:VEVENT", ics)
        self.assertIn("SUMMARY:", ics)
        names = [p.name for p in self.store._query(Player)] \
            if hasattr(self.store, "_query") else \
            [p.name for p in self.store.players.values()]
        for name in names:
            self.assertNotIn(name, ics)

    def test_official_feed_carries_role_note(self):
        now = datetime(2026, 3, 1, tzinfo=UTC)
        ics = build_ics(self.store, "official", self.ref, now)
        self.assertIn("Role:", ics)

    # -- audit trail (#82) -------------------------------------------------
    def test_mint_writes_audit_without_token_material(self):
        before = len(self.store.all_setup_audit())
        row = self.api.create_calendar_feed_token(
            "team", self.home, label="Home sync", actor_id="user_admin")
        entries = self.store.all_setup_audit()
        self.assertEqual(len(entries), before + 1)
        e = entries[-1]
        self.assertEqual(e.action, "calendar_feed_token_created")
        self.assertEqual(e.entity_type, "calendar_feed_token")
        self.assertEqual(e.entity_id, row["id"])
        self.assertEqual(e.actor_id, "user_admin")
        self.assertEqual(e.detail["actor_type"], "team")
        self.assertEqual(e.detail["actor_ref"], self.home)
        self.assertEqual(e.detail["label"], "Home sync")
        # The audit record must never carry the raw token or its hash.
        blob = json.dumps(e.detail)
        self.assertNotIn(row["token"], blob)
        self.assertNotIn("token_hash", blob)
        self.assertNotIn(hash_feed_token(row["token"]), blob)

    def test_revoke_writes_audit(self):
        row = self.api.create_calendar_feed_token(
            "team", self.home, actor_id="user_admin")
        before = len(self.store.all_setup_audit())
        self.api.revoke_calendar_feed_token(row["id"], actor_id="user_admin")
        e = self.store.all_setup_audit()[-1]
        self.assertEqual(len(self.store.all_setup_audit()), before + 1)
        self.assertEqual(e.action, "calendar_feed_token_revoked")
        self.assertEqual(e.entity_type, "calendar_feed_token")
        self.assertEqual(e.entity_id, row["id"])
        self.assertEqual(e.actor_id, "user_admin")
        self.assertFalse(e.detail["already_revoked"])
        blob = json.dumps(e.detail)
        self.assertNotIn(row["token"], blob)
        self.assertNotIn("token_hash", blob)

    def test_repeat_revoke_flags_already_revoked(self):
        row = self.api.create_calendar_feed_token(
            "team", self.home, actor_id="user_admin")
        self.api.revoke_calendar_feed_token(row["id"], actor_id="user_admin")
        self.api.revoke_calendar_feed_token(row["id"], actor_id="user_admin")
        e = self.store.all_setup_audit()[-1]
        self.assertEqual(e.action, "calendar_feed_token_revoked")
        self.assertTrue(e.detail["already_revoked"])  # idempotent no-op recorded


class CalendarHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
        cls.srv = srv
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.home = srv.STATE.ids["home_team_id"]
        cls.ref = srv.STATE.ids["referee_id"]
        cls.player = srv.STATE.ids["selected_player_id"]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _req(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req) as r:
                ctype = r.headers.get("Content-Type", "")
                raw = r.read()
                body = json.loads(raw or b"{}") if "json" in ctype else raw.decode()
                return r.status, ctype, body
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), \
                json.loads(e.read() or b"{}")

    def test_valid_token_returns_ics_and_revoked_returns_404(self):
        admin = self._client()
        self._req(admin, "POST", "/api/auth/login",
                  {"username": "admin", "password": "demo"})
        status, _, created = self._req(
            admin, "POST", "/api/calendar-feeds",
            {"actor_type": "team", "actor_ref": self.home})
        self.assertEqual(status, 200)
        token = created["token"]
        self.assertNotIn("token_hash", created)

        # Public, unauthenticated fetch of the feed.
        pub = urllib.request.build_opener()
        s, ctype, text = self._reqx(pub, f"/calendar/team/{token}.ics")
        self.assertEqual(s, 200)
        self.assertIn("text/calendar", ctype)
        self.assertIn("BEGIN:VCALENDAR", text)

        # Revoke → the feed 404s.
        self._req(admin, "POST",
                  f"/api/calendar-feeds/{created['id']}/revoke")
        s2, _, _ = self._reqx(pub, f"/calendar/team/{token}.ics")
        self.assertEqual(s2, 404)

    def _reqx(self, opener, path):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        try:
            with opener.open(req) as r:
                return r.status, r.headers.get("Content-Type", ""), \
                    r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read().decode()

    def test_unknown_and_mismatched_tokens_404(self):
        pub = urllib.request.build_opener()
        self.assertEqual(self._reqx(pub, "/calendar/team/bogus.ics")[0], 404)
        # A token minted for a team must not resolve on the official route.
        admin = self._client()
        self._req(admin, "POST", "/api/auth/login",
                  {"username": "admin", "password": "demo"})
        _, _, created = self._req(
            admin, "POST", "/api/calendar-feeds",
            {"actor_type": "team", "actor_ref": self.home})
        self.assertEqual(
            self._reqx(pub, f"/calendar/official/{created['token']}.ics")[0], 404)

    def test_list_never_exposes_token_material(self):
        admin = self._client()
        self._req(admin, "POST", "/api/auth/login",
                  {"username": "admin", "password": "demo"})
        self._req(admin, "POST", "/api/calendar-feeds",
                  {"actor_type": "team", "actor_ref": self.home})
        status, _, body = self._req(
            admin, "GET",
            f"/api/calendar-feeds?actor_type=team&actor_ref={self.home}")
        self.assertEqual(status, 200)
        blob = json.dumps(body)
        self.assertNotIn("token_hash", blob)
        for t in body["feed_tokens"]:
            self.assertNotIn("token", t)

    def test_mint_and_revoke_over_http_write_audit(self):
        admin = self._client()
        self._req(admin, "POST", "/api/auth/login",
                  {"username": "admin", "password": "demo"})
        _, _, created = self._req(
            admin, "POST", "/api/calendar-feeds",
            {"actor_type": "team", "actor_ref": self.home,
             # A forged actor_id in the body must be ignored (server resolves).
             "actor_id": "attacker"})
        _, _, ov = self._req(admin, "GET", "/api/demo/overview")
        actions = [a["action"] for a in ov["setup_audit"]]
        self.assertIn("calendar_feed_token_created", actions)
        # And the created audit points at the real token id, not the raw token.
        mint = [a for a in ov["setup_audit"]
                if a["action"] == "calendar_feed_token_created"][-1]
        self.assertEqual(mint["entity_id"], created["id"])
        # The overview audit never carries the raw token string itself.
        self.assertNotIn(created["token"], json.dumps(ov["setup_audit"]))

        self._req(admin, "POST", f"/api/calendar-feeds/{created['id']}/revoke")
        _, _, ov2 = self._req(admin, "GET", "/api/demo/overview")
        self.assertIn("calendar_feed_token_revoked",
                      [a["action"] for a in ov2["setup_audit"]])

    def test_user_cannot_manage_another_actors_feed(self):
        coach = self._client()
        self._req(coach, "POST", "/api/auth/login",
                  {"username": "coach", "password": "demo"})
        # Coach owns the home team; a different team is forbidden.
        status, _, _ = self._req(
            coach, "POST", "/api/calendar-feeds",
            {"actor_type": "team", "actor_ref": "team_elsewhere"})
        self.assertEqual(status, 403)
        # Own team is allowed.
        ok, _, _ = self._req(
            coach, "POST", "/api/calendar-feeds",
            {"actor_type": "team", "actor_ref": self.home})
        self.assertEqual(ok, 200)

    # -- role-specific feed ownership (#82) --------------------------------
    def _login(self, username):
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": username, "password": "demo"})
        return c

    def test_player_manages_own_player_feed_but_not_team_feed(self):
        player = self._login("player")
        # Own player feed: create, list, revoke all allowed.
        st, _, created = self._req(
            player, "POST", "/api/calendar-feeds",
            {"actor_type": "player", "actor_ref": self.player})
        self.assertEqual(st, 200)
        st_l, _, _ = self._req(
            player, "GET",
            f"/api/calendar-feeds?actor_type=player&actor_ref={self.player}")
        self.assertEqual(st_l, 200)
        st_r, _, _ = self._req(
            player, "POST", f"/api/calendar-feeds/{created['id']}/revoke")
        self.assertEqual(st_r, 200)
        # The shared team feed — even for the player's OWN team — is forbidden
        # on create, list, and revoke (the player carries team_id in scope but
        # does not own the team resource).
        st_c, _, _ = self._req(
            player, "POST", "/api/calendar-feeds",
            {"actor_type": "team", "actor_ref": self.home})
        self.assertEqual(st_c, 403)
        st_gl, _, _ = self._req(
            player, "GET",
            f"/api/calendar-feeds?actor_type=team&actor_ref={self.home}")
        self.assertEqual(st_gl, 403)

    def test_coach_cannot_manage_player_feed(self):
        coach = self._login("coach")
        st, _, _ = self._req(
            coach, "POST", "/api/calendar-feeds",
            {"actor_type": "player", "actor_ref": self.player})
        self.assertEqual(st, 403)

    def test_operator_manages_any_actor_feed(self):
        admin = self._login("admin")
        for actor_type, ref in (("team", self.home), ("official", self.ref),
                                ("player", self.player)):
            st, _, _ = self._req(
                admin, "POST", "/api/calendar-feeds",
                {"actor_type": actor_type, "actor_ref": ref})
            self.assertEqual(st, 200, f"operator denied {actor_type}")

    def test_player_cannot_revoke_coach_created_team_feed(self):
        # The critical regression: a coach mints a team feed; a player on that
        # same team must not be able to revoke it via the API.
        coach = self._login("coach")
        _, _, team_feed = self._req(
            coach, "POST", "/api/calendar-feeds",
            {"actor_type": "team", "actor_ref": self.home})
        self.assertIn("id", team_feed)

        player = self._login("player")
        st, _, _ = self._req(
            player, "POST", f"/api/calendar-feeds/{team_feed['id']}/revoke")
        self.assertEqual(st, 403)
        # And the feed is still live: the coach's token still resolves.
        pub = urllib.request.build_opener()
        s, ctype, _ = self._reqx(pub, f"/calendar/team/{team_feed['token']}.ics")
        self.assertEqual(s, 200)
        self.assertIn("text/calendar", ctype)


if __name__ == "__main__":
    unittest.main()
