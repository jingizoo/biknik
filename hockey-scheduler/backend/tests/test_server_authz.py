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

    def test_roles_endpoint_lists_roles(self):
        status, body = self._get("/api/auth/roles")
        self.assertEqual(status, 200)
        ids = {r["id"] for r in body["roles"]}
        self.assertEqual(ids, {"league_admin", "arena_manager", "coach",
                               "player", "official", "viewer"})
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


if __name__ == "__main__":
    unittest.main()
