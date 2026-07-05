import json
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Role
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.web import server as srv


class PerUserReadReceiptFacadeTest(unittest.TestCase):
    """Two accounts with the same role must not share read state (#69)."""

    def setUp(self):
        self.store, self.game_id, self.ids = build_full_demo_store()
        self.api = ApiService(self.store)

    def _public_id(self, role="league_admin", scope=None, user_id=None):
        feed = self.api.get_notifications(role, scope or {}, user_id=user_id)
        return next(n for n in feed["notifications"]
                    if n["kind"] == "result_approved")["id"]

    def test_same_role_different_user_id_do_not_share_read_state(self):
        # Before #69, every "viewer" — real account or not — shared exactly
        # one "role:viewer" bucket. Two distinct viewer accounts must now get
        # independent read state.
        nid = self._public_id()
        self.api.mark_notification_read(nid, "viewer", {}, user_id="user_v1")
        v1 = self.api.get_notifications("viewer", {}, user_id="user_v1")
        v2 = self.api.get_notifications("viewer", {}, user_id="user_v2")
        self.assertTrue(next(n for n in v1["notifications"]
                             if n["id"] == nid)["read"])
        self.assertFalse(next(n for n in v2["notifications"]
                              if n["id"] == nid)["read"])
        # v2 read nothing: every public notification (one "game published"
        # per published game, plus the finalized result, #87) remains
        # unread for them — the pilot data pack (#97) publishes 19 games.
        expected_public = sum(1 for g in self.store.all_games() if g.published) + 1
        self.assertEqual(v2["unread"], expected_public)

    def test_two_officials_with_the_same_scope_stay_independent(self):
        # Even if two accounts somehow shared identical role+scope (the old
        # bucket key), distinct user_ids must still not collide.
        ref = self.ids["referee_id"]
        scope = {"official_id": ref}
        nid = next(n for n in self.api.get_notifications(
            "official", scope, user_id="user_o1")["notifications"]
            if n["kind"] == "assignment_offered")["id"]
        self.api.mark_notification_read(nid, "official", scope, user_id="user_o1")
        o1 = self.api.get_notifications("official", scope, user_id="user_o1")
        o2 = self.api.get_notifications("official", scope, user_id="user_o2")
        self.assertTrue(next(n for n in o1["notifications"]
                             if n["id"] == nid)["read"])
        self.assertFalse(next(n for n in o2["notifications"]
                              if n["id"] == nid)["read"])

    def test_mark_all_is_scoped_to_the_user_not_the_role(self):
        self.api.mark_all_notifications_read("viewer", {}, user_id="user_v1")
        v1_unread = self.api.get_notifications(
            "viewer", {}, user_id="user_v1")["unread"]
        v2_unread = self.api.get_notifications(
            "viewer", {}, user_id="user_v2")["unread"]
        self.assertEqual(v1_unread, 0)
        self.assertGreater(v2_unread, 0)

    def test_no_user_id_falls_back_to_the_role_scope_bucket(self):
        # The identity-less demo fallback (X-Demo-Role/headerless) keeps its
        # pre-#69 coarser behavior: two requests with no user_id and the same
        # role DO share state, as they did before.
        nid = self._public_id()
        self.api.mark_notification_read(nid, "viewer", {})  # no user_id
        again = self.api.get_notifications("viewer", {})  # no user_id
        self.assertTrue(next(n for n in again["notifications"]
                             if n["id"] == nid)["read"])

    def test_user_id_takes_precedence_over_scope_derivation(self):
        # A coach session normally keys off team_id; with a real user_id the
        # per-user key wins, so two coaches on the same team don't collide.
        home = self.ids["home_team_id"]
        scope = {"team_id": home}
        nid = self._public_id()
        self.api.mark_notification_read(nid, "coach", scope, user_id="user_c1")
        c1 = self.api.get_notifications("coach", scope, user_id="user_c1")
        c2 = self.api.get_notifications("coach", scope, user_id="user_c2")
        self.assertTrue(next(n for n in c1["notifications"]
                             if n["id"] == nid)["read"])
        self.assertFalse(next(n for n in c2["notifications"]
                              if n["id"] == nid)["read"])


class PerUserReadReceiptHttpTest(unittest.TestCase):
    """End-to-end: two logged-in accounts with the same role (#69)."""

    @classmethod
    def setUpClass(cls):
        srv.STATE.reset()
        # A second viewer account, distinct from the seeded "viewer" persona,
        # so both real sessions share role=viewer with no scope at all.
        srv.STATE.api.accounts.create_account(
            "viewer2", "demo2", Role.VIEWER, account_id="user_viewer2")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        srv.STATE.reset()

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _req(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_two_viewer_sessions_have_independent_read_state(self):
        a = self._client()
        b = self._client()
        self._req(a, "POST", "/api/auth/login",
                 {"username": "viewer", "password": "demo"})
        self._req(b, "POST", "/api/auth/login",
                 {"username": "viewer2", "password": "demo2"})

        _, feed_a = self._req(a, "GET", "/api/notifications")
        nid = next(n for n in feed_a["notifications"]
                  if n["kind"] == "result_approved")["id"]
        self._req(a, "POST", f"/api/notifications/{nid}/read", {})

        _, feed_a2 = self._req(a, "GET", "/api/notifications")
        _, feed_b = self._req(b, "GET", "/api/notifications")
        self.assertTrue(next(n for n in feed_a2["notifications"]
                             if n["id"] == nid)["read"])
        self.assertFalse(next(n for n in feed_b["notifications"]
                              if n["id"] == nid)["read"])


if __name__ == "__main__":
    unittest.main()
