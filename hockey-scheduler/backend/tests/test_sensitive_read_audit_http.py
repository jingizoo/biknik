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

import email.message
import io
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
# A real DeviceToken sentinel (#426 round-3 review finding 1) — distinct
# from SENTINEL_EMAIL, which only ever backs ContactDestination rows here.
SENTINEL_PUSH_TOKEN = "sentinel-http-push-leak-probe-426"

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
        # A real device token (#426 round-3 review finding 1) — the SAME
        # official the contact-destination toggle above uses, so the
        # active-toggle route has a real player:/official:-scoped row.
        self.device_token_id = self.api.register_device_token(
            self.official_ref, "fcm", SENTINEL_PUSH_TOKEN, label="Ref phone"
        )["id"]
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
        """#426 round-4 review finding 2: the ``except`` branch below used to
        read ``e`` and return without ever closing it — unlike the success
        path, which already closes its response via ``with op.open(req) as
        r:``. ``urllib.error.HTTPError`` IS an ``addinfourl``/``addbase`` (it
        subclasses both ``OSError`` and the response-wrapper class urlopen()
        itself returns), so it supports the SAME context-manager protocol as
        the success path's ``r`` — ``with e:`` below closes the underlying
        socket/file the moment this method returns, exactly like the success
        branch, instead of leaving it for the garbage collector to find at
        some later, unpredictable point. Left unclosed, every 401/403/405
        response this file's whole matrix provokes (the common case for a
        denial-proving test) leaked one open response per call: reproduced
        verbatim before this fix (see the PR history for the captured
        transcript) as two ``ResourceWarning: Implicitly cleaning up
        <HTTPError ...>`` warnings from a single test method, invisible to
        plain ``python3 -m unittest`` because the warning fires from a
        ``__del__`` at garbage-collection time, well after the test already
        recorded "ok".

        WHAT KEEPS THE ``with e:`` HERE, on every interpreter:
        :class:`HttpErrorBranchClosesOnEveryReturnPath` at the bottom of this
        file. It injects an ``HTTPError`` that RECORDS its own ``close()``
        and drives THIS method's error branch through all four of that
        branch's exits, so removing the ``with e:`` — or narrowing it to one
        status code — fails. That regression does not depend on
        ``ResourceWarning``, which is what makes it work on CI's 3.11, where
        an abandoned ``HTTPError`` warns about nothing at all;
        test_resource_warning_leak.py covers the ResourceWarning-visible half
        and documents exactly which interpreters can see it.
        """
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
            with e:
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

    # -- GET /api/notifications/device-tokens (#426 round-3 review finding
    # 1) — the SAME contract as /api/notifications/contacts above, real
    # HTTP, real sentinel token, mirroring each test 1:1 rather than
    # parameterising: the two routes' authz/audit wiring is independent
    # code in server.py even though it now shares the SAME facade gate.
    def test_device_tokens_authorized_roles_get_real_reads_with_exact_attribution(self):
        for role in AUTHORIZED:
            with self.subTest(role=role):
                before = len(self._rows())
                op = self._login(PERSONA[role])
                status, body, _ = self._req(
                    "GET", "/api/notifications/device-tokens", opener=op)
                self.assertEqual(status, 200, body)
                self.assertIn(SENTINEL_PUSH_TOKEN,
                              [t["token"] for t in body["device_tokens"]])
                new_rows = self._rows()[before:]
                official_rows = [r for r in new_rows
                                 if r.subject_id == self.official_ref]
                self.assertEqual(len(official_rows), 1, new_rows)
                row = official_rows[0]
                self.assertEqual(row.outcome, ACCESS_ALLOWED)
                self.assertEqual(row.actor_role, role)
                self.assertIsNotNone(row.actor_user_id)
                self.assertEqual(row.purpose, "list_device_tokens")

    def test_device_tokens_unauthorized_signed_in_roles_get_zero_disclosure_and_a_denial(self):
        for role in UNAUTHORIZED:
            with self.subTest(role=role):
                before = len(self._rows())
                op = self._login(PERSONA[role])
                status, body, _ = self._req(
                    "GET", "/api/notifications/device-tokens", opener=op)
                self.assertEqual(status, 403, body)
                self.assertNotIn(SENTINEL_PUSH_TOKEN, json.dumps(body))
                self.assertNotIn("device_tokens", body)
                new_rows = self._rows()[before:]
                self.assertEqual(len(new_rows), 1, new_rows)
                self.assertEqual(new_rows[0].outcome, ACCESS_DENIED)
                self.assertEqual(new_rows[0].actor_role, role)
                self.assertEqual(new_rows[0].subject_id, "*")

    def test_device_tokens_public_no_session_gets_401_and_a_durable_denial(self):
        before = len(self._rows())
        status, body, _ = self._req("GET", "/api/notifications/device-tokens")
        self.assertEqual(status, 401, body)
        self.assertNotIn(SENTINEL_PUSH_TOKEN, json.dumps(body))
        new_rows = self._rows()[before:]
        self.assertEqual(len(new_rows), 1, new_rows)
        self.assertEqual(new_rows[0].outcome, ACCESS_DENIED)
        self.assertEqual(new_rows[0].actor_role, vp.NO_PRINCIPAL)
        self.assertIsNone(new_rows[0].actor_user_id)

    def test_device_tokens_invalid_session_cookie_gets_401_and_a_durable_denial(self):
        before = len(self._rows())
        status, body, _ = self._req(
            "GET", "/api/notifications/device-tokens",
            cookie=f"{srv.SESSION_COOKIE}=totally-bogus-session-token")
        self.assertEqual(status, 401, body)
        new_rows = self._rows()[before:]
        self.assertEqual(len(new_rows), 1, new_rows)
        self.assertEqual(new_rows[0].outcome, ACCESS_DENIED)
        self.assertEqual(new_rows[0].actor_role, vp.NO_PRINCIPAL)

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

    def _device_token_prepare(self):
        # #426 round-3 review finding 1 — the same table-driven shape as
        # the contacts toggle above, added as a THIRD `_route_fixtures()`
        # entry so it is automatically exercised by every existing
        # allowed/denied/public/GET-405 table-driven test below, not just
        # a hand-written duplicate of each.
        path = self._device_token_toggle_path()
        before = next(t for t in self.store.all_device_tokens()
                     if t.id == self.device_token_id).active

        def check_unchanged():
            still = next(t for t in self.store.all_device_tokens()
                        if t.id == self.device_token_id)
            self.assertEqual(still.active, before)
        return path, check_unchanged

    def _device_token_toggle_path(self):
        return f"/api/notifications/device-tokens/{self.device_token_id}/active"

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
            # Device-token active-toggle (#426 round-3 review finding 1):
            # gated at MANAGE_SCHEDULE (web/authz.py), the SAME permission
            # retry/ignore use — both League Admin and Arena Manager hold
            # it, unlike the contacts toggle's stricter League-Admin-only
            # MANAGE_SETUP.
            "device_token_toggle": (
                self._device_token_prepare, {"active": False},
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

    def test_device_token_toggle_propagates_the_real_signed_in_actor(self):
        # #426 round-3 review finding 1: the exact SENT/returned token
        # agrees with the exact audited subject/purpose/request id for
        # ONE real HTTP round trip — not just "some row appeared".
        before = len(self._rows())
        op = self._login("arena")
        status, body, _ = self._req(
            "POST", self._device_token_toggle_path(), {"active": False},
            opener=op)
        self.assertEqual(status, 200, body)
        self.assertFalse(body["active"])
        self.assertEqual(body["token"], SENTINEL_PUSH_TOKEN)
        new_rows = self._rows()[before:]
        self.assertEqual(len(new_rows), 1, new_rows)
        row = new_rows[0]
        self.assertEqual(row.outcome, ACCESS_ALLOWED)
        self.assertEqual(row.actor_role, "arena_manager")
        self.assertIsNotNone(row.actor_user_id)
        self.assertEqual(row.purpose, "set_device_token_active")
        self.assertEqual(row.subject_id, self.official_ref)
        self.assertTrue(row.request_id.startswith("req_"))

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


# ======================================================================
# THE TARGETED CLOSE REGRESSION FOR _req()'s ERROR BRANCH
# ======================================================================
# OWNER RULING, PR #427, 2026-08-22: "#427 must retain the smallest
# targeted close-tracking regression around the real
# SensitiveReadHttpContract._req(). Replace the negative 'scanner is
# absent' importability test: it does not protect _req() and makes #427
# fail if #433 lands first. Under Python 3.11, injecting an HTTPError
# that records close() must prove that the actual error branch closes on
# every return path; removing or conditionalizing `with e:` must fail."
#
# WHY NOT ResourceWarning. test_resource_warning_leak.py's HTTPError half
# is a NO-OP on the interpreter that gates this repository: an abandoned
# HTTPError emits no ResourceWarning of any kind on 3.11 or 3.12 (it does
# on 3.14). Watching the garbage collector therefore cannot protect this
# branch where it most needs protecting. CLOSING IS AN OBSERVABLE ACT,
# not a garbage-collection side effect, so this observes the act: an
# HTTPError that RECORDS ITS OWN close() calls, driven through the real
# _req(), asserted directly. That works identically on every interpreter,
# which is the whole point.
#
# WHY THIS IS NOT THE REPOSITORY-WIDE GUARD. It protects _req() and
# nothing else. A NEW `except urllib.error.HTTPError as e:` somewhere
# else in the tree that never closes what it binds is still caught by
# nothing on an interpreter that cannot warn. That guard is
# jingizoo/biknik#433, and it has not landed.
#
# THE ERROR BRANCH'S EXITS, enumerated from the code rather than assumed
# (see _req above -- `with e:` / `raw = e.read()` /
# `return e.code, (json.loads(raw) if raw else {}), dict(e.headers)`):
#
#   R1  empty body            -> RETURNS (code, {}, headers)
#   R2  non-empty JSON body   -> RETURNS (code, parsed, headers)
#   R3  non-empty NON-JSON    -> json.loads raises; branch exits by
#                                EXCEPTION, mid-`with`
#   R4  e.read() itself fails -> branch exits by EXCEPTION before the
#                                return expression is even built
#
# Two of the four are exception exits, which is exactly where a hand-
# rolled `e.close()` before the `return` would silently miss -- so
# "every return path" is enforced by covering all four, each across
# SEVERAL DISTINCT STATUS CODES so a `with e:` made conditional on one
# code cannot pass either.


class _ExplodingBody(io.BytesIO):
    """A response body whose ``read()`` fails, for exit R4."""

    def read(self, *args, **kwargs):
        raise OSError("body stream failed mid-read")


class _CloseRecordingHTTPError(urllib.error.HTTPError):
    """A REAL ``urllib.error.HTTPError`` that records its own ``close()``.

    A subclass rather than a mock, because what is under test is whether
    the real branch invokes the real protocol: ``HTTPError`` IS an
    ``addinfourl``/``addbase``, ``with e:`` calls ``addbase.__exit__``,
    and ``__exit__`` calls ``self.close()`` -- so an override here sees
    exactly the call the fix is supposed to make, and ``super().close()``
    still performs the real close (asserted through ``fp.closed``, so a
    recorded call that closed nothing would not pass).

    THE TEST HOLDS A REFERENCE to every instance it builds, so nothing
    here can be closed by the garbage collector part-way through: a
    recorded close is a close the branch made.
    """

    def __init__(self, code, body=b"", fp=None):
        self.closes = []
        headers = email.message.Message()
        headers["Content-Type"] = "application/json"
        headers["X-Close-Probe"] = f"code-{code}"
        super().__init__(f"http://127.0.0.1/close-probe/{code}", code,
                         "Close Probe", headers,
                         io.BytesIO(body) if fp is None else fp)

    def close(self):
        self.closes.append(self.code)
        super().close()


class _RaisingOpener:
    """The injection point: stands where ``urllib.request.build_opener()``
    normally stands and raises the prepared error out of ``open()``.

    ``_req`` takes its opener as a parameter, so this drives the REAL
    method -- its Request construction, its ``try``, its ``except
    urllib.error.HTTPError as e:`` branch -- with no server, no socket
    and no interpreter-dependent timing. Records the Request it was
    handed so the test can prove ``_req`` really got that far.
    """

    def __init__(self, error):
        self.error = error
        self.requests = []

    def open(self, req, *args, **kwargs):
        self.requests.append(req)
        raise self.error


class _ReqOnly:
    """The smallest object ``_req`` needs: it reads ``self.port`` and
    nothing else. Bound to the REAL function below (asserted), so no
    fixture server has to be started to exercise the error branch."""

    port = 65535


# The status codes this file's own matrix provokes, plus a 500. Several
# codes, deliberately: a `with e:` narrowed to one status ("close only
# for 403") would still pass a single-code test.
CLOSE_PROBE_CODES = (401, 403, 405, 500)


class HttpErrorBranchClosesOnEveryReturnPath(unittest.TestCase):
    """The #427 targeted regression for ``_req()``'s ``except`` branch.

    Interpreter-independent by construction: it never consults
    ``ResourceWarning``, never starts a server and never waits for a GC
    pass. Verified on CPython 3.12 (the closest available proxy for CI's
    3.11 -- neither warns for an abandoned HTTPError) and on 3.14.
    """

    def _drive(self, err):
        """Run the REAL ``_req`` against a prepared error and return
        whatever it produced."""
        opener = _RaisingOpener(err)
        result = SensitiveReadHttpContract._req(
            _ReqOnly(), "GET", "/api/notifications/contacts", opener=opener)
        # _req really reached the opener, i.e. the branch under test is
        # the one that ran.
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(
            opener.requests[0].full_url,
            f"http://127.0.0.1:{_ReqOnly.port}/api/notifications/contacts")
        return result

    def test_the_function_under_test_is_the_one_the_file_uses(self):
        """Anti-vacuity: this class calls ``_req`` unbound, so it must be
        the very function every test class in this file inherits. If a
        subclass ever overrode it, this regression would be protecting
        something nothing uses."""
        for cls in (MemorySensitiveReadHttpTest, SqliteSensitiveReadHttpTest,
                    PostgresSensitiveReadHttpTest):
            self.assertIs(cls._req, SensitiveReadHttpContract._req, cls)

    def test_r1_empty_body_return_path_closes(self):
        for code in CLOSE_PROBE_CODES:
            with self.subTest(code=code, path="R1 empty body"):
                err = _CloseRecordingHTTPError(code, b"")
                status, body, headers = self._drive(err)
                self.assertEqual(status, code)
                self.assertEqual(body, {})
                self.assertEqual(headers["X-Close-Probe"], f"code-{code}")
                self._assert_closed(err, code)

    def test_r2_json_body_return_path_closes(self):
        for code in CLOSE_PROBE_CODES:
            with self.subTest(code=code, path="R2 json body"):
                err = _CloseRecordingHTTPError(
                    code, b'{"error": "denied", "code": %d}' % code)
                status, body, _headers = self._drive(err)
                self.assertEqual(status, code)
                self.assertEqual(body, {"error": "denied", "code": code})
                self._assert_closed(err, code)

    def test_r3_undecodable_body_exception_exit_closes(self):
        """The branch can leave through ``json.loads`` raising, which is
        NOT a return -- and is precisely where a hand-rolled
        ``e.close()`` placed before the ``return`` would be skipped."""
        for code in CLOSE_PROBE_CODES:
            with self.subTest(code=code, path="R3 undecodable body"):
                err = _CloseRecordingHTTPError(code, b"<html>nope</html>")
                with self.assertRaises(json.JSONDecodeError):
                    self._drive(err)
                self._assert_closed(err, code)

    def test_r4_failing_read_exception_exit_closes(self):
        """The earliest exit of all: ``e.read()`` itself raises, before
        the return expression is even built."""
        for code in CLOSE_PROBE_CODES:
            with self.subTest(code=code, path="R4 failing read"):
                err = _CloseRecordingHTTPError(
                    code, fp=_ExplodingBody(b"never read"))
                with self.assertRaises(OSError):
                    self._drive(err)
                self._assert_closed(err, code)

    def test_every_enumerated_exit_is_covered_by_a_test(self):
        """The enumeration in this section's header is a claim about
        ``_req``'s source; this keeps the claim and the coverage tied
        together, so an exit added to the branch is noticed."""
        covered = set()
        for name in dir(self):
            if not name.startswith("test_r"):
                continue
            tag = name.split("_")[1]
            if len(tag) == 2 and tag[0] == "r" and tag[1].isdigit():
                covered.add(tag)
        self.assertEqual(covered, {"r1", "r2", "r3", "r4"}, sorted(covered))

    def _assert_closed(self, err, code):
        # RECORDED: the branch called close() -- exactly once, on the way
        # out, whatever way it went out.
        self.assertEqual(
            err.closes, [code],
            f"_req()'s HTTPError branch did not close the {code} response "
            f"exactly once (recorded closes: {err.closes}). The `with e:` "
            f"in _req's `except urllib.error.HTTPError as e:` branch is "
            f"what closes it; removing it, or making it conditional on a "
            f"status code, leaves the response body open on this path. "
            f"This is NOT caught by ResourceWarning on CI's interpreter "
            f"-- see test_resource_warning_leak.py's docstring.")
        # ...and the close was REAL, not merely recorded.
        self.assertTrue(err.fp.closed, code)

if __name__ == "__main__":
    unittest.main()
