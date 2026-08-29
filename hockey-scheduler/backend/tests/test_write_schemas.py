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
import os
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from unittest.mock import patch

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


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class PostgresAddPlayerTest(AddPlayerRequiredContract, unittest.TestCase):
    def make_store(self):
        store = SqlStore(os.environ["TEST_DATABASE_URL"])
        store.clear_all_data()  # isolate from any prior run's rows
        return store


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
        cls.httpd.server_close()

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
        # #409: a guarded setup MUTATION (this class drives
        # `team/<id>/assign-club`) now needs an EXPLICITLY persisted context
        # rather than the read-only fallback. Persist the very tuple this
        # session already resolves, so nothing else about the case changes.
        _s, _h, ctx = self._req(c, "GET", "/api/context")
        self._req(c, "POST", "/api/context",
                  {"program_id": ctx.get("program_id"),
                   "season_id": ctx.get("season_id"),
                   "league_id": ctx.get("league_id")})
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

    def test_malformed_post_on_get_only_route_is_405_not_400(self):
        # The method contract is decided BEFORE the body is parsed (#271): a POST
        # to a GET-only route (/api/players is GET-only; player creates go to the
        # v2 setup routes) is 405 + Allow even when its body is malformed JSON —
        # the malformed_json 400 must not mask the real method_not_allowed answer.
        admin = self._admin()
        status, headers, body = self._req(
            admin, "POST", "/api/players", raw=b"{not valid json")
        self.assertEqual(status, 405)
        self.assertEqual(body["error"]["code"], "method_not_allowed")
        allow = {m.strip() for m in headers.get("Allow", "").split(",")}
        self.assertEqual(allow, {"GET", "HEAD", "OPTIONS"})

    def test_malformed_post_on_unknown_route_is_404_not_400(self):
        # A malformed POST to a path that supports no method at all is 404
        # (unknown endpoint), never 400 malformed_json — the route is rejected
        # before the body is ever read.
        admin = self._admin()
        status, _headers, body = self._req(
            admin, "POST", "/api/nope", raw=b"{not valid json")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_malformed_post_on_valid_post_route_is_400(self):
        # Only a route that genuinely accepts POST reaches body parsing, so
        # malformed JSON there is the 400 malformed_json it should be.
        admin = self._admin()
        status, _headers, body = self._req(
            admin, "POST", "/api/accounts", raw=b"{not valid json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "malformed_json")

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
        self.assertEqual(headers.get("Allow"), "GET, HEAD, OPTIONS")
        self.assertEqual(body["error"]["code"], "method_not_allowed")
        self.assertEqual(body["error"]["details"]["allow"], "GET, HEAD, OPTIONS")

    def test_patch_on_players_is_405(self):
        admin = self._admin()
        status, headers, _body = self._req(admin, "PATCH", "/api/players")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "GET, HEAD, OPTIONS")

    def test_delete_on_game_board_is_405_allow_get(self):
        admin = self._admin()
        status, headers, body = self._req(
            admin, "DELETE", f"/api/games/{self.gid}/board")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "GET, HEAD, OPTIONS")
        self.assertEqual(body["error"]["code"], "method_not_allowed")

    def test_put_on_accounts_is_405_allow_get_post(self):
        admin = self._admin()
        status, headers, _body = self._req(admin, "PUT", "/api/accounts")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "GET, HEAD, OPTIONS, POST")

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

    def test_unimplemented_verbs_use_the_json_method_contract(self):
        """Extension verbs cannot bypass routing into stdlib HTML 501s."""
        anon = self._client()
        expected_allow = "GET, HEAD, OPTIONS"
        known_paths = (
            "/api/players",
            "/calendar/team/missing.ics",
        )
        for method in ("TRACE", "PROPFIND"):
            for path in known_paths:
                with self.subTest(method=method, path=path):
                    status, headers, body = self._req(anon, method, path)
                    self.assertEqual(status, 405)
                    self.assertIn("application/json",
                                  headers.get("Content-Type", ""))
                    self.assertEqual(headers.get("Allow"), expected_allow)
                    self.assertEqual(body["error"]["code"],
                                     "method_not_allowed")
                    self.assertEqual(body["error"]["details"]["allow"],
                                     expected_allow)

        # The dynamic verb bridge changes only error format, not existence:
        # static shells, fallthroughs, and lookalikes remain unknown.
        for path in (
            "/setup",
            "/app.js",
            "/api/nope",
            "/api/games/not-a-game/not-an-action",
            "/calendar/nope/missing.ics",
        ):
            with self.subTest(method="TRACE", path=path):
                status, headers, body = self._req(anon, "TRACE", path)
                self.assertEqual(status, 404)
                self.assertIn("application/json",
                              headers.get("Content-Type", ""))
                self.assertNotIn("Allow", headers)
                self.assertEqual(body["error"]["code"], "not_found")

    def test_concrete_non_api_get_routes_publish_the_full_method_contract(self):
        """Known non-API GETs are not unknown merely because of their prefix."""
        anon = self._client()
        paths = (
            "/favicon.ico",
            "/calendar/division/missing.ics",
            "/calendar/official/missing.ics",
            "/calendar/player/missing.ics",
            "/calendar/team/missing.ics",
        )
        expected_allow = "GET, HEAD, OPTIONS"
        for path in paths:
            with self.subTest(path=path, method="OPTIONS"):
                status, headers, body = self._req(anon, "OPTIONS", path)
                self.assertEqual(status, 204)
                self.assertEqual(headers.get("Allow"), expected_allow)
                self.assertEqual(body, {})
            for method in ("PUT", "PATCH", "DELETE"):
                with self.subTest(path=path, method=method):
                    status, headers, body = self._req(anon, method, path)
                    self.assertEqual(status, 405)
                    self.assertEqual(headers.get("Allow"), expected_allow)
                    self.assertEqual(body["error"]["code"],
                                     "method_not_allowed")
                    self.assertEqual(body["error"]["details"]["allow"],
                                     expected_allow)
            with self.subTest(path=path, method="POST-malformed"):
                status, headers, body = self._req(
                    anon, "POST", path, raw=b"{not valid json")
                self.assertEqual(status, 405)
                self.assertEqual(headers.get("Allow"), expected_allow)
                self.assertEqual(body["error"]["code"], "method_not_allowed")

    def test_non_api_get_method_contract_does_not_admit_similar_unknown_paths(self):
        anon = self._client()
        for path in (
            "/favicon.ico/extra",
            "/calendar/nope/missing.ics",
            "/calendar/team/.ics",
            "/calendar/team/missing.txt",
        ):
            with self.subTest(path=path):
                status, _headers, body = self._req(anon, "PUT", path)
                self.assertEqual(status, 404)
                self.assertEqual(body["error"]["code"], "not_found")

    def test_non_api_get_method_contract_ignores_query_string(self):
        """Routing decisions use the path, not optional feed-client queries."""
        anon = self._client()
        path = "/calendar/team/missing.ics?download=1"
        expected_allow = "GET, HEAD, OPTIONS"

        status, headers, body = self._req(anon, "OPTIONS", path)
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Allow"), expected_allow)
        self.assertEqual(body, {})

        status, headers, body = self._req(anon, "PATCH", path)
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), expected_allow)
        self.assertEqual(body["error"]["code"], "method_not_allowed")

        status, headers, body = self._req(
            anon, "POST", path, raw=b"{not valid json")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), expected_allow)
        self.assertEqual(body["error"]["code"], "method_not_allowed")

    def test_head_on_non_api_get_routes_runs_the_real_get(self):
        anon = self._client()
        status, _headers, body = self._req(anon, "HEAD", "/favicon.ico")
        self.assertEqual(status, 204)
        self.assertEqual(body, {})

        # A route-shaped but nonexistent calendar token mirrors GET's 404;
        # method discovery must not turn HEAD into a blind synthetic 200.
        status, _headers, body = self._req(
            anon, "HEAD", "/calendar/team/definitely-missing.ics")
        self.assertEqual(status, 404)
        self.assertEqual(body, {})

        # A valid feed must execute the real bearer-token lookup and preserve
        # GET's representation headers while suppressing only the body.
        created = srv.STATE.api.create_calendar_feed_token(
            "team", self.home, actor_id="user_admin")
        token = created["token"]
        with patch.object(
                srv.STATE.api, "calendar_feed_ics",
                wraps=srv.STATE.api.calendar_feed_ics) as feed:
            status, headers, body = self._req(
                anon, "HEAD", f"/calendar/team/{token}.ics")
        self.assertEqual(status, 200)
        self.assertIn("text/calendar", headers.get("Content-Type", ""))
        self.assertEqual(headers.get("Content-Disposition"),
                         "inline; filename=calendar.ics")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertGreater(int(headers.get("Content-Length", "0")), 0)
        self.assertEqual(body, {})
        feed.assert_called_once_with("team", token)

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

    # -- Blocker 1: strict TYPE validation -----------------------------------
    def test_account_username_wrong_type_is_400_zero_writes(self):
        admin = self._admin()
        store = srv.STATE.api.store
        before = len(store.all_user_accounts())
        status, _headers, body = self._req(
            admin, "POST", "/api/accounts",
            {"username": ["not", "a", "string"], "password": "pw",
             "role": "viewer"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"]["reason"], "wrong_type")
        self.assertEqual(body["error"]["details"]["field"], "username")
        self.assertEqual(len(store.all_user_accounts()), before)

    def test_player_name_wrong_type_is_400(self):
        admin = self._admin()
        status, _headers, body = self._req(
            admin, "POST", "/api/setup/player",
            {"team_id": self.home, "name": {"first": "N"}, "position": "forward"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"]["reason"], "wrong_type")
        self.assertEqual(body["error"]["details"]["field"], "name")

    def test_player_jersey_number_bool_is_wrong_type_zero_writes(self):
        # ``true`` is a JSON bool; even though bool subclasses int it must be
        # rejected for an int-typed field, and nothing is written.
        admin = self._admin()
        store = srv.STATE.api.store
        before = len(store.all_players())
        status, _headers, body = self._req(
            admin, "POST", "/api/setup/player",
            {"team_id": self.home, "name": "Boolplayer", "position": "forward",
             "jersey_number": True})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"]["reason"], "wrong_type")
        self.assertEqual(body["error"]["details"]["field"], "jersey_number")
        self.assertEqual(len(store.all_players()), before)

    def test_player_valid_int_jersey_number_succeeds(self):
        admin = self._admin()
        status, _headers, _body = self._req(
            admin, "POST", "/api/setup/player",
            {"team_id": self.home, "name": "Numbered", "position": "forward",
             "jersey_number": 42})
        self.assertEqual(status, 200)

    # -- Blocker 2: exact setup-route recognition ----------------------------
    def test_delete_on_nonexistent_setup_path_is_404(self):
        admin = self._admin()
        for path in ("/api/setup/nonsense", "/api/setup/player/x/frobnicate"):
            status, _headers, body = self._req(admin, "DELETE", path)
            self.assertEqual(status, 404, path)
            self.assertEqual(body["error"]["code"], "not_found", path)

    def test_put_on_real_setup_path_is_405_with_allow(self):
        admin = self._admin()
        for path in ("/api/setup/player/x/delete", "/api/setup/team"):
            status, headers, body = self._req(admin, "PUT", path)
            self.assertEqual(status, 405, path)
            self.assertEqual(headers.get("Allow"), "OPTIONS, POST", path)
            self.assertEqual(body["error"]["code"], "method_not_allowed", path)

    # -- Blocker 3: strict schemas on lifecycle routes -----------------------
    def test_player_assign_team_unknown_key_is_400_zero_writes(self):
        admin = self._admin()
        store = srv.STATE.api.store
        pid = store.players_for_team(self.home)[0].id
        original_team = store.get_player(pid).team_id
        audits_before = len(store.all_setup_audit())
        status, _headers, body = self._req(
            admin, "POST", f"/api/setup/player/{pid}/assign-team",
            {"team_id": self.home, "surprise": 1})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"]["reason"], "unknown_field")
        self.assertIn("surprise", body["error"]["details"]["fields"])
        # Zero writes / audits: the player's team is unchanged.
        self.assertEqual(store.get_player(pid).team_id, original_team)
        self.assertEqual(len(store.all_setup_audit()), audits_before)

    def test_v2_player_assign_team_unknown_key_is_400(self):
        admin = self._admin()
        store = srv.STATE.api.store
        pid = store.players_for_team(self.home)[0].id
        status, _headers, body = self._req(
            admin, "POST", f"/api/v2/setup/player/{pid}/assign-team",
            {"team_id": self.home, "surprise": 1})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"]["reason"], "unknown_field")

    def test_delete_route_with_unknown_key_is_400_zero_writes(self):
        admin = self._admin()
        store = srv.STATE.api.store
        # A real, deletable entity: create a throwaway club.
        _s, _h, club = self._req(
            admin, "POST", "/api/setup/club", {"name": "Throwaway"})
        club_id = club["id"]
        audits_before = len(store.all_setup_audit())
        status, _headers, body = self._req(
            admin, "POST", f"/api/setup/club/{club_id}/delete",
            {"surprise": 1})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"]["reason"], "unknown_field")
        # Zero writes / audits and the club is still present.
        self.assertIsNotNone(store.get_club(club_id))
        self.assertEqual(len(store.all_setup_audit()), audits_before)

    # -- Blocker 3 (cont.): registration / venue-access lifecycle routes ------
    def test_v1_assign_division_unknown_key_is_400_zero_audits(self):
        # The unknown-key check fires before the service, so an arbitrary
        # registration id still 400s on a stray key with zero audits written.
        admin = self._admin()
        store = srv.STATE.api.store
        audits_before = len(store.all_setup_audit())
        status, _headers, body = self._req(
            admin, "POST",
            "/api/setup/season-team-registration/streg_1/assign-division",
            {"division_id": "division_1", "surprise": 1})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"]["reason"], "unknown_field")
        self.assertIn("surprise", body["error"]["details"]["fields"])
        self.assertEqual(len(store.all_setup_audit()), audits_before)

    def test_v1_registration_remove_unknown_key_row_present(self):
        admin = self._admin()
        store = srv.STATE.api.store
        reg = store.all_season_team_registrations()[0]
        active_before = reg.active
        status, _headers, body = self._req(
            admin, "POST",
            f"/api/setup/season-team-registration/{reg.id}/remove",
            {"surprise": 1})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"]["reason"], "unknown_field")
        # The row is untouched (not deactivated).
        after = next(r for r in store.all_season_team_registrations()
                     if r.id == reg.id)
        self.assertEqual(after.active, active_before)

    def test_v2_registration_delete_unknown_key_row_present(self):
        admin = self._admin()
        store = srv.STATE.api.store
        reg_id = store.all_season_team_registrations()[0].id
        status, _headers, body = self._req(
            admin, "POST",
            f"/api/v2/setup/season-team-registration/{reg_id}/delete",
            {"surprise": 1})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"]["reason"], "unknown_field")
        # The row is still present.
        self.assertIn(reg_id,
                      [r.id for r in store.all_season_team_registrations()])

    def test_v2_venue_access_grant_unknown_key_is_400(self):
        admin = self._admin()
        status, _headers, body = self._req(
            admin, "POST", "/api/v2/setup/seasons/season_1/venue-access",
            {"venue_id": "venue_1", "surprise": 1})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"]["reason"], "unknown_field")

    # -- Blocker 4: HEAD / OPTIONS JSON method contract ----------------------
    def test_head_on_get_path_is_200_no_body(self):
        # HEAD mirrors the (public) GET on /api/health: 200, and no body.
        admin = self._admin()
        status, _headers, body = self._req(admin, "HEAD", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {})  # no body written

    def test_head_mirrors_get_auth_not_blind_200(self):
        # HEAD must run the real GET (auth/authz), NOT a regex-derived 200: an
        # UNAUTHENTICATED HEAD on the operator-gated /api/accounts mirrors the
        # GET's 401/403, never a blind 200.
        anon = self._client()  # never logs in
        status, _headers, _body = self._req(anon, "HEAD", "/api/accounts")
        self.assertNotEqual(status, 200)
        self.assertIn(status, (401, 403))

    def test_head_on_authenticated_get_path_is_200(self):
        admin = self._admin()
        status, _headers, body = self._req(admin, "HEAD", "/api/accounts")
        self.assertEqual(status, 200)
        self.assertEqual(body, {})  # no body written

    def test_head_on_nonexistent_resource_path_is_404(self):
        # A resource-shaped GET path whose record doesn't exist mirrors GET's
        # 404 (not a regex-derived 200).
        admin = self._admin()
        status, _headers, _body = self._req(
            admin, "HEAD", "/api/games/ghost_game/board")
        self.assertEqual(status, 404)

    def test_head_on_post_only_path_is_405_with_allow(self):
        admin = self._admin()
        status, headers, _body = self._req(admin, "HEAD", "/api/auth/login")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "OPTIONS, POST")

    def test_options_on_known_path_is_204_with_allow(self):
        admin = self._admin()
        status, headers, _body = self._req(admin, "OPTIONS", "/api/accounts")
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Allow"), "GET, HEAD, OPTIONS, POST")

    def test_head_on_unknown_api_path_is_404(self):
        admin = self._admin()
        status, _headers, _body = self._req(admin, "HEAD", "/api/nope")
        self.assertEqual(status, 404)

    def test_options_on_unknown_api_path_is_404(self):
        admin = self._admin()
        status, _headers, _body = self._req(admin, "OPTIONS", "/api/nope")
        self.assertEqual(status, 404)

    # -- Blocker 2: cross-method 405 in do_GET / do_POST ---------------------
    def test_get_on_post_only_path_is_405_with_allow(self):
        admin = self._admin()
        status, headers, body = self._req(admin, "GET", "/api/auth/login")
        self.assertEqual(status, 405)
        self.assertEqual(body["error"]["code"], "method_not_allowed")
        allow = headers.get("Allow", "")
        self.assertIn("POST", allow)
        self.assertIn("OPTIONS", allow)

    def test_post_on_get_only_path_is_405_with_allow(self):
        admin = self._admin()
        status, headers, body = self._req(admin, "POST", "/api/players", {})
        self.assertEqual(status, 405)
        self.assertEqual(body["error"]["code"], "method_not_allowed")
        allow = headers.get("Allow", "")
        for m in ("GET", "HEAD", "OPTIONS"):
            self.assertIn(m, allow)

    def test_get_on_unknown_api_path_is_404(self):
        admin = self._admin()
        status, _headers, body = self._req(admin, "GET", "/api/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    # -- Blocker 1: nullable vs non-nullable lifecycle schemas ---------------
    def test_player_assign_team_missing_id_is_field_required_zero_writes(self):
        admin = self._admin()
        store = srv.STATE.api.store
        pid = store.players_for_team(self.home)[0].id
        original = store.get_player(pid).team_id
        audits_before = len(store.all_setup_audit())
        status, _headers, body = self._req(
            admin, "POST", f"/api/setup/player/{pid}/assign-team", {})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"]["reason"], "field_required")
        self.assertEqual(body["error"]["details"]["field"], "team_id")
        self.assertEqual(store.get_player(pid).team_id, original)
        self.assertEqual(len(store.all_setup_audit()), audits_before)

    def test_player_assign_team_id_wrong_type_is_wrong_type_zero_writes(self):
        admin = self._admin()
        store = srv.STATE.api.store
        pid = store.players_for_team(self.home)[0].id
        original = store.get_player(pid).team_id
        audits_before = len(store.all_setup_audit())
        status, _headers, body = self._req(
            admin, "POST", f"/api/setup/player/{pid}/assign-team",
            {"team_id": ["array", "not", "id"]})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"]["reason"], "wrong_type")
        self.assertEqual(body["error"]["details"]["field"], "team_id")
        self.assertEqual(store.get_player(pid).team_id, original)
        self.assertEqual(len(store.all_setup_audit()), audits_before)

    def test_v2_player_assign_team_missing_id_is_field_required(self):
        admin = self._admin()
        store = srv.STATE.api.store
        pid = store.players_for_team(self.home)[0].id
        status, _headers, body = self._req(
            admin, "POST", f"/api/v2/setup/player/{pid}/assign-team", {})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"]["reason"], "field_required")
        self.assertEqual(body["error"]["details"]["field"], "team_id")

    def test_nullable_assign_club_empty_body_is_field_required(self):
        # A nullable relation still requires the key PRESENT: `{}` can't silently
        # unassign the team's club.
        admin = self._admin()
        store = srv.STATE.api.store
        # A real team to reassign.
        team_id = self.home
        status, _headers, body = self._req(
            admin, "POST", f"/api/setup/team/{team_id}/assign-club", {})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"]["reason"], "field_required")
        self.assertEqual(body["error"]["details"]["field"], "club_id")

    def test_nullable_assign_club_explicit_null_unassigns(self):
        # An explicit {"club_id": null} is the sanctioned way to unassign.
        admin = self._admin()
        store = srv.STATE.api.store
        team_id = self.home
        status, _headers, body = self._req(
            admin, "POST", f"/api/setup/team/{team_id}/assign-club",
            {"club_id": None})
        self.assertEqual(status, 200)
        self.assertIsNone(store.get_team(team_id).club_id)

    def test_nullable_assign_club_wrong_type_is_wrong_type(self):
        admin = self._admin()
        status, _headers, body = self._req(
            admin, "POST", f"/api/setup/team/{self.home}/assign-club",
            {"club_id": {"an": "object"}})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"]["reason"], "wrong_type")

    def test_v2_assign_division_empty_body_is_field_required(self):
        # v2 season-team assign-division is nullable (clearing preserves the
        # league) — `{}` still can't silently unassign.
        admin = self._admin()
        status, _headers, body = self._req(
            admin, "POST",
            "/api/v2/setup/season-team-registration/streg_1/assign-division", {})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"]["reason"], "field_required")
        self.assertEqual(body["error"]["details"]["field"], "division_id")


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
