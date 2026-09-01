"""Real-HTTP contract for every concrete reassignment request body (#202).

The registry test proves the metadata and pure validator agree. This file
proves all sixteen admitted ``assign-*`` routes actually reach that validator
through their production handlers. Arbitrary record ids are deliberate: body
validation runs before context/resource lookup, so malformed input must return
the stable 400 envelope without disclosing whether a target exists.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)


# Independent wire oracle: path, its one accepted field, and whether explicit
# null is an allowed unassignment. Do not derive this from RouteSpec; this is
# the caller-visible contract RouteSpec must reproduce.
REASSIGNMENT_BODIES = (
    ("/api/setup/division/record/assign-level", "level_id", True),
    ("/api/setup/league/record/assign-organization",
     "organization_id", True),
    ("/api/setup/player/record/assign-team", "team_id", False),
    ("/api/setup/rink/record/assign-venue", "venue_id", False),
    ("/api/setup/season-team-registration/record/assign-division",
     "division_id", True),
    ("/api/setup/team/record/assign-club", "club_id", True),
    ("/api/setup/venue/record/assign-organization",
     "organization_id", True),
    ("/api/v2/setup/division/record/assign-league", "league_id", False),
    ("/api/v2/setup/player/record/assign-team", "team_id", False),
    ("/api/v2/setup/program/record/assign-organization",
     "operator_organization_id", True),
    ("/api/v2/setup/rink/record/assign-venue", "venue_id", False),
    ("/api/v2/setup/season-team-registration/record/assign-division",
     "division_id", True),
    ("/api/v2/setup/season-team-registration/record/assign-league",
     "league_id", False),
    ("/api/v2/setup/team/record/assign-club", "club_id", True),
    ("/api/v2/setup/team/record/assign-league", "league_id", False),
    ("/api/v2/setup/venue/record/assign-organization",
     "organization_id", True),
)


class ReassignmentBodyHttpContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(
            target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _request(self, client, path, body):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with client.open(request) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read() or b"{}")

    def _admin(self):
        client = self._client()
        status, payload = self._request(
            client, "/api/auth/login",
            {"username": "admin", "password": "demo"})
        self.assertEqual(status, 200, payload)
        return client

    def assertValidationReason(self, response, expected):
        status, payload = response
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["code"], "validation_error", payload)
        self.assertEqual(
            payload["error"]["details"]["reason"], expected, payload)

    def test_all_assign_routes_enforce_their_one_field_contract(self):
        self.assertEqual(len(REASSIGNMENT_BODIES), 16)
        self.assertEqual({nullable for _, _, nullable in REASSIGNMENT_BODIES},
                         {False, True})
        client = self._admin()

        for path, field, nullable in REASSIGNMENT_BODIES:
            with self.subTest(path=path, case="missing"):
                self.assertValidationReason(
                    self._request(client, path, {}), "field_required")
            with self.subTest(path=path, case="wrong-type"):
                self.assertValidationReason(
                    self._request(client, path, {field: 7}), "wrong_type")
            with self.subTest(path=path, case="unknown"):
                self.assertValidationReason(
                    self._request(
                        client, path, {field: "destination", "extra": True}),
                    "unknown_field")
            with self.subTest(path=path, case="explicit-null"):
                response = self._request(client, path, {field: None})
                if nullable:
                    # Validation passed; the arbitrary source id is then
                    # refused by the ordinary context/resource gate.
                    self.assertNotEqual(response[0], 400, response[1])
                else:
                    self.assertValidationReason(response, "field_required")


if __name__ == "__main__":
    unittest.main()
