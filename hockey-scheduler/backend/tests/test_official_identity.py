import json
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import Role
from hockey_scheduler.web import server as srv
from hockey_scheduler.web.scope import scope_violation


class OfficialScopeUnitTest(unittest.TestCase):
    """An official may only respond to their own assignment (#54)."""

    def setUp(self):
        self.store, self.game_id, self.ids = __import__(
            "hockey_scheduler.full_demo", fromlist=["build_full_demo_store"]
        ).build_full_demo_store()
        self.assignment_id = self.ids["ref_assignment_id"]
        self.ref_id = self.ids["referee_id"]

    def test_official_can_respond_to_own(self):
        scope = {"official_id": self.ref_id}
        path = f"/api/officials/assignments/{self.assignment_id}/accept"
        self.assertIsNone(scope_violation(Role.OFFICIAL, scope, path, {}, self.store))

    def test_official_cannot_respond_to_others(self):
        scope = {"official_id": "official_other"}
        path = f"/api/officials/assignments/{self.assignment_id}/accept"
        self.assertIsNotNone(
            scope_violation(Role.OFFICIAL, scope, path, {}, self.store))

    def test_unbound_official_cannot_respond(self):
        # Self-service identity: an unbound official (dev header) may not respond.
        path = f"/api/officials/assignments/{self.assignment_id}/decline"
        self.assertIsNotNone(scope_violation(Role.OFFICIAL, {}, path, {}, self.store))


class OfficialHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.gid = srv.STATE.game_id
        cls.assignment_id = srv.STATE.ids["ref_assignment_id"]
        cls.ref_id = srv.STATE.ids["referee_id"]
        # A second official + a second assignment (on the future game) to test
        # cross-official scoping.
        store = srv.STATE.api.store
        other = srv.STATE.api.create_official("Other Ref")
        cls.other_official_id = other["id"]
        a = srv.STATE.api.assign_official(
            srv.STATE.ids["next_game_id"], cls.other_official_id, "referee")
        cls.other_assignment_id = a["id"]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def _login(self, username):
        c = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        self._post(c, "/api/auth/login", {"username": username, "password": "demo"})
        return c

    def _post(self, opener, path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _me(self, opener):
        with opener.open(f"http://127.0.0.1:{self.port}/api/auth/me") as r:
            return json.loads(r.read() or b"{}")

    def test_official_session_bound_to_official(self):
        c = self._login("official")
        user = self._me(c)["user"]
        self.assertEqual(user["role"], "official")
        self.assertEqual(user["scope"]["official_id"], self.ref_id)
        self.assertIn("official_name", user["scope"])

    def test_official_accepts_own_assignment(self):
        c = self._login("official")
        status, _ = self._post(
            c, f"/api/officials/assignments/{self.assignment_id}/accept", {})
        self.assertNotEqual(status, 403)

    def test_official_cannot_accept_another_assignment(self):
        c = self._login("official")
        status, body = self._post(
            c, f"/api/officials/assignments/{self.other_assignment_id}/accept", {})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")

    def test_official_cannot_assign(self):
        c = self._login("official")
        status, _ = self._post(
            c, f"/api/games/{self.gid}/officials/assign",
            {"official_id": self.ref_id, "role": "linesperson"})
        self.assertEqual(status, 403)

    def test_official_cannot_unassign(self):
        c = self._login("official")
        status, _ = self._post(
            c, f"/api/officials/assignments/{self.assignment_id}/unassign", {})
        self.assertEqual(status, 403)

    def test_unbound_official_header_cannot_respond(self):
        # No session — just the dev X-Demo-Role header, so no official_id binding.
        c = urllib.request.build_opener()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}"
            f"/api/officials/assignments/{self.assignment_id}/accept",
            data=b"{}", method="POST",
            headers={"Content-Type": "application/json", "X-Demo-Role": "official"})
        try:
            with c.open(req) as r:
                status = r.status
        except urllib.error.HTTPError as e:
            status = e.code
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
