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
from hockey_scheduler.domain import (
    ACCESS_ALLOWED,
    ACCESS_DENIED,
    NotificationChannel,
    NotificationDelivery,
)
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
        self.official_ref = f"official:{official.id}"
        self.player_cid = self.api.set_contact_destination(
            self.official_ref, "email", SENTINEL_EMAIL,
            label="Ref")["id"]
        self._delivery_seq = 0

    def _fresh_delivery_id(self):
        # retry/ignore MUTATE the row's status, so each test (and each
        # role within a test) that exercises one needs its OWN untouched
        # row — this seeds a brand new one, addressed to the same real
        # official the toggle route above uses, directly (#426 round-2
        # review finding 2's retry/ignore routes need no Notification row
        # to exist, only the NotificationDelivery itself; see
        # retry_notification_delivery/ignore_notification_delivery's own
        # store.get_notification_delivery(delivery_id) lookup).
        self._delivery_seq += 1
        d = self.store.add_notification_delivery(NotificationDelivery(
            id=self.store.next_id("notif_delivery"),
            notification_id=self.store.next_id("notif"),
            channel=NotificationChannel.EMAIL,
            recipient_ref=self.official_ref,
            destination=SENTINEL_EMAIL))
        return d.id

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

    # -- active-toggle / retry / ignore: real actor propagation + audit -----
    # (#426 round-2 review finding 2). Three routes, each gated by
    # do_POST's GENERIC authorize(role, path) transport gate BEFORE their
    # own facade-level privacy gate ever runs — required_permission()
    # differs per route (web/authz.py), which changes WHO is denied at
    # the transport layer, not merely whether: the contacts-active-toggle
    # route requires the League-Admin-only MANAGE_SETUP (so an Arena
    # Manager — who DOES hold RAW CONTACT_DESTINATION at the facade
    # privacy layer — is STILL denied here, never reaching that facade
    # check at all), while delivery retry/ignore require MANAGE_SCHEDULE,
    # which both League Admin and Arena Manager hold.
    #
    # `_route_fixtures()` maps each route to (``prepare``, request body,
    # allowed personas, denied-at-transport personas). ``prepare()`` builds
    # a FRESH target (a fresh delivery row for retry/ignore; the shared
    # toggle contact for toggle — its own idempotent {"active": False}
    # body means re-toggling an ALREADY-inactive row from an earlier
    # allowed subTest is harmless and still leaves "still inactive" true)
    # and returns ``(path, check_unchanged)`` — a path to POST to, and a
    # zero-arg assertion to call AFTER the request proving nothing about
    # the target moved. Building both together, atomically, right before
    # each request is what keeps retry/ignore's per-request fresh row from
    # ever being confused with a DIFFERENT subTest's row.

    def _toggle_prepare(self):
        path = self._toggle_path()
        before = next(c for c in self.store.all_contact_destinations()
                     if c.id == self.player_cid).active

        def check_unchanged():
            still = next(c for c in self.store.all_contact_destinations()
                        if c.id == self.player_cid)
            self.assertEqual(still.active, before)
        return path, check_unchanged

    def _toggle_path(self):
        return f"/api/notifications/contacts/{self.player_cid}/active"

    def _delivery_prepare(self, action):
        did = self._fresh_delivery_id()
        path = f"/api/notifications/deliveries/{did}/{action}"

        def check_unchanged():
            d = self.store.get_notification_delivery(did)
            self.assertEqual(d.status.value, "pending")
        return path, check_unchanged

    def _route_fixtures(self):
        return {
            "toggle": (self._toggle_prepare, {"active": False},
                      ("league_admin",), ("arena_manager", "coach", "viewer")),
            "retry": (lambda: self._delivery_prepare("retry"), {},
                     ("league_admin", "arena_manager"), ("coach", "viewer")),
            "ignore": (lambda: self._delivery_prepare("ignore"), {},
                      ("league_admin", "arena_manager"), ("coach", "viewer")),
        }

    def test_toggle_propagates_the_real_signed_in_actor(self):
        before = len(self._rows())
        op = self._login("admin")
        status, body, _ = self._req(
            "POST", self._toggle_path(), {"active": False}, opener=op)
        self.assertEqual(status, 200, body)
        self.assertFalse(body["active"])
        new_rows = self._rows()[before:]
        self.assertEqual(len(new_rows), 1, new_rows)
        self.assertEqual(new_rows[0].outcome, ACCESS_ALLOWED)
        self.assertEqual(new_rows[0].actor_role, "league_admin")
        self.assertIsNotNone(new_rows[0].actor_user_id)
        self.assertNotEqual(new_rows[0].actor_role, "operator_boundary")

    def test_allowed_personas_get_normal_behavior_for_every_route(self):
        # The fix is additive to the DENIAL paths only — every route's
        # OWN allowed behavior (status, response shape, its facade-level
        # audit row) must be byte-for-byte what it was before this round.
        for name, (prepare, body, allowed, _denied) in (
                self._route_fixtures().items()):
            for role in allowed:
                with self.subTest(route=name, role=role):
                    path, _check_unchanged = prepare()
                    before = len(self._rows())
                    op = self._login(PERSONA[role])
                    status, resp, _ = self._req(
                        "POST", path, body, opener=op)
                    self.assertEqual(status, 200, resp)
                    self.assertNotIn("error", resp, resp)
                    new_rows = self._rows()[before:]
                    self.assertEqual(len(new_rows), 1, new_rows)
                    self.assertEqual(new_rows[0].outcome, ACCESS_ALLOWED)
                    self.assertEqual(new_rows[0].actor_role, role)

    def test_denied_personas_get_zero_disclosure_and_exactly_one_denial(self):
        # The core proof for finding 2: a signed-in but insufficiently
        # privileged caller refused at do_POST's GENERIC transport gate —
        # for EVERY denied persona on EVERY one of the three routes — now
        # gets zero mutation/disclosure AND exactly one durable
        # CONTACT_DESTINATION denial row, attributed to their real
        # actor/role, with a real request_id. Was previously a silently
        # unaudited gap (contacts toggle) or entirely untested at all
        # (delivery retry/ignore).
        for name, (prepare, body, _allowed, denied) in (
                self._route_fixtures().items()):
            for role in denied:
                with self.subTest(route=name, role=role):
                    path, check_unchanged = prepare()
                    before = len(self._rows())
                    op = self._login(PERSONA[role])
                    status, resp, _ = self._req(
                        "POST", path, body, opener=op)
                    self.assertEqual(status, 403, resp)
                    self.assertNotIn(SENTINEL_EMAIL, json.dumps(resp))
                    check_unchanged()
                    new_rows = self._rows()[before:]
                    self.assertEqual(len(new_rows), 1, new_rows)
                    row = new_rows[0]
                    self.assertEqual(row.outcome, ACCESS_DENIED)
                    self.assertEqual(row.actor_role, role)
                    self.assertIsNotNone(row.actor_user_id)
                    self.assertEqual(row.subject_id, "*")
                    self.assertTrue(row.request_id.startswith("req_"))

    def test_toggle_refused_for_coach_leaves_row_untouched(self):
        # Historical name kept (other tests/PR history reference it); now
        # asserts the CLOSED gap rather than documenting it (#426 round-2
        # review finding 2 — the owner has ruled this in scope). Zero
        # mutation AND a durable denial, not merely the former.
        before = len(self._rows())
        op = self._login("coach")
        status, body, _ = self._req(
            "POST", self._toggle_path(), {"active": False}, opener=op)
        self.assertEqual(status, 403, body)
        c = next(c for c in self.store.all_contact_destinations()
                if c.id == self.player_cid)
        self.assertTrue(c.active)
        new_rows = self._rows()[before:]
        self.assertEqual(len(new_rows), 1, new_rows)
        self.assertEqual(new_rows[0].outcome, ACCESS_DENIED)
        self.assertEqual(new_rows[0].actor_role, "coach")
        self.assertEqual(new_rows[0].purpose, "set_contact_destination_active")

    def test_public_and_invalid_session_get_denial_on_every_route(self):
        for name, (prepare, body, _allowed, _denied) in (
                self._route_fixtures().items()):
            with self.subTest(route=name, session="none"):
                path, check_unchanged = prepare()
                before = len(self._rows())
                status, resp, _ = self._req("POST", path, body)
                self.assertEqual(status, 401, resp)
                check_unchanged()
                new_rows = self._rows()[before:]
                self.assertEqual(len(new_rows), 1, new_rows)
                self.assertEqual(new_rows[0].outcome, ACCESS_DENIED)
                self.assertEqual(new_rows[0].actor_role, vp.NO_PRINCIPAL)
                self.assertIsNone(new_rows[0].actor_user_id)
            with self.subTest(route=name, session="invalid"):
                path, check_unchanged = prepare()
                before = len(self._rows())
                status, resp, _ = self._req(
                    "POST", path, body,
                    cookie=f"{srv.SESSION_COOKIE}=totally-bogus-session-token")
                self.assertEqual(status, 401, resp)
                check_unchanged()
                new_rows = self._rows()[before:]
                self.assertEqual(len(new_rows), 1, new_rows)
                self.assertEqual(new_rows[0].outcome, ACCESS_DENIED)
                self.assertEqual(new_rows[0].actor_role, vp.NO_PRINCIPAL)

    def test_get_and_head_to_the_post_only_routes_are_405_unaffected(self):
        # do_GET's own dispatch, entirely separate from do_POST's generic
        # gate this round changes — confirms the fix touches only the
        # POST path, never turns these into reachable GET/HEAD routes.
        op = self._login("admin")
        for method in ("GET", "HEAD"):
            for name, (prepare, _body, _allowed, _denied) in (
                    self._route_fixtures().items()):
                with self.subTest(method=method, route=name):
                    path, check_unchanged = prepare()
                    before = len(self._rows())
                    status, _resp, _ = self._req(method, path, opener=op)
                    self.assertEqual(status, 405)
                    check_unchanged()
                    self.assertEqual(self._rows()[before:], [])


class MemorySensitiveReadHttpTest(SensitiveReadHttpContract, unittest.TestCase):
    def database_url(self):
        return None


class SqliteSensitiveReadHttpTest(SensitiveReadHttpContract, unittest.TestCase):
    def database_url(self):
        fd, self._tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        return self._tmp_path


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL suite only (TEST_DATABASE_URL not set)")
class PostgresSensitiveReadHttpTest(SensitiveReadHttpContract, unittest.TestCase):
    # #426 round-2 review finding 2 explicitly asks for tri-backend HTTP
    # coverage of the NEW denial-audit behavior — added here rather than
    # relying solely on this file's original "a live PostgreSQL job
    # already exercises this HTTP surface elsewhere" rationale, since that
    # applied to finding 1's already-shipped behavior, not this round's.
    def database_url(self):
        return os.environ["TEST_DATABASE_URL"]

    def setUp(self):
        from hockey_scheduler.store import SqlStore
        # Tracked and closed via addCleanup (#426 round-3 review finding
        # 2) — this one-off schema-reset connection used to be a bare
        # `SqlStore(...)` with no `.close()`, leaking one connection per
        # test. addCleanup runs regardless of what setUp/the test/
        # tearDown do afterward, so it closes even if super().setUp()
        # below raises.
        store = SqlStore(os.environ["TEST_DATABASE_URL"])
        self.addCleanup(store.close)
        store.clear_all_data()
        super().setUp()


if __name__ == "__main__":
    unittest.main()
