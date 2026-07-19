"""Strict write schemas + consistent JSON errors (#271).

Contract tests that pin the HTTP write surface's fail-loud behavior:

* malformed / non-object JSON never becomes ``{}`` — it's a stable 400;
* unknown write-body keys are rejected with ``unknown_field`` (not dropped);
* a missing required field names the field (``field_required``) and writes
  nothing;
* a known path hit with an unsupported method returns a JSON 405 with a correct
  ``Allow`` header (not the stdlib HTML 501); an unknown path returns JSON 404;
* v1 Player/Official delete returns an explicit ``moved_to_v2`` (409);
* the ``venue_access_missing`` validation error carries a remediation hint.

A service-level check (dual-backend Memory + SQLite) pins that ``add_player``
with a ``None`` team_id is a ``field_required`` ValidationError even when called
directly, not the old ``NotFoundError("Team None not found.")``.
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

from hockey_scheduler.domain import Position, Team
from hockey_scheduler.domain.errors import ValidationError
from hockey_scheduler.services.setup_service import SetupService
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web import server as srv


def _clock():
    return datetime(2026, 7, 19, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Service layer: add_player names the missing team_id (criterion 4).           #
# --------------------------------------------------------------------------- #
class AddPlayerRequiredContract:
    """Backend-agnostic. Subclasses provide ``make_store``."""

    def setUp(self):
        self.store = self.make_store()
        self.setup = SetupService(self.store, _clock)

    def test_add_player_with_none_team_is_field_required(self):
        with self.assertRaises(ValidationError) as cm:
            self.setup.add_player(None, "Alice", Position.FORWARD)
        self.assertEqual(cm.exception.details.get("reason"), "field_required")
        self.assertEqual(cm.exception.details.get("field"), "team_id")
        # Nothing was written.
        self.assertEqual(self.store.all_players(), [])

    def test_add_player_with_empty_team_is_field_required(self):
        with self.assertRaises(ValidationError) as cm:
            self.setup.add_player("", "Alice", Position.FORWARD)
        self.assertEqual(cm.exception.details.get("field"), "team_id")
        self.assertEqual(self.store.all_players(), [])

    def test_add_player_with_valid_team_succeeds(self):
        self.store.add_team(Team(id="team_home", name="Lions"))
        player = self.setup.add_player("team_home", "Alice", Position.FORWARD)
        self.assertEqual(player.team_id, "team_home")


class MemoryAddPlayerTest(AddPlayerRequiredContract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


class DurableAddPlayerTest(AddPlayerRequiredContract, unittest.TestCase):
    def make_store(self):
        return SqlStore(":memory:")


# --------------------------------------------------------------------------- #
# HTTP layer contract tests.                                                   #
# --------------------------------------------------------------------------- #
class WriteSchemaHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.gid = srv.STATE.game_id
        cls.home = srv.STATE.ids["home_team_id"]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _req(self, opener, method, path, body=None, raw=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        if raw is not None:
            data = raw
        elif body is not None:
            data = json.dumps(body).encode()
        else:
            data = None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with opener.open(req) as r:
                return r.status, dict(r.headers), json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), json.loads(e.read() or b"{}")

    def _admin(self):
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": "admin", "password": "demo"})
        return c

    # -- malformed / non-object JSON (criterion 2) ---------------------------
    def test_malformed_json_is_400_not_empty_dict(self):
        admin = self._admin()
        status, _headers, body = self._req(
            admin, "POST", "/api/accounts", raw=b"{not valid json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "malformed_json")

    def test_non_object_json_body_is_400(self):
        admin = self._admin()
        status, _headers, body = self._req(
            admin, "POST", "/api/accounts", raw=b"[1, 2, 3]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "malformed_json")

    def test_empty_body_is_accepted(self):
        # An empty body stays a valid {} — logout takes no body.
        admin = self._admin()
        status, _headers, _body = self._req(admin, "POST", "/api/auth/logout")
        self.assertEqual(status, 200)

    # -- unknown-key rejection (criterion 1) ---------------------------------
    def test_unknown_key_on_account_create_is_rejected(self):
        admin = self._admin()
        status, _headers, body = self._req(
            admin, "POST", "/api/accounts",
            {"username": "u1", "password": "pw", "role": "viewer",
             "surprise": 1})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")
        self.assertEqual(body["error"]["details"]["reason"], "unknown_field")
        self.assertIn("surprise", body["error"]["details"]["fields"])
        self.assertIsNone(
            srv.STATE.api.store.get_user_account_by_username("u1"))

    def test_unknown_key_on_availability_is_rejected(self):
        # A typo'd status field must be rejected, not silently recorded as
        # pending. The player list gives us a real home player.
        admin = self._admin()
        store = srv.STATE.api.store
        pid = store.players_for_team(self.home)[0].id
        status, _headers, body = self._req(
            admin, "POST", f"/api/games/{self.gid}/availability",
            {"player_id": pid, "availabilty_status": "available"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"]["reason"], "unknown_field")
        self.assertIn("availabilty_status", body["error"]["details"]["fields"])

    def test_unknown_key_on_player_create_is_rejected(self):
        admin = self._admin()
        status, _headers, body = self._req(
            admin, "POST", "/api/setup/player",
            {"team_id": self.home, "name": "New", "position": "forward",
             "nickname": "Ace"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"]["reason"], "unknown_field")
        self.assertIn("nickname", body["error"]["details"]["fields"])

    # -- required-field naming (criterion 4) ---------------------------------
    def test_missing_team_id_on_player_create_names_field_writes_nothing(self):
        admin = self._admin()
        before = len(srv.STATE.api.store.all_players())
        status, _headers, body = self._req(
            admin, "POST", "/api/setup/player",
            {"name": "Ghost", "position": "forward"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")
        self.assertEqual(body["error"]["details"]["reason"], "field_required")
        self.assertEqual(body["error"]["details"]["field"], "team_id")
        # Nothing written and no stale "Team None not found" message.
        self.assertNotIn("None not found", body["error"]["message"])
        self.assertEqual(len(srv.STATE.api.store.all_players()), before)

    def test_missing_team_id_on_v2_player_create_names_field(self):
        admin = self._admin()
        status, _headers, body = self._req(
            admin, "POST", "/api/v2/setup/player",
            {"name": "Ghost2", "position": "forward"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"]["field"], "team_id")

    # -- availability value validation (criterion via #271 design) -----------
    def test_availability_unknown_status_value_is_400(self):
        admin = self._admin()
        store = srv.STATE.api.store
        pid = store.players_for_team(self.home)[0].id
        status, _headers, body = self._req(
            admin, "POST", f"/api/games/{self.gid}/availability",
            {"player_id": pid, "availability_status": "banana"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")

    def test_availability_wrong_type_is_400(self):
        admin = self._admin()
        store = srv.STATE.api.store
        pid = store.players_for_team(self.home)[0].id
        status, _headers, body = self._req(
            admin, "POST", f"/api/games/{self.gid}/availability",
            {"player_id": pid, "availability_status": 7})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"]["reason"], "wrong_type")

    def test_availability_valid_status_succeeds(self):
        admin = self._admin()
        store = srv.STATE.api.store
        pid = store.players_for_team(self.home)[0].id
        status, _headers, _body = self._req(
            admin, "POST", f"/api/games/{self.gid}/availability",
            {"player_id": pid, "availability_status": "available"})
        self.assertEqual(status, 200)

    # -- method 405 / 404 (criterion 3) --------------------------------------
    def test_put_on_players_is_405_with_allow_header(self):
        admin = self._admin()
        status, headers, body = self._req(admin, "PUT", "/api/players")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "GET")
        self.assertEqual(body["error"]["code"], "method_not_allowed")
        self.assertEqual(body["error"]["details"]["allow"], "GET")

    def test_patch_on_players_is_405(self):
        admin = self._admin()
        status, headers, _body = self._req(admin, "PATCH", "/api/players")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "GET")

    def test_delete_on_game_board_is_405_allow_get(self):
        admin = self._admin()
        status, headers, body = self._req(
            admin, "DELETE", f"/api/games/{self.gid}/board")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "GET")
        self.assertEqual(body["error"]["code"], "method_not_allowed")

    def test_put_on_accounts_is_405_allow_get_post(self):
        admin = self._admin()
        status, headers, _body = self._req(admin, "PUT", "/api/accounts")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "GET, POST")

    def test_delete_on_unknown_api_path_is_404_json(self):
        admin = self._admin()
        status, headers, body = self._req(admin, "DELETE", "/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertEqual(body["error"]["code"], "not_found")

    def test_delete_on_non_api_path_is_404_json(self):
        admin = self._admin()
        status, _headers, body = self._req(admin, "DELETE", "/definitely/not/here")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    # -- v1 Player/Official delete moved to v2 (criterion 5) -----------------
    def test_v1_player_delete_is_moved_to_v2(self):
        admin = self._admin()
        store = srv.STATE.api.store
        pid = store.players_for_team(self.home)[0].id
        status, _headers, body = self._req(
            admin, "POST", f"/api/setup/player/{pid}/delete")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "moved_to_v2")
        self.assertEqual(body["error"]["details"]["route"],
                         f"/api/v2/setup/player/{pid}/delete")
        # The player was NOT deleted.
        self.assertIsNotNone(store.get_player(pid))

    def test_v1_official_delete_is_moved_to_v2(self):
        admin = self._admin()
        status, _headers, body = self._req(
            admin, "POST", "/api/setup/official/off_x/delete")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "moved_to_v2")
        self.assertEqual(body["error"]["details"]["entity"], "official")

    # -- official assignment respond rejects a stray key ---------------------
    def test_official_assignment_respond_rejects_unknown_key(self):
        admin = self._admin()
        status, _headers, body = self._req(
            admin, "POST", "/api/officials/assignments/asg_x/accept",
            {"note": "hi"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"]["reason"], "unknown_field")


# --------------------------------------------------------------------------- #
# venue_access_missing remediation (criterion 6).                              #
# --------------------------------------------------------------------------- #
class VenueAccessRemediationTest(unittest.TestCase):
    def test_remediation_hint_present(self):
        from hockey_scheduler.services.league_scope import (
            require_slot_belongs_to_season)
        from hockey_scheduler.domain import IceSlot, Rink, Season, Venue

        store = InMemoryStore()
        store.add_season(Season(id="season_1", program_id="prog_1", name="S",
                                start_date="2026-01-01", end_date="2026-06-01"))
        store.add_venue(Venue(id="venue_1", name="Rink House"))
        store.add_rink(Rink(id="rink_1", venue_id="venue_1", name="Sheet A"))
        store.add_ice_slot(IceSlot(
            id="slot_1", rink_id="rink_1",
            start_time=datetime(2026, 2, 1, 18, tzinfo=timezone.utc),
            end_time=datetime(2026, 2, 1, 19, 30, tzinfo=timezone.utc)))
        with self.assertRaises(ValidationError) as cm:
            require_slot_belongs_to_season(store, "slot_1", "season_1")
        details = cm.exception.details
        self.assertEqual(details["reason"], "venue_access_missing")
        self.assertIn("remediation", details)
        self.assertEqual(details["remediation_route"],
                         "/api/v2/setup/seasons/season_1/venue-access")


if __name__ == "__main__":
    unittest.main()
