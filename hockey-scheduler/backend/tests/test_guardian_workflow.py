"""Guardian-linked junior player response workflow (#26).

A verified guardian may respond (I'm In / Can't Play / accept-or-decline a
substitute offer / view an opportunity) on behalf of a LINKED junior. The
authority is a verified :class:`GuardianLink`; an unverified or absent link
grants nothing. The audit trail records WHO acted (the guardian) separately
from WHOM it was for (the junior), and no guardian PII exists in the model, so
nothing can leak into a coach/operator view.

Two layers are covered:
  * the service (link model + verification + authorization predicate), and
  * the ``/api/me/guardian/*`` HTTP routes (identity scoping, per-action link
    enforcement, audit attribution, no data leakage).
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import timedelta
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND, FakeClock  # noqa: F401  (BACKEND sets up sys.path)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    Division, Game, LeagueSeason, Player, Position, Team)
from hockey_scheduler.domain.errors import NotFoundError
from hockey_scheduler.store import InMemoryStore
from hockey_scheduler.web import server as srv


def _seeded_api():
    s = InMemoryStore()
    s.add_league_season(LeagueSeason(id="ls", league_id="l", season_id="se"))
    s.add_division(Division(id="d", league_season_id="ls", name="U16 Elite"))
    s.add_team(Team(id="home", name="Home", division="U16 Elite", division_id="d"))
    s.add_team(Team(id="away", name="Away", division="U16 Elite", division_id="d"))
    s.add_player(Player(id="junior", team_id="home", name="Junior J.",
                        position=Position.FORWARD))
    s.add_player(Player(id="other", team_id="home", name="Other O.",
                        position=Position.FORWARD))
    api = ApiService(s)
    api.roster.clock = FakeClock()
    # link_guardian requires a real account with the guardian role (#35) —
    # "g1"/"g2" below are the guardian_user_ids this file's tests link/verify.
    api.accounts.create_account("guardian1", "demo", "guardian", account_id="g1")
    api.accounts.create_account("guardian2", "demo", "guardian", account_id="g2")
    return api, s


class GuardianLinkModelTest(unittest.TestCase):
    def setUp(self):
        self.api, self.store = _seeded_api()

    def test_new_link_is_unverified_and_grants_nothing(self):
        self.api.guardians.link_guardian("g1", "junior")
        self.assertFalse(self.api.guardians.is_verified_guardian("g1", "junior"))
        self.assertEqual(self.api.guardians.verified_junior_ids("g1"), [])

    def test_verify_link_grants_authority(self):
        link = self.api.guardians.link_guardian("g1", "junior")
        self.api.guardians.verify_link(link.id)
        self.assertTrue(self.api.guardians.is_verified_guardian("g1", "junior"))
        self.assertEqual(self.api.guardians.verified_junior_ids("g1"), ["junior"])

    def test_link_is_idempotent(self):
        a = self.api.guardians.link_guardian("g1", "junior")
        b = self.api.guardians.link_guardian("g1", "junior")
        self.assertEqual(a.id, b.id)

    def test_link_to_unknown_player_is_rejected(self):
        with self.assertRaises(NotFoundError):
            self.api.guardians.link_guardian("g1", "no_such_player")

    def test_authority_does_not_bleed_across_players_or_guardians(self):
        link = self.api.guardians.link_guardian("g1", "junior")
        self.api.guardians.verify_link(link.id)
        # Same guardian, a different junior they are not linked to.
        self.assertFalse(self.api.guardians.is_verified_guardian("g1", "other"))
        # A different guardian, the same junior.
        self.assertFalse(self.api.guardians.is_verified_guardian("g2", "junior"))

    def test_link_and_verify_are_audited(self):
        link = self.api.guardians.link_guardian(
            "g1", "junior", actor_id="op")
        self.api.guardians.verify_link(link.id, actor_id="op")
        actions = [a.action for a in self.store.all_setup_audit()
                   if a.entity_type == "guardian_link"]
        self.assertIn("guardian_link_created", actions)
        self.assertIn("guardian_link_verified", actions)

    def test_audit_carries_no_guardian_pii(self):
        # A link is two opaque ids + a flag — the audit detail can only ever
        # reference those, never a name/email/phone.
        link = self.api.guardians.link_guardian("g1", "junior", actor_id="op")
        entry = next(a for a in self.store.all_setup_audit()
                     if a.action == "guardian_link_created")
        self.assertEqual(set(entry.detail), {"guardian_user_id", "player_id",
                                             "verified"})


class GuardianHomeTest(unittest.TestCase):
    def setUp(self):
        self.api, self.store = _seeded_api()
        base = self.api.roster.clock()
        self.store.add_game(Game(
            id="g1", home_team_id="home", away_team_id="away",
            start_time=base + timedelta(days=1),
            end_time=base + timedelta(days=1, hours=1),
            division_id="d", published=True,
            target_goalies=0, target_skaters=2))
        self.api.select_roster("g1", ["junior", "other"])
        link = self.api.guardians.link_guardian("g1", "junior")
        self.api.guardians.verify_link(link.id)

    def test_home_lists_only_verified_juniors(self):
        home = self.api.get_guardian_home("g1")
        ids = [j["player_id"] for j in home["juniors"]]
        self.assertEqual(ids, ["junior"])

    def test_home_carries_full_player_surface_per_junior(self):
        home = self.api.get_guardian_home("g1")
        junior = home["juniors"][0]
        # The junior's own Player Home shape, reused verbatim.
        self.assertEqual(junior["player_name"], "Junior J.")
        self.assertIsNotNone(junior["next_game"])

    def test_home_excludes_unverified_links(self):
        self.api.guardians.link_guardian("g1", "other")  # unverified
        home = self.api.get_guardian_home("g1")
        ids = [j["player_id"] for j in home["juniors"]]
        self.assertEqual(ids, ["junior"])

    def test_home_empty_for_guardian_with_no_links(self):
        home = self.api.get_guardian_home("nobody")
        self.assertEqual(home["juniors"], [])


class GuardianHttpTest(unittest.TestCase):
    """Session identity scoping + per-action verified-link enforcement, on the
    demo store (which seeds a guardian verified to a junior via #26)."""

    @classmethod
    def setUpClass(cls):
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.junior_id = srv.STATE.ids["selected_player_id"]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()

    def _login(self, username):
        c = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        self._post(c, "/api/auth/login", {"username": username, "password": "demo"})
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

    # -- guardian home -----------------------------------------------------
    def test_guardian_home_lists_linked_junior(self):
        c = self._login("guardian")
        status, home = self._get(c, "/api/me/guardian/home")
        self.assertEqual(status, 200)
        ids = [j["player_id"] for j in home["juniors"]]
        self.assertIn(self.junior_id, ids)

    def test_guardian_home_requires_a_guardian_session(self):
        c = self._login("player")  # a player, not a guardian
        status, _ = self._get(c, "/api/me/guardian/home")
        self.assertEqual(status, 403)

    def test_guardian_home_unauthenticated_is_401(self):
        c = urllib.request.build_opener()  # no cookie
        status, _ = self._get(c, "/api/me/guardian/home")
        self.assertEqual(status, 401)

    # -- guardian acts for the linked junior -------------------------------
    def test_guardian_sets_attendance_for_linked_junior(self):
        c = self._login("guardian")
        status, av = self._post(
            c, f"/api/me/guardian/{self.junior_id}/games/"
               f"{srv.STATE.ids['game_id']}/availability",
            {"availability_status": "available"})
        self.assertEqual(status, 200)
        self.assertEqual(av["availability_status"], "available")
        self.assertEqual(av["response_source"], "guardian")

    def test_guardian_action_is_attributed_to_guardian_not_junior(self):
        c = self._login("guardian")
        gid = srv.STATE.ids["game_id"]
        self._post(c, f"/api/me/guardian/{self.junior_id}/games/{gid}/availability",
                   {"availability_status": "available"})
        audit = srv.STATE.api.store.audit_for_game(gid)
        setter = next(a for a in audit
                      if a.action.value == "availability_set"
                      and a.subject_player_id == self.junior_id
                      and a.detail.get("response_source") == "guardian")
        # WHO acted (guardian) is separate from WHOM it was for (junior).
        self.assertEqual(setter.actor_id, "user_guardian")
        self.assertEqual(setter.subject_player_id, self.junior_id)
        self.assertNotEqual(setter.actor_id, setter.subject_player_id)

    def test_guardian_cannot_act_for_an_unlinked_junior(self):
        c = self._login("guardian")
        gid = srv.STATE.ids["game_id"]
        # substitute_player_id is a different player the demo guardian has no
        # verified link to.
        stranger = srv.STATE.ids["substitute_player_id"]
        status, body = self._post(
            c, f"/api/me/guardian/{stranger}/games/{gid}/availability",
            {"availability_status": "available"})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")

    def test_non_guardian_cannot_use_guardian_action_route(self):
        c = self._login("coach")
        gid = srv.STATE.ids["game_id"]
        status, _ = self._post(
            c, f"/api/me/guardian/{self.junior_id}/games/{gid}/availability",
            {"availability_status": "available"})
        self.assertEqual(status, 403)

    def test_guardian_opportunity_detail_requires_the_link(self):
        c = self._login("guardian")
        gid = srv.STATE.ids["game_id"]
        stranger = srv.STATE.ids["substitute_player_id"]
        status, _ = self._get(
            c, f"/api/me/guardian/{stranger}/substitute-opportunities/{gid}")
        self.assertEqual(status, 403)

    def test_guardian_cannot_bypass_link_via_general_route(self):
        # The link check lives on /api/me/guardian/*. A guardian must NOT be
        # able to reach the general player/coach availability route and set a
        # player_id directly — even for their OWN linked junior — because that
        # route performs no link check. Otherwise the whole verified-link gate
        # is side-stepped. Both the linked junior and an unlinked stranger are
        # rejected here; the guardian's only sanctioned path is the dedicated
        # route family.
        c = self._login("guardian")
        gid = srv.STATE.ids["game_id"]
        for target in (self.junior_id, srv.STATE.ids["substitute_player_id"]):
            status, body = self._post(
                c, f"/api/games/{gid}/availability",
                {"player_id": target, "availability_status": "available"})
            self.assertEqual(status, 403)
            self.assertEqual(body["error"]["code"], "forbidden")

    # -- no PII leakage ----------------------------------------------------
    def test_guardian_home_exposes_no_guardian_pii(self):
        c = self._login("guardian")
        _status, home = self._get(c, "/api/me/guardian/home")
        # The payload is entirely player-scoped: the only guardian reference is
        # the opaque user id, never a name/email/phone.
        blob = json.dumps(home)
        self.assertNotIn("password", blob)
        for junior in home["juniors"]:
            self.assertNotIn("guardian_name", junior)
            self.assertNotIn("guardian_email", junior)
            self.assertNotIn("guardian_phone", junior)

    def test_coach_board_exposes_no_guardian_contact_details(self):
        # A guardian sets attendance for the junior; the coach-facing game
        # board attributes the action in its audit trail (by design — #26 wants
        # the acting guardian recorded), but only by the OPAQUE user id, never
        # any guardian contact detail — the guardian model holds no PII to leak.
        gc = self._login("guardian")
        gid = srv.STATE.ids["game_id"]
        self._post(gc, f"/api/me/guardian/{self.junior_id}/games/{gid}/availability",
                   {"availability_status": "available"})
        cc = self._login("coach")
        status, board = self._get(cc, f"/api/games/{gid}/board")
        self.assertEqual(status, 200)
        blob = json.dumps(board)
        for pii in ("guardian_name", "guardian_email", "guardian_phone",
                    "@", "password"):
            self.assertNotIn(pii, blob)


if __name__ == "__main__":
    unittest.main()
