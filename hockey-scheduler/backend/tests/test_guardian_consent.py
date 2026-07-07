"""Guardian↔junior link administration + consent record (#35 V1).

Before this, a GuardianLink could only come from deterministic demo seeding —
there was no HTTP path to create or verify one, so "verifiable guardian
authorization" (the #35 acceptance criterion) was unexercisable in the
running app. This covers the new admin surface:

  * ``link_guardian`` now requires ``guardian_user_id`` to reference a real
    account holding the GUARDIAN role (previously any opaque string worked,
    which was safe only because nothing HTTP-reachable called it).
  * ``verify_link`` optionally records a ``consent_method``/``consented_at``
    pair — the GDPR Art. 8 consent record. The service method itself still
    allows verifying without one (internal/seed callers), but the real
    operator-facing route (``verify_guardian_link`` / ``POST
    /api/guardians/links/{id}/verify``) requires a non-blank consent_method.
  * The HTTP routes are League-Admin-only (MANAGE_USERS) and attribute the
    action to the server-resolved session, not a client-supplied actor_id —
    the same anti-forgery precedent as ``revoke_account_session``.

Three layers are covered: the service (account validation, consent capture),
the API facade (consent_method requiredness), and the HTTP routes (permission
gating, actor attribution, and a full create -> verify -> guardian-acts-for-
junior integration proving the new path produces the same authorization state
the existing #26 gate already consumes.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND, FakeClock  # noqa: F401  (BACKEND sets up sys.path)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Division, Player, Position, Team
from hockey_scheduler.domain.errors import ValidationError
from hockey_scheduler.store import InMemoryStore
from hockey_scheduler.web import server as srv


def _seeded_api():
    s = InMemoryStore()
    s.add_division(Division(id="d", season_id="se", name="U16 Elite"))
    s.add_team(Team(id="home", name="Home", division="U16 Elite", division_id="d"))
    s.add_player(Player(id="junior", team_id="home", name="Junior J.",
                        position=Position.FORWARD))
    api = ApiService(s)
    api.roster.clock = FakeClock()
    return api, s


class LinkGuardianAccountValidationTest(unittest.TestCase):
    def setUp(self):
        self.api, self.store = _seeded_api()

    def test_requires_an_existing_account(self):
        with self.assertRaises(ValidationError):
            self.api.guardians.link_guardian("no_such_account", "junior")

    def test_requires_the_guardian_role(self):
        self.api.accounts.create_account("coach1", "pw", "coach", account_id="acct1")
        with self.assertRaises(ValidationError):
            self.api.guardians.link_guardian("acct1", "junior")

    def test_a_real_guardian_account_works(self):
        self.api.accounts.create_account("guard1", "pw", "guardian", account_id="g1")
        link = self.api.guardians.link_guardian("g1", "junior")
        self.assertEqual(link.guardian_user_id, "g1")
        self.assertFalse(link.verified)


class VerifyConsentTest(unittest.TestCase):
    def setUp(self):
        self.api, self.store = _seeded_api()
        self.api.accounts.create_account("guard1", "pw", "guardian", account_id="g1")
        self.link = self.api.guardians.link_guardian("g1", "junior")

    def test_verify_without_consent_method_still_grants_authority(self):
        # Service-level flexibility for internal/seed callers (#26 predates
        # #35's consent record) — verifying without one still works.
        self.api.guardians.verify_link(self.link.id)
        self.assertTrue(self.api.guardians.is_verified_guardian("g1", "junior"))
        link = self.store.get_guardian_link(self.link.id)
        self.assertIsNone(link.consent_method)
        self.assertIsNone(link.consented_at)

    def test_verify_with_consent_method_records_it(self):
        self.api.guardians.verify_link(self.link.id, consent_method="signed_form")
        link = self.store.get_guardian_link(self.link.id)
        self.assertEqual(link.consent_method, "signed_form")
        self.assertIsNotNone(link.consented_at)

    def test_verify_audit_includes_consent_method_when_provided(self):
        self.api.guardians.verify_link(self.link.id, consent_method="verbal_confirmed")
        entry = next(a for a in self.store.all_setup_audit()
                    if a.action == "guardian_link_verified")
        self.assertEqual(entry.detail.get("consent_method"), "verbal_confirmed")

    def test_all_links_lists_every_link(self):
        links = self.api.guardians.all_links()
        self.assertEqual([l.id for l in links], [self.link.id])


class ApiFacadeConsentTest(unittest.TestCase):
    """The API facade layer requires consent_method even though the
    underlying service method leaves it optional (#35)."""

    def setUp(self):
        self.api, self.store = _seeded_api()
        self.api.accounts.create_account("guard1", "pw", "guardian", account_id="g1")

    def test_create_guardian_link_via_api(self):
        res = self.api.create_guardian_link("g1", "junior")
        self.assertEqual(res["guardian_user_id"], "g1")
        self.assertFalse(res["verified"])

    def test_verify_requires_a_non_blank_consent_method(self):
        link = self.api.create_guardian_link("g1", "junior")
        for bad in (None, "", "   "):
            res = self.api.verify_guardian_link(link["id"], bad)
            self.assertEqual(res["error"]["code"], "validation_error")

    def test_verify_with_consent_method_succeeds(self):
        link = self.api.create_guardian_link("g1", "junior")
        res = self.api.verify_guardian_link(link["id"], "signed_form")
        self.assertTrue(res["verified"])
        self.assertEqual(res["consent_method"], "signed_form")

    def test_list_guardian_links_via_api(self):
        self.api.create_guardian_link("g1", "junior")
        self.assertEqual(len(self.api.list_guardian_links()), 1)


class GuardianConsentHttpTest(unittest.TestCase):
    """Route-level permission gating, actor attribution, and the full
    create -> verify -> guardian-acts-for-junior flow over real HTTP."""

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

    def _login(self, username, password="demo"):
        c = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        self._post(c, "/api/auth/login", {"username": username, "password": password})
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

    def _get(self, opener, path):
        try:
            with opener.open(f"http://127.0.0.1:{self.port}{path}") as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_create_guardian_link_requires_manage_users(self):
        team_id = srv.STATE.ids["home_team_id"]
        _, p = self._post(self._login("admin"), "/api/setup/player",
                          {"team_id": team_id, "name": "New Junior", "position": "forward"})
        anon = urllib.request.build_opener()
        status, _ = self._post(anon, "/api/guardians/links",
                               {"guardian_user_id": "user_guardian", "player_id": p["id"]})
        self.assertEqual(status, 401)
        coach = self._login("coach")
        status, _ = self._post(coach, "/api/guardians/links",
                               {"guardian_user_id": "user_guardian", "player_id": p["id"]})
        self.assertEqual(status, 403)

    def test_list_guardian_links_requires_manage_users(self):
        coach = self._login("coach")
        status, _ = self._get(coach, "/api/guardians/links")
        self.assertEqual(status, 403)
        admin = self._login("admin")
        status, body = self._get(admin, "/api/guardians/links")
        self.assertEqual(status, 200)
        self.assertIsInstance(body, list)

    def test_admin_creates_and_verifies_a_link_with_consent(self):
        admin = self._login("admin")
        team_id = srv.STATE.ids["home_team_id"]
        _, p = self._post(admin, "/api/setup/player",
                          {"team_id": team_id, "name": "Consent Kid", "position": "forward"})
        status, acct = self._post(admin, "/api/accounts", {
            "username": "new_guardian", "password": "demo", "role": "guardian"})
        self.assertEqual(status, 200)

        status, link = self._post(admin, "/api/guardians/links", {
            "guardian_user_id": acct["id"], "player_id": p["id"]})
        self.assertEqual(status, 200)
        self.assertFalse(link["verified"])

        status, body = self._post(
            admin, f"/api/guardians/links/{link['id']}/verify", {"consent_method": ""})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")

        status, verified = self._post(
            admin, f"/api/guardians/links/{link['id']}/verify",
            {"consent_method": "signed_form"})
        self.assertEqual(status, 200)
        self.assertTrue(verified["verified"])
        self.assertEqual(verified["consent_method"], "signed_form")

    def test_actor_attribution_is_server_resolved_not_client_supplied(self):
        # Verification is a consent record — the acting identity must come
        # from the signed-in session, never a client-suppliable actor_id
        # (same anti-forgery reasoning as revoke_account_session).
        admin = self._login("admin")
        team_id = srv.STATE.ids["home_team_id"]
        _, p = self._post(admin, "/api/setup/player",
                          {"team_id": team_id, "name": "Audit Kid", "position": "forward"})
        _, acct = self._post(admin, "/api/accounts", {
            "username": "audit_guardian", "password": "demo", "role": "guardian"})
        _, link = self._post(admin, "/api/guardians/links", {
            "guardian_user_id": acct["id"], "player_id": p["id"]})
        self._post(admin, f"/api/guardians/links/{link['id']}/verify",
                  {"consent_method": "signed_form", "actor_id": "spoofed_someone_else"})
        audit = srv.STATE.api.store.all_setup_audit()
        entry = next(a for a in audit if a.action == "guardian_link_verified"
                    and a.entity_id == link["id"])
        _, accts = self._get(admin, "/api/accounts")
        admin_account_id = next(a["id"] for a in accts["user_accounts"]
                                if a["username"] == "admin")
        self.assertEqual(entry.actor_id, admin_account_id)
        self.assertNotEqual(entry.actor_id, "spoofed_someone_else")

    def test_newly_verified_guardian_can_act_for_the_junior(self):
        # Proves the new admin-facing path produces the SAME authorization
        # state the existing #26 gate already consumes.
        admin = self._login("admin")
        team_id = srv.STATE.ids["home_team_id"]
        _, p = self._post(admin, "/api/setup/player",
                          {"team_id": team_id, "name": "Integration Kid", "position": "forward"})
        _, acct = self._post(admin, "/api/accounts", {
            "username": "flow_guardian", "password": "demo", "role": "guardian"})
        _, link = self._post(admin, "/api/guardians/links", {
            "guardian_user_id": acct["id"], "player_id": p["id"]})
        self._post(admin, f"/api/guardians/links/{link['id']}/verify",
                  {"consent_method": "signed_form"})

        guardian = self._login("flow_guardian")
        status, home = self._get(guardian, "/api/me/guardian/home")
        self.assertEqual(status, 200)
        ids = [j["player_id"] for j in home["juniors"]]
        self.assertIn(p["id"], ids)

        gid = srv.STATE.ids["game_id"]
        status, av = self._post(
            guardian, f"/api/me/guardian/{p['id']}/games/{gid}/availability",
            {"availability_status": "available"})
        self.assertEqual(status, 200)
        self.assertEqual(av["response_source"], "guardian")


if __name__ == "__main__":
    unittest.main()
