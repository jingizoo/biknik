"""#426 review finding 1, the REAL-HTTP half: GET /api/notifications/contacts
and POST /api/notifications/contacts/<id>/active through a genuine
``ThreadingHTTPServer`` running the real ``Handler``, driven with real
session cookies — proving the WIRED contract (role/user_id/request_id
actually propagated end to end), not a facade call that merely resembles
it. The retired bug: the route resolved the caller's real session, then
called the facade with NO principal at all, so a signed-in Admin's read
was attributed to the transitional "operator_boundary" label and a
Viewer's 403 (decided by a separate, now-removed transport gate) left no
audit row whatsoever.

Same body runs on Memory and file-backed SQLite (``SensitiveReadHttpContract``
subclasses), because a live PostgreSQL job already exercises this exact
HTTP surface via test_sensitive_read_audit.py's tri-store facade tests and
CI's separate postgres job; this file's job is proving the WIRING, which is
backend-independent.

Each test tracks its OWN "how many DataAccessLog rows existed before this
request" checkpoint rather than wiping the store between cases — a wipe
(``clear_all_data``) would also delete the six seeded persona accounts
every login in this file depends on.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401

import hockey_scheduler.web.server as srv
from hockey_scheduler.domain import ACCESS_ALLOWED, ACCESS_DENIED
from hockey_scheduler.services import visibility_policy as vp
from hockey_scheduler.web.server import STATE, Handler

PASSWORD = "demo"
SENTINEL_EMAIL = "sentinel-http-leak-probe@leak-probe.invalid"

# role -> demo persona username, per the six real seeded accounts every
# other HTTP test file in this suite logs in as.
PERSONA = {
    "league_admin": "admin",
    "arena_manager": "arena",
    "coach": "coach",
    "viewer": "viewer",
}
# The two roles CONTACT_DESTINATION grants RAW to (services/visibility_policy.py).
AUTHORIZED = ("league_admin", "arena_manager")
UNAUTHORIZED = ("coach", "viewer")


class SensitiveReadHttpContract:
    """Shared body; subclasses supply the backing store."""

    def database_url(self):
        raise NotImplementedError

    def setUp(self):
        self._prev_db = os.environ.get("DATABASE_URL")
        self._tmp_path = None
        url = self.database_url()
        if url:
            os.environ["DATABASE_URL"] = url
        else:
            os.environ.pop("DATABASE_URL", None)
        self.addCleanup(self._restore_environment)
        STATE.reset()
        srv.RATE_LIMITER.reset()
        srv.LOGIN_THROTTLE.reset()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.api = STATE.api
        self.store = self.api.store
        self.cid = self.api.set_contact_destination(
            "scheduler", "email", SENTINEL_EMAIL, label="Ops")["id"]
        # A real, id-carrying Official (the toggle route requires a
        # player:/official:-scoped row) — STATE.reset() already seeded the
        # full demo scenario (build_full_demo_store), so pick one of ITS
        # officials rather than seeding a second, colliding one.
        official = self.store.all_officials()[0]
        self.player_cid = self.api.set_contact_destination(
            f"official:{official.id}", "email", SENTINEL_EMAIL,
            label="Ref")["id"]

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()

    def _restore_environment(self):
        if self._prev_db is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self._prev_db
        try:
            STATE.reset()
        except Exception:
            pass
        if self._tmp_path:
            try:
                os.remove(self._tmp_path)
            except OSError:
                pass

    def _req(self, method, path, body=None, opener=None, cookie=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if cookie is not None:
            req.add_header("Cookie", cookie)
        op = opener or urllib.request.build_opener()
        try:
            with op.open(req) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {}), dict(r.headers)
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, (json.loads(raw) if raw else {}), dict(e.headers)

    def _login(self, username):
        op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        status, body, _ = self._req(
            "POST", "/api/auth/login",
            {"username": username, "password": PASSWORD}, opener=op)
        self.assertEqual(status, 200, body)
        return op

    def _rows(self):
        return self.store.list_data_access()

    # -- allowed: Admin, Arena Manager --------------------------------------
    def test_authorized_roles_get_real_reads_with_exact_attribution(self):
        for role in AUTHORIZED:
            with self.subTest(role=role):
                before = len(self._rows())
                op = self._login(PERSONA[role])
                status, body, _ = self._req(
                    "GET", "/api/notifications/contacts", opener=op)
                self.assertEqual(status, 200, body)
                self.assertIn(SENTINEL_EMAIL,
                              [c["destination"] for c in body["contacts"]])
                new_rows = self._rows()[before:]
                scheduler_rows = [r for r in new_rows
                                  if r.subject_id == "scheduler"]
                self.assertEqual(len(scheduler_rows), 1, new_rows)
                row = scheduler_rows[0]
                self.assertEqual(row.outcome, ACCESS_ALLOWED)
                self.assertEqual(row.actor_role, role)
                self.assertIsNotNone(row.actor_user_id)
                # Never the retired no-principal label.
                self.assertNotEqual(row.actor_role, vp.NO_PRINCIPAL)
                self.assertNotEqual(row.actor_role, "operator_boundary")

    # -- refused, with disclosure zero and a denial record ------------------
    def test_unauthorized_signed_in_roles_get_zero_disclosure_and_a_denial(self):
        for role in UNAUTHORIZED:
            with self.subTest(role=role):
                before = len(self._rows())
                op = self._login(PERSONA[role])
                status, body, _ = self._req(
                    "GET", "/api/notifications/contacts", opener=op)
                self.assertEqual(status, 403, body)
                self.assertNotIn(SENTINEL_EMAIL, json.dumps(body))
                self.assertNotIn("contacts", body)
                new_rows = self._rows()[before:]
                self.assertEqual(len(new_rows), 1, new_rows)
                self.assertEqual(new_rows[0].outcome, ACCESS_DENIED)
                self.assertEqual(new_rows[0].actor_role, role)
                self.assertEqual(new_rows[0].subject_id, "*")

    def test_public_no_session_gets_401_and_a_durable_denial(self):
        before = len(self._rows())
        status, body, _ = self._req("GET", "/api/notifications/contacts")
        self.assertEqual(status, 401, body)
        self.assertNotIn(SENTINEL_EMAIL, json.dumps(body))
        new_rows = self._rows()[before:]
        self.assertEqual(len(new_rows), 1, new_rows)
        self.assertEqual(new_rows[0].outcome, ACCESS_DENIED)
        self.assertEqual(new_rows[0].actor_role, vp.NO_PRINCIPAL)
        self.assertIsNone(new_rows[0].actor_user_id)

    def test_invalid_session_cookie_gets_401_and_a_durable_denial(self):
        before = len(self._rows())
        status, body, _ = self._req(
            "GET", "/api/notifications/contacts",
            cookie=f"{srv.SESSION_COOKIE}=totally-bogus-session-token")
        self.assertEqual(status, 401, body)
        new_rows = self._rows()[before:]
        self.assertEqual(len(new_rows), 1, new_rows)
        self.assertEqual(new_rows[0].outcome, ACCESS_DENIED)
        self.assertEqual(new_rows[0].actor_role, vp.NO_PRINCIPAL)

    def test_one_request_one_correlation_id_distinct_across_requests(self):
        before = len(self._rows())
        op_admin = self._login("admin")
        op_coach = self._login("coach")
        self._req("GET", "/api/notifications/contacts", opener=op_admin)
        self._req("GET", "/api/notifications/contacts", opener=op_coach)
        new_rows = self._rows()[before:]
        # Admin's allowed read discloses 2 subjects (scheduler + the
        # seeded official) sharing ONE id; Coach's refusal is 1 more row
        # with its OWN id — 3 rows, 2 distinct correlation ids.
        self.assertEqual(len(new_rows), 3, new_rows)
        ids = [r.request_id for r in new_rows]
        self.assertEqual(len(set(ids)), 2)  # distinct per REQUEST, not per row
        for rid in ids:
            self.assertTrue(rid.startswith("req_"))
        admin_ids = {r.request_id for r in new_rows if r.outcome == ACCESS_ALLOWED}
        coach_ids = {r.request_id for r in new_rows if r.outcome == ACCESS_DENIED}
        self.assertEqual(len(admin_ids), 1)
        self.assertEqual(len(coach_ids), 1)
        self.assertNotEqual(admin_ids, coach_ids)

    def test_head_request_is_gated_the_same_as_get(self):
        # do_HEAD reuses do_GET's dispatch (BaseHTTPRequestHandler
        # convention: identical headers/status, no body) — so it must be
        # refused/audited identically for an unauthorized caller.
        before = len(self._rows())
        op = self._login("coach")
        status, _body, _headers = self._req(
            "HEAD", "/api/notifications/contacts", opener=op)
        self.assertEqual(status, 403)
        new_rows = self._rows()[before:]
        self.assertEqual(len(new_rows), 1, new_rows)
        self.assertEqual(new_rows[0].outcome, ACCESS_DENIED)

    # -- active-toggle route: real actor propagation + audit ----------------
    def test_toggle_propagates_the_real_signed_in_actor(self):
        before = len(self._rows())
        op = self._login("admin")
        status, body, _ = self._req(
            "POST", f"/api/notifications/contacts/{self.player_cid}/active",
            {"active": False}, opener=op)
        self.assertEqual(status, 200, body)
        self.assertFalse(body["active"])
        new_rows = self._rows()[before:]
        self.assertEqual(len(new_rows), 1, new_rows)
        self.assertEqual(new_rows[0].outcome, ACCESS_ALLOWED)
        self.assertEqual(new_rows[0].actor_role, "league_admin")
        self.assertIsNotNone(new_rows[0].actor_user_id)
        self.assertNotEqual(new_rows[0].actor_role, "operator_boundary")

    def test_toggle_refused_for_coach_leaves_row_untouched(self):
        # KNOWN RESIDUAL GAP (see PR body): a Coach is refused here by the
        # GENERIC authorize() gate (MANAGE_SETUP, before this route's own
        # code — including the facade's privacy gate — ever runs), so this
        # specific refusal does NOT durably audit a CONTACT_DESTINATION
        # denial row the way GET /api/notifications/contacts now does. The
        # security property this test exists to pin — zero disclosure, the
        # row genuinely untouched — still holds; only the audit-trail
        # completeness for this one transport-level refusal shape does not.
        before = len(self._rows())
        op = self._login("coach")
        status, body, _ = self._req(
            "POST", f"/api/notifications/contacts/{self.player_cid}/active",
            {"active": False}, opener=op)
        self.assertEqual(status, 403, body)
        c = next(c for c in self.store.all_contact_destinations()
                if c.id == self.player_cid)
        self.assertTrue(c.active)
        self.assertEqual(self._rows()[before:], [])


class MemorySensitiveReadHttpTest(SensitiveReadHttpContract, unittest.TestCase):
    def database_url(self):
        return None


class SqliteSensitiveReadHttpTest(SensitiveReadHttpContract, unittest.TestCase):
    def database_url(self):
        fd, self._tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        return self._tmp_path


if __name__ == "__main__":
    unittest.main()
