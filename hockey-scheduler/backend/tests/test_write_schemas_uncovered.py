"""Strict write schemas on the routes #271 did not reach (#202).

#271 pinned the highest-risk write surface (see ``test_write_schemas.py``), but
it covered exactly one availability route — ``/api/games/<id>/availability``.
Three sibling write routes still built their kwargs with bare ``body.get(...)``,
so an unknown or typo'd key was silently dropped and the write went through
anyway. Each of the cases below is a real defect, not a style point:

* ``POST /api/officials/<id>/availability`` — a ``note`` → ``notes`` typo wrote
  the window with ``note=None`` (the operator's note vanished), and a ``status``
  → ``staus`` typo refused only by accident, with the internal ``None`` leaked
  into the operator-facing message ("Unknown availability status 'None'.") and
  no ``details`` block naming a field.
* ``POST /api/me/guardian/<junior>/games/<game>/availability`` — the route
  defaulted a missing ``availability_status`` to ``"pending"``, so a guardian
  declaring their junior UNAVAILABLE with a typo'd key was silently recorded as
  PENDING at 200. A silent WRONG write, not a dropped one.
* ``POST /api/accounts/<id>/active`` — ``bool(body.get("active"))`` collapses a
  missing key to ``False``, so ``{"activ": false}`` DEACTIVATED the account and
  revoked its sessions at 200. Its sibling ``/api/accounts/<id>/scope`` dropped
  stray keys, and reported a misplaced top-level ``team_id`` as the misleading
  ``scope_required`` rather than naming the unknown field.

Every refusal here must also be a ZERO-WRITE refusal: the assertions pin the
persisted row, not just the status code.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.web import server as srv


class _HttpBase(unittest.TestCase):
    """Shared real-boundary harness: a live ThreadingHTTPServer + cookies."""

    @classmethod
    def setUpClass(cls):
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _req(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _login(self, username, password="demo"):
        c = self._client()
        self._req(c, "POST", "/api/auth/login",
                  {"username": username, "password": password})
        return c


# --------------------------------------------------------------------------- #
# POST /api/officials/<id>/availability                                       #
# --------------------------------------------------------------------------- #
class OfficialAvailabilityWriteSchemaTest(_HttpBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ref = srv.STATE.ids["referee_id"]

    def _windows(self):
        return srv.STATE.api.store.availability_for_official(self.ref)

    def _window(self, **overrides):
        body = {"start_time": "2027-04-01T18:00:00Z",
                "end_time": "2027-04-01T21:00:00Z",
                "status": "available"}
        body.update(overrides)
        return body

    def test_clean_official_availability_write_still_succeeds(self):
        # Control: the sanctioned shape (including the optional note) is
        # unaffected by the strict schema.
        admin = self._login("admin")
        before = len(self._windows())
        status, body = self._req(
            admin, "POST", f"/api/officials/{self.ref}/availability",
            self._window(note="Reffing the early slot"))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "available")
        self.assertEqual(body["note"], "Reffing the early slot")
        self.assertEqual(len(self._windows()), before + 1)

    def test_unknown_key_on_official_availability_is_rejected_zero_writes(self):
        admin = self._login("admin")
        before = len(self._windows())
        status, body = self._req(
            admin, "POST", f"/api/officials/{self.ref}/availability",
            self._window(surprise=1))
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["code"], "validation_error")
        self.assertEqual(body["error"]["details"]["reason"], "unknown_field")
        self.assertIn("surprise", body["error"]["details"]["fields"])
        # The stray key must not be shrugged off and the window written anyway.
        self.assertEqual(len(self._windows()), before)

    def test_note_typo_on_official_availability_is_rejected_not_dropped(self):
        # `notes` (plural) used to be dropped and the window written with
        # note=None — the operator's note silently lost at 200.
        admin = self._login("admin")
        before = len(self._windows())
        status, body = self._req(
            admin, "POST", f"/api/officials/{self.ref}/availability",
            self._window(notes="typo'd note key"))
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["details"]["reason"], "unknown_field")
        self.assertIn("notes", body["error"]["details"]["fields"])
        self.assertEqual(len(self._windows()), before)

    def test_status_typo_on_official_availability_names_the_unknown_field(self):
        # `staus` used to refuse only BY ACCIDENT: the key went missing, the
        # enum lookup then saw None, and the operator got the shapeless
        # "Unknown availability status 'None'." with no details and an internal
        # None leaked into the message. It must name the typo instead.
        admin = self._login("admin")
        before = len(self._windows())
        body_in = self._window()
        body_in.pop("status")
        body_in["staus"] = "unavailable"
        status, body = self._req(
            admin, "POST", f"/api/officials/{self.ref}/availability", body_in)
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["details"]["reason"], "unknown_field")
        self.assertIn("staus", body["error"]["details"]["fields"])
        self.assertNotIn("None", body["error"]["message"])
        self.assertEqual(len(self._windows()), before)

    def test_missing_status_on_official_availability_names_the_field(self):
        # A genuinely absent status is a named required field, not an enum
        # accident that prints the internal None.
        admin = self._login("admin")
        before = len(self._windows())
        body_in = self._window()
        body_in.pop("status")
        status, body = self._req(
            admin, "POST", f"/api/officials/{self.ref}/availability", body_in)
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["details"]["reason"], "field_required")
        self.assertEqual(body["error"]["details"]["field"], "status")
        self.assertNotIn("None", body["error"]["message"])
        self.assertEqual(len(self._windows()), before)

    def test_missing_start_time_on_official_availability_names_the_field(self):
        admin = self._login("admin")
        before = len(self._windows())
        body_in = self._window()
        body_in.pop("start_time")
        status, body = self._req(
            admin, "POST", f"/api/officials/{self.ref}/availability", body_in)
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["details"]["reason"], "field_required")
        self.assertEqual(body["error"]["details"]["field"], "start_time")
        self.assertEqual(len(self._windows()), before)

    def test_non_string_status_on_official_availability_is_wrong_type(self):
        admin = self._login("admin")
        before = len(self._windows())
        status, body = self._req(
            admin, "POST", f"/api/officials/{self.ref}/availability",
            self._window(status=7))
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["details"]["reason"], "wrong_type")
        self.assertEqual(body["error"]["details"]["field"], "status")
        self.assertEqual(len(self._windows()), before)

    def test_forged_actor_id_on_official_availability_is_rejected(self):
        # actor_id is server-resolved (#88). It was previously accepted in the
        # body and ignored; now it is refused outright as an unknown field, so
        # a caller can never believe it was honoured.
        admin = self._login("admin")
        before = len(self._windows())
        status, body = self._req(
            admin, "POST", f"/api/officials/{self.ref}/availability",
            self._window(actor_id="attacker"))
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["details"]["reason"], "unknown_field")
        self.assertIn("actor_id", body["error"]["details"]["fields"])
        self.assertEqual(len(self._windows()), before)

    def test_schema_refusal_is_not_an_existence_oracle(self):
        # A body fault on a nonexistent official must look exactly like a body
        # fault on a real one, so the refusal cannot be used to enumerate
        # officials. (Both are refused before any lookup.)
        admin = self._login("admin")
        real = self._req(admin, "POST",
                         f"/api/officials/{self.ref}/availability",
                         self._window(surprise=1))
        ghost = self._req(admin, "POST",
                          "/api/officials/official_does_not_exist/availability",
                          self._window(surprise=1))
        self.assertEqual(real, ghost)


# --------------------------------------------------------------------------- #
# POST /api/me/guardian/<junior>/games/<game>/availability                     #
# --------------------------------------------------------------------------- #
class GuardianAvailabilityWriteSchemaTest(_HttpBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.junior = srv.STATE.ids["selected_player_id"]
        cls.gid = srv.STATE.ids["game_id"]

    @property
    def _path(self):
        return (f"/api/me/guardian/{self.junior}/games/{self.gid}"
                f"/availability")

    def _recorded(self):
        av = srv.STATE.api.store.availability_for_player(self.gid, self.junior)
        return None if av is None else av.availability_status.value

    def test_clean_guardian_write_still_succeeds(self):
        guardian = self._login("guardian")
        status, body = self._req(
            guardian, "POST", self._path,
            {"availability_status": "unavailable"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["availability_status"], "unavailable")
        self.assertEqual(body["response_source"], "guardian")
        self.assertEqual(self._recorded(), "unavailable")

    def test_typo_key_is_rejected_not_silently_recorded_as_pending(self):
        # The sharpest case: a guardian declaring the junior UNAVAILABLE with a
        # typo'd key used to get 200 and a PENDING row. The declaration must be
        # refused loudly and change nothing.
        guardian = self._login("guardian")
        self._req(guardian, "POST", self._path,
                  {"availability_status": "available"})  # known starting state
        before = self._recorded()
        self.assertEqual(before, "available")
        status, body = self._req(
            guardian, "POST", self._path,
            {"availabilty_status": "unavailable"})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["code"], "validation_error")
        self.assertEqual(body["error"]["details"]["reason"], "unknown_field")
        self.assertIn("availabilty_status", body["error"]["details"]["fields"])
        # Not overwritten with the hardcoded "pending" default.
        self.assertEqual(self._recorded(), "available")

    def test_stray_key_alongside_a_valid_status_is_rejected(self):
        guardian = self._login("guardian")
        self._req(guardian, "POST", self._path,
                  {"availability_status": "available"})
        status, body = self._req(
            guardian, "POST", self._path,
            {"availability_status": "unavailable", "surprise": 1})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["details"]["reason"], "unknown_field")
        self.assertIn("surprise", body["error"]["details"]["fields"])
        self.assertEqual(self._recorded(), "available")

    def test_empty_body_names_the_required_status_field(self):
        # An empty body used to write "pending" — a real attendance record the
        # guardian never asked for. It must name the missing field instead.
        guardian = self._login("guardian")
        self._req(guardian, "POST", self._path,
                  {"availability_status": "available"})
        status, body = self._req(guardian, "POST", self._path, {})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["details"]["reason"], "field_required")
        self.assertEqual(body["error"]["details"]["field"],
                         "availability_status")
        self.assertEqual(self._recorded(), "available")

    def test_link_gate_still_decides_before_the_body_schema(self):
        # An unlinked junior stays a 403 even with a perfectly-shaped body AND
        # with a malformed one: the body schema must not become a way to tell
        # "wrong shape" from "not your junior".
        guardian = self._login("guardian")
        stranger = srv.STATE.ids["substitute_player_id"]
        path = (f"/api/me/guardian/{stranger}/games/{self.gid}/availability")
        status, body = self._req(
            guardian, "POST", path, {"availability_status": "available"})
        self.assertEqual(status, 403, body)
        status2, body2 = self._req(guardian, "POST", path, {"surprise": 1})
        self.assertEqual(status2, 403, body2)
        self.assertEqual(body, body2)


# --------------------------------------------------------------------------- #
# POST /api/accounts/<id>/active  and  /api/accounts/<id>/scope                #
# --------------------------------------------------------------------------- #
class AccountWriteSchemaTest(_HttpBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.home = srv.STATE.ids["home_team_id"]
        cls.away = srv.STATE.ids["away_team_id"]

    def _make_coach(self, username):
        admin = self._login("admin")
        status, body = self._req(
            admin, "POST", "/api/accounts",
            {"username": username, "password": "Probe-pw-123456",
             "role": "coach", "scope": {"team_id": self.home}})
        self.assertEqual(status, 200, body)
        return admin, body["id"]

    def _account(self, acct_id):
        return srv.STATE.api.store.get_user_account(acct_id)

    # -- /active ------------------------------------------------------------
    def test_clean_deactivation_still_succeeds(self):
        admin, acct_id = self._make_coach("wsu_active_control")
        status, body = self._req(
            admin, "POST", f"/api/accounts/{acct_id}/active", {"active": False})
        self.assertEqual(status, 200, body)
        self.assertFalse(body["active"])
        self.assertFalse(self._account(acct_id).active)

    def test_typo_key_on_active_is_rejected_not_a_silent_deactivation(self):
        # bool(body.get("active")) collapsed a missing key to False, so a single
        # typo'd key DEACTIVATED the account and revoked its live sessions.
        admin, acct_id = self._make_coach("wsu_active_typo")
        self.assertTrue(self._account(acct_id).active)
        status, body = self._req(
            admin, "POST", f"/api/accounts/{acct_id}/active", {"activ": False})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["code"], "validation_error")
        self.assertEqual(body["error"]["details"]["reason"], "unknown_field")
        self.assertIn("activ", body["error"]["details"]["fields"])
        self.assertTrue(self._account(acct_id).active)

    def test_typo_key_on_active_does_not_revoke_a_live_session(self):
        # The destructive half of the same defect: the silent deactivation also
        # killed the account's live session.
        admin, acct_id = self._make_coach("wsu_active_session")
        victim = self._login("wsu_active_session", "Probe-pw-123456")
        status, _ = self._req(victim, "GET", "/api/auth/me")
        self.assertEqual(status, 200)
        self._req(admin, "POST", f"/api/accounts/{acct_id}/active",
                  {"activ": False})
        status, me = self._req(victim, "GET", "/api/auth/me")
        self.assertEqual(status, 200, me)  # session survives a refused write

    def test_missing_active_names_the_field(self):
        admin, acct_id = self._make_coach("wsu_active_missing")
        status, body = self._req(
            admin, "POST", f"/api/accounts/{acct_id}/active", {})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["details"]["reason"], "field_required")
        self.assertEqual(body["error"]["details"]["field"], "active")
        self.assertTrue(self._account(acct_id).active)

    def test_null_active_names_the_field(self):
        admin, acct_id = self._make_coach("wsu_active_null")
        status, body = self._req(
            admin, "POST", f"/api/accounts/{acct_id}/active", {"active": None})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["details"]["reason"], "field_required")
        self.assertEqual(body["error"]["details"]["field"], "active")
        self.assertTrue(self._account(acct_id).active)

    def test_string_active_is_wrong_type_not_a_truthy_coercion(self):
        # "false" is a truthy string: bool("false") is True, so a JSON-typed
        # client bug used to ACTIVATE where it meant to deactivate.
        admin, acct_id = self._make_coach("wsu_active_string")
        status, body = self._req(
            admin, "POST", f"/api/accounts/{acct_id}/active",
            {"active": "false"})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["details"]["reason"], "wrong_type")
        self.assertEqual(body["error"]["details"]["field"], "active")
        self.assertTrue(self._account(acct_id).active)

    # -- /scope -------------------------------------------------------------
    def test_clean_rebind_still_succeeds(self):
        admin, acct_id = self._make_coach("wsu_scope_control")
        status, body = self._req(
            admin, "POST", f"/api/accounts/{acct_id}/scope",
            {"scope": {"team_id": self.away}})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["scope"], {"team_id": self.away})
        self.assertEqual(self._account(acct_id).scope, {"team_id": self.away})

    def test_stray_key_on_scope_is_rejected_zero_writes(self):
        admin, acct_id = self._make_coach("wsu_scope_stray")
        status, body = self._req(
            admin, "POST", f"/api/accounts/{acct_id}/scope",
            {"scope": {"team_id": self.away}, "surprise": 1})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["details"]["reason"], "unknown_field")
        self.assertIn("surprise", body["error"]["details"]["fields"])
        self.assertEqual(self._account(acct_id).scope, {"team_id": self.home})

    def test_misplaced_top_level_team_id_on_scope_names_the_unknown_field(self):
        # Same misplacement as the account-create defect (#266): the binding
        # must travel inside `scope`. It already failed closed, but reported the
        # misleading `scope_required` instead of naming the misplaced key.
        admin, acct_id = self._make_coach("wsu_scope_misplaced")
        status, body = self._req(
            admin, "POST", f"/api/accounts/{acct_id}/scope",
            {"team_id": self.away})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["details"]["reason"], "unknown_field")
        self.assertIn("team_id", body["error"]["details"]["fields"])
        self.assertEqual(self._account(acct_id).scope, {"team_id": self.home})


if __name__ == "__main__":
    unittest.main()
