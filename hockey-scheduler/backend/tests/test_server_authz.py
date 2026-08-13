import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.web.server import STATE, Handler


class ServerAuthzTest(unittest.TestCase):
    """End-to-end: the HTTP boundary enforces the role permission model (#24)."""

    @classmethod
    def setUpClass(cls):
        STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.game_id = STATE.game_id

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()

    def _post(self, path, body=None, role=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        if role is not None:
            req.add_header("X-Demo-Role", role)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as r:
            return r.status, json.loads(r.read() or b"{}")

    def _get_signed_in(self, path):
        # #367: /api/demo/overview needs a real signed-in session (not just
        # X-Demo-Role, which carries no user_id to resolve a context from).
        login = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/auth/login",
            data=json.dumps({"username": "admin", "password": "demo"}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(login) as r:
            cookie = r.headers.get("Set-Cookie", "").split(";", 1)[0]
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", headers={"Cookie": cookie})
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}")

    def _get_h(self, path, role=None, cookie=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, method="GET")
        if role is not None:
            req.add_header("X-Demo-Role", role)
        if cookie is not None:
            req.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_roles_endpoint_lists_roles(self):
        status, body = self._get("/api/auth/roles")
        self.assertEqual(status, 200)
        ids = {r["id"] for r in body["roles"]}
        self.assertEqual(ids, {"league_admin", "arena_manager", "coach",
                               "player", "official", "viewer", "guardian"})
        self.assertEqual(body["default"], "league_admin")

    def test_viewer_cannot_create_league(self):
        status, body = self._post("/api/setup/league", {"name": "X"}, role="viewer")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")
        self.assertEqual(body["error"]["details"]["required"], "manage_setup")

    def test_coach_cannot_move_game(self):
        status, body = self._post(f"/api/games/{self.game_id}/move",
                                  {"ice_slot_id": "slot_x"}, role="coach")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["details"]["role"], "coach")

    def test_coach_can_reach_roster_action(self):
        # Not a 403 — authorized (may still fail validation, but not forbidden).
        status, body = self._post(f"/api/games/{self.game_id}/roster/lock",
                                  {}, role="coach")
        self.assertNotEqual(status, 403)

    def test_missing_header_defaults_to_admin(self):
        status, body = self._post(f"/api/games/{self.game_id}/roster/lock", {})
        self.assertNotEqual(status, 403)

    def test_invalid_role_does_not_escalate_to_admin(self):
        # A supplied-but-unknown role must be rejected, never treated as admin.
        status, body = self._post("/api/setup/league", {"name": "Sneaky"},
                                  role="nonsense")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")
        # And it must not have created the league.
        _, ov = self._get_signed_in("/api/demo/overview")
        self.assertNotIn("Sneaky", [l["name"] for l in ov["leagues"]])

    # -- delivery-queue overview is operator-only (#58) --------------------
    def test_viewer_cannot_read_delivery_overview(self):
        status, body = self._get_h("/api/notifications/deliveries", role="viewer")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")

    def test_operator_can_read_delivery_overview(self):
        for role in ("league_admin", "arena_manager"):
            status, body = self._get_h("/api/notifications/deliveries", role=role)
            self.assertEqual(status, 200, role)
            self.assertIn("by_status", body)

    def test_invalid_cookie_on_delivery_overview_is_401(self):
        status, body = self._get_h("/api/notifications/deliveries",
                                   cookie="hs_sid=bogus-session")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

    # -- setup hierarchy tree is operator-only, MANAGE_SETUP (#166 PR C) ----
    def test_viewer_cannot_read_setup_hierarchy(self):
        status, body = self._get_h("/api/setup/hierarchy", role="viewer")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")
        self.assertEqual(body["error"]["details"]["required"], "manage_setup")

    def test_admin_can_read_setup_hierarchy(self):
        status, body = self._get_h("/api/setup/hierarchy", role="league_admin")
        self.assertEqual(status, 200)
        self.assertIn("organizations", body)
        self.assertIn("leagues", body)
        self.assertIn("missing_assignments", body)

    # -- reassignment routes carry the same gate as their create sibling ----
    def test_viewer_cannot_reassign_team_club(self):
        status, body = self._post("/api/setup/team/team_x/assign-club",
                                  {"club_id": "club_x"}, role="viewer")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["details"]["required"], "manage_setup")

    def test_arena_manager_gates_venue_vs_division_reassignment(self):
        # An Arena Manager holds MANAGE_ARENA but not MANAGE_SETUP: the
        # facility-side venue move is authorized (not 403), while the
        # league-side division move is forbidden.
        status, _ = self._post("/api/setup/venue/venue_x/assign-organization",
                               {"organization_id": None}, role="arena_manager")
        self.assertNotEqual(status, 403)
        status, body = self._post("/api/setup/division/div_x/assign-level",
                                  {"level_id": None}, role="arena_manager")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["details"]["required"], "manage_setup")

    def test_cross_domain_league_venue_moves_are_setup_only(self):
        # Tying a league to an owner spans both domains (#173): MANAGE_SETUP
        # only. An Arena Manager (MANAGE_ARENA) is forbidden even though its
        # plain venue moves are allowed.
        for path, body in (
            ("/api/setup/league/league_x/assign-organization", {"organization_id": None}),
        ):
            status, resp = self._post(path, body, role="arena_manager")
            self.assertEqual(status, 403, path)
            self.assertEqual(resp["error"]["details"]["required"], "manage_setup", path)

    # -- contact registry is operator-only (#60) ---------------------------
    def test_viewer_cannot_read_or_write_contacts(self):
        status, _ = self._get_h("/api/notifications/contacts", role="viewer")
        self.assertEqual(status, 403)
        status, _ = self._post("/api/notifications/contacts",
                               {"recipient_ref": "scheduler", "channel": "email",
                                "destination": "x@y.invalid"}, role="viewer")
        self.assertEqual(status, 403)

    def test_operator_can_manage_contacts(self):
        status, body = self._post(
            "/api/notifications/contacts",
            {"recipient_ref": "scheduler", "channel": "email",
             "destination": "ops@contacts.invalid"}, role="league_admin")
        self.assertEqual(status, 200)
        self.assertEqual(body["destination"], "ops@contacts.invalid")
        status, body = self._get_h("/api/notifications/contacts",
                                   role="arena_manager")
        self.assertEqual(status, 200)
        self.assertIn("contacts", body)

    def test_invalid_cookie_on_contacts_is_401(self):
        status, body = self._get_h("/api/notifications/contacts",
                                   cookie="hs_sid=bogus-session")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

    # -- device token registry is operator-only (#65) ---------------------
    def test_viewer_cannot_read_or_write_device_tokens(self):
        status, _ = self._get_h("/api/notifications/device-tokens", role="viewer")
        self.assertEqual(status, 403)
        status, _ = self._post("/api/notifications/device-tokens",
                               {"recipient_ref": "scheduler", "provider": "fcm",
                                "token": "tok"}, role="viewer")
        self.assertEqual(status, 403)

    def test_operator_can_manage_device_tokens(self):
        status, body = self._post(
            "/api/notifications/device-tokens",
            {"recipient_ref": "scheduler", "provider": "fcm",
             "token": "tok-http"}, role="league_admin")
        self.assertEqual(status, 200)
        self.assertTrue(body["active"])
        status, deact = self._post(
            f"/api/notifications/device-tokens/{body['id']}/active",
            {"active": False}, role="league_admin")
        self.assertEqual(status, 200)
        self.assertFalse(deact["active"])
        status, listing = self._get_h("/api/notifications/device-tokens",
                                      role="arena_manager")
        self.assertEqual(status, 200)
        self.assertIn("device_tokens", listing)

    # -- user accounts are league-admin-only, narrower than MANAGE_SCHEDULE (#67) --
    def test_viewer_cannot_read_or_write_accounts(self):
        status, _ = self._get_h("/api/accounts", role="viewer")
        self.assertEqual(status, 403)
        status, _ = self._post("/api/accounts",
                               {"username": "sneaky", "password": "pw",
                                "role": "league_admin"}, role="viewer")
        self.assertEqual(status, 403)

    def test_arena_manager_cannot_manage_accounts(self):
        # MANAGE_USERS is narrower than MANAGE_SCHEDULE — an arena manager can
        # drain the delivery queue but must not be able to create logins.
        status, _ = self._get_h("/api/accounts", role="arena_manager")
        self.assertEqual(status, 403)
        status, _ = self._post("/api/accounts",
                               {"username": "sneaky2", "password": "pw",
                                "role": "viewer"}, role="arena_manager")
        self.assertEqual(status, 403)

    def test_league_admin_can_create_and_deactivate_accounts(self):
        # A coach account now requires a real team scope (#266).
        home_team = STATE.ids["home_team_id"]
        status, body = self._post(
            "/api/accounts",
            {"username": "http_created", "password": "pw", "role": "coach",
             "scope": {"team_id": home_team}}, role="league_admin")
        self.assertEqual(status, 200)
        self.assertEqual(body["role"], "coach")
        self.assertNotIn("password_hash", body)
        status, listing = self._get_h("/api/accounts", role="league_admin")
        self.assertEqual(status, 200)
        self.assertIn("http_created",
                      [a["username"] for a in listing["user_accounts"]])
        status, deact = self._post(
            f"/api/accounts/{body['id']}/active",
            {"active": False}, role="league_admin")
        self.assertEqual(status, 200)
        self.assertFalse(deact["active"])

    def test_invalid_cookie_on_accounts_is_401(self):
        status, body = self._get_h("/api/accounts", cookie="hs_sid=bogus-session")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")


# --------------------------------------------------------------------------- #
# #202 repair round 4, finding 5: /api/auth/me, /api/me/assignments, and       #
# /api/me/player-home were labelled auth="session" in route_registry.py --    #
# the SAME label used for a route that 401s outright with no cookie -- but    #
# each of these three answers a NO-COOKIE request with an ANONYMOUS 200       #
# (null/empty data), only 401ing for a cookie that IS present but invalid/    #
# expired. Investigated first (server.py:2045-2057, 1827-1843, 1844-1863):    #
# each is a deliberate, consistently-documented "who am I / my inbox / my     #
# home screen" pattern -- explicitly NOT calling _resolve_role (which would   #
# 401 on no cookie) and instead reading the cookie directly, matching the     #
# exact contract every SPA needs on load to tell "signed out" from "signed    #
# in" without an error. NOT a bug -- confirmed against real HTTP below,       #
# across all three cookie states, for all three routes.                       #
# --------------------------------------------------------------------------- #
class OptionalSessionRouteTests(unittest.TestCase):
    """Real-HTTP proof that route_registry.py's new 'optional_session' label
    matches actual server behaviour for exactly these three routes, in all
    three cookie states -- the demonstration this finding's own fix is
    checked in against, not merely asserted in a commit message."""

    @classmethod
    def setUpClass(cls):
        STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        api = STATE.api
        # A real, BOUND official account (#54) -- so the "valid cookie"
        # case for /api/me/assignments proves a REAL inbox shape, not just
        # "the same empty shape as no-cookie, via a different code path".
        official_id = api.create_official("Finding5 Official")["id"]
        api.create_user_account("finding5_official", "pw", "official",
                                scope={"official_id": official_id})
        cls.official_id = official_id
        # A real, BOUND player account (#107) -- same reasoning for
        # /api/me/player-home.
        home_team = STATE.ids["home_team_id"]
        player_id = api.create_player(home_team, "Finding5 Player", "forward")["id"]
        api.create_user_account("finding5_player", "pw", "player",
                                scope={"player_id": player_id})
        cls.player_id = player_id

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()

    def _get_h(self, path, cookie=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, method="GET")
        if cookie is not None:
            req.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _login(self, username, password):
        url = f"http://127.0.0.1:{self.port}/api/auth/login"
        data = json.dumps({"username": username, "password": password}).encode()
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return r.headers.get("Set-Cookie", "").split(";", 1)[0]

    # -- /api/auth/me --------------------------------------------------
    def test_auth_me_no_cookie_is_anonymous_200(self):
        status, body = self._get_h("/api/auth/me")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"user": None})

    def test_auth_me_invalid_cookie_is_401(self):
        status, body = self._get_h("/api/auth/me", cookie="hs_sid=bogus-session")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

    def test_auth_me_valid_cookie_returns_the_real_user(self):
        cookie = self._login("admin", "demo")
        status, body = self._get_h("/api/auth/me", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIsNotNone(body["user"])
        self.assertEqual(body["user"]["username"], "admin")

    # -- /api/me/assignments --------------------------------------------
    def test_me_assignments_no_cookie_is_anonymous_200(self):
        status, body = self._get_h("/api/me/assignments")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"official_id": None, "assignments": []})

    def test_me_assignments_invalid_cookie_is_401(self):
        status, body = self._get_h("/api/me/assignments",
                                   cookie="hs_sid=bogus-session")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

    def test_me_assignments_valid_bound_cookie_returns_the_real_inbox(self):
        cookie = self._login("finding5_official", "pw")
        status, body = self._get_h("/api/me/assignments", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["official_id"], self.official_id)
        self.assertIn("assignments", body)

    def test_me_assignments_valid_unbound_cookie_is_the_same_empty_shape(self):
        """A valid session with NO official binding (e.g. an admin) gets the
        SAME empty shape as no cookie at all -- but via _cookie/SESSIONS.
        resolve succeeding, not the early no-cookie return. Distinguishes
        'no session' from 'session but not an official' from the response
        alone being intentionally identical for both -- exercised here so a
        future change that made them diverge would be caught."""
        cookie = self._login("admin", "demo")
        status, body = self._get_h("/api/me/assignments", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"official_id": None, "assignments": []})

    # -- /api/me/player-home ---------------------------------------------
    def test_me_player_home_no_cookie_is_anonymous_200(self):
        status, body = self._get_h("/api/me/player-home")
        self.assertEqual(status, 200)
        self.assertEqual(body, {
            "player_id": None, "next_game": None, "today_count": 0,
            "substitute_offers": [], "substitute_opportunities": [],
            "unread_notifications": 0})

    def test_me_player_home_invalid_cookie_is_401(self):
        status, body = self._get_h("/api/me/player-home",
                                   cookie="hs_sid=bogus-session")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

    def test_me_player_home_valid_bound_cookie_returns_the_real_home(self):
        cookie = self._login("finding5_player", "pw")
        status, body = self._get_h("/api/me/player-home", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body["player_id"], self.player_id)


if __name__ == "__main__":
    unittest.main()
