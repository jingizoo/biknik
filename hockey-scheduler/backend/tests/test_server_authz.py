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
        _, ov = self._get("/api/demo/overview")
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
        status, body = self._post(
            "/api/accounts",
            {"username": "http_created", "password": "pw", "role": "coach",
             "scope": {"team_id": "team_x"}}, role="league_admin")
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


if __name__ == "__main__":
    unittest.main()
