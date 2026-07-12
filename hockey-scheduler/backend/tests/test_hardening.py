import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import DeliveryStatus, NotificationChannel
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.web.server import STATE, Handler


def _clock():
    return datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)


class HardeningHttpTest(unittest.TestCase):
    """Security headers, cookie flags, logout, reset gating, traversal."""

    @classmethod
    def setUpClass(cls):
        STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def _req(self, method, path, body=None, headers=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    # -- headers -------------------------------------------------------------
    def test_api_responses_carry_security_headers(self):
        _, headers, _ = self._req("GET", "/api/demo/overview")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("Referrer-Policy"), "same-origin")
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_html_gets_csp_and_frame_protection(self):
        _, headers, _ = self._req("GET", "/")
        csp = headers.get("Content-Security-Policy", "")
        self.assertIn("default-src 'self'", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

    def test_static_assets_get_nosniff_but_no_csp(self):
        _, headers, _ = self._req("GET", "/app.js")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertIsNone(headers.get("Content-Security-Policy"))

    # -- session cookie / logout ---------------------------------------------
    def test_login_cookie_is_httponly_samesite(self):
        _, headers, _ = self._req(
            "POST", "/api/auth/login",
            {"username": "admin", "password": "demo"},
            {"Content-Type": "application/json"})
        cookie = headers.get("Set-Cookie", "")
        self.assertIn("hs_sid=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_logout_invalidates_the_session(self):
        _, headers, _ = self._req(
            "POST", "/api/auth/login",
            {"username": "coach", "password": "demo"},
            {"Content-Type": "application/json"})
        sid = headers.get("Set-Cookie", "").split(";")[0]
        status, _, body = self._req("GET", "/api/auth/me", None, {"Cookie": sid})
        self.assertEqual(json.loads(body)["user"]["role"], "coach")
        self._req("POST", "/api/auth/logout", {}, {"Cookie": sid})
        # The old token must no longer resolve — stale cookie → 401.
        status, _, _ = self._req("GET", "/api/auth/me", None, {"Cookie": sid})
        self.assertEqual(status, 401)

    # -- destructive reset is League-Admin-only (#215) ------------------------
    def test_viewer_cannot_reset(self):
        status, _, body = self._req("POST", "/api/reset",
                                    {"confirm": "RESET"},
                                    {"X-Demo-Role": "viewer",
                                     "Content-Type": "application/json"})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "forbidden")

    def test_arena_manager_cannot_reset(self):
        # #215: reset is MANAGE_SETUP (League-Admin-only), so the arena manager
        # — who can create rinks/ice — is nonetheless rejected.
        status, _, body = self._req("POST", "/api/reset",
                                    {"confirm": "RESET"},
                                    {"X-Demo-Role": "arena_manager",
                                     "Content-Type": "application/json"})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "forbidden")

    def test_league_admin_can_reset(self):
        status, _, _ = self._req("POST", "/api/reset",
                                 {"confirm": "RESET"},
                                 {"X-Demo-Role": "league_admin",
                                  "Content-Type": "application/json"})
        self.assertEqual(status, 200)

    def test_reset_requires_typed_confirmation(self):
        # An empty POST (no confirm value) is refused server-side (#215).
        status, _, body = self._req("POST", "/api/reset", {},
                                    {"X-Demo-Role": "league_admin",
                                     "Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "validation_error")

    def _admin(self):
        return {"X-Demo-Role": "league_admin", "Content-Type": "application/json"}

    def _leagues(self):
        _, _, body = self._req("GET", "/api/demo/overview", None)
        return json.loads(body)["leagues"]

    def test_demo_load_and_clear_roundtrip(self):
        # #215: Load builds the sample dataset; Clear (typed CLEAR) empties it.
        status, _, _ = self._req("POST", "/api/demo/load", {}, self._admin())
        self.assertEqual(status, 200)
        self.assertTrue(self._leagues())
        # Clear needs the typed confirmation.
        status, _, body = self._req("POST", "/api/demo/clear", {}, self._admin())
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "validation_error")
        status, _, _ = self._req("POST", "/api/demo/clear",
                                 {"confirm": "CLEAR"}, self._admin())
        self.assertEqual(status, 200)
        self.assertEqual(self._leagues(), [])

    def test_demo_load_and_clear_are_league_admin_only(self):
        for route, body in (("/api/demo/load", {}),
                            ("/api/demo/clear", {"confirm": "CLEAR"})):
            status, _, _ = self._req("POST", route, body,
                                     {"X-Demo-Role": "viewer",
                                      "Content-Type": "application/json"})
            self.assertEqual(status, 403, route)

    # -- static traversal stays blocked ----------------------------------------
    def test_path_traversal_is_rejected(self):
        for probe in ("/../CLAUDE.md", "/..%2f..%2fetc%2fpasswd",
                      "/static/../../server.py"):
            status, _, _ = self._req("GET", probe)
            self.assertEqual(status, 404, probe)


class LateRegistrationDeliveryTest(unittest.TestCase):
    """Destinations re-resolve at send time (hardening review, functional)."""

    def setUp(self):
        self.store, self.game_id, self.ids = build_full_demo_store()
        self.api = ApiService(self.store)

    def test_contact_registered_after_emission_is_used(self):
        # Seed already emitted deliveries with .invalid placeholders. Register
        # the real contact afterwards — processing must pick it up.
        self.api.set_contact_destination("public", "email", "fans@club.example")
        self.api.process_notification_deliveries()
        pub = next(d for d in self.store.all_notification_deliveries()
                   if d.recipient_ref == "public"
                   and d.channel == NotificationChannel.EMAIL)
        self.assertEqual(pub.destination, "fans@club.example")
        self.assertEqual(pub.status, DeliveryStatus.SENT)

    def test_token_registered_after_emission_is_used(self):
        self.api.register_device_token(
            "official:" + self.ids["referee_id"], "fcm", "tok-late")
        self.api.process_notification_deliveries()
        push = next(d for d in self.store.all_notification_deliveries()
                    if (d.recipient_ref or "").startswith("official:")
                    and d.channel == NotificationChannel.PUSH)
        self.assertEqual(push.destination, "tok-late")

    def test_failed_placeholder_retry_recovers_after_registration(self):
        # In live push mode the placeholder fails; registering a token between
        # runs lets the retry succeed instead of failing to exhaustion.
        from hockey_scheduler.services import DeliveryWorker, ProviderPushTransport
        sent = []
        transport = ProviderPushTransport(
            endpoint="https://push.example.test/send",
            client=lambda ep, key, payload: sent.append(payload))
        worker = DeliveryWorker(self.store, _clock, push_transport=transport)
        worker.process_pending()  # placeholders → failed
        ref = "official:" + self.ids["referee_id"]
        self.api.register_device_token(ref, "fcm", "tok-recovered")
        worker.process_pending()  # retry re-resolves and succeeds
        push = next(d for d in self.store.all_notification_deliveries()
                    if d.recipient_ref == ref
                    and d.channel == NotificationChannel.PUSH)
        self.assertEqual(push.status, DeliveryStatus.SENT)
        self.assertTrue(any(p["token"] == "tok-recovered" for p in sent))


if __name__ == "__main__":
    unittest.main()
