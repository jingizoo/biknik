"""Registration safety + actor integrity for season team registrations (#180 PR C).

Removing a team from a season, or changing its season division, must not
silently invalidate games already scheduled for that team — the service refuses
and returns the affected game ids so an operator can resolve them first (issue
rule 6 + service safety rules). Also proves the setup routes attribute changes
to the authenticated session, never a client-supplied actor_id.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.store import InMemoryStore
from hockey_scheduler.web.server import STATE, Handler

ADMIN = "setup_admin"


class RegistrationSafetyTest(unittest.TestCase):
    def setUp(self):
        self.api = ApiService(InMemoryStore())
        api = self.api
        self.league = api.create_program("L", actor_id=ADMIN)
        self.season = api.create_season(self.league["id"], "S", actor_id=ADMIN)
        self.division = api.create_division(self.season["id"], "D", actor_id=ADMIN)
        club = api.create_club("C", actor_id=ADMIN)
        self.team_a = api.create_team(club["id"], self.division["id"], "A",
                                      actor_id=ADMIN)
        self.team_b = api.create_team(club["id"], self.division["id"], "B",
                                      actor_id=ADMIN)
        # A venue tied to the same league so the #173 ice-isolation guard is
        # satisfied when the game is scheduled onto its ice.
        venue = api.create_venue("V", league_id=self.league["id"], actor_id=ADMIN)
        api.grant_season_venue_access(self.season["id"], venue["id"], actor_id=ADMIN)
        rink = api.create_rink(venue["id"], "R", actor_id=ADMIN)
        self.slot = api.create_ice_slot(
            rink["id"], "2026-09-01T18:30:00+00:00",
            "2026-09-01T20:00:00+00:00", actor_id=ADMIN)
        self.reg_a = api.register_team_for_season(
            self.season["id"], self.team_a["id"], self.division["id"], actor_id=ADMIN)
        api.register_team_for_season(
            self.season["id"], self.team_b["id"], self.division["id"], actor_id=ADMIN)

    def _schedule_game(self):
        game = self.api.create_game(
            self.season["id"], self.division["id"], self.team_a["id"],
            self.team_b["id"], self.slot["id"], actor_id=ADMIN)
        self.assertNotIn("error", game, game)
        return game

    def test_unregister_blocked_by_scheduled_game_reports_affected_ids(self):
        game = self._schedule_game()
        result = self.api.unregister_team_from_season(self.reg_a["id"],
                                                      actor_id=ADMIN)
        self.assertEqual(result["error"]["code"], "validation_error")
        details = result["error"]["details"]
        self.assertEqual(details["reason"], "team_has_scheduled_games")
        self.assertIn(game["id"], details["affected_game_ids"])
        self.assertEqual(details["count"], 1)
        # No partial write — the registration is still active.
        self.assertTrue(
            self.api.store.get_season_team_registration(self.reg_a["id"]).active)

    def test_reassign_division_blocked_by_scheduled_game(self):
        self._schedule_game()
        div_b = self.api.create_division(self.season["id"], "D2", actor_id=ADMIN)
        result = self.api.assign_season_team_division(
            self.reg_a["id"], div_b["id"], actor_id=ADMIN)
        self.assertEqual(result["error"]["code"], "validation_error")
        self.assertEqual(result["error"]["details"]["reason"],
                         "team_has_scheduled_games")
        # Division unchanged.
        self.assertEqual(
            self.api.store.get_season_team_registration(self.reg_a["id"]).division_id,
            self.division["id"])

    def test_unregister_succeeds_after_the_game_is_cancelled(self):
        game = self._schedule_game()
        self.api.cancel_game(game["id"], actor_id=ADMIN)
        result = self.api.unregister_team_from_season(self.reg_a["id"],
                                                      actor_id=ADMIN)
        self.assertNotIn("error", result)
        self.assertFalse(result["active"])


class RegistrationActorIntegrityHttpTest(unittest.TestCase):
    """A forged body actor_id must be ignored — the audit records the session
    actor (the #136 actor-forgery guarantee), even on the new routes."""

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

    def _post(self, path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(),
            method="POST", headers={"Content-Type": "application/json",
                                    "X-Demo-Role": "league_admin"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_forged_actor_id_in_body_is_ignored(self):
        _, league = self._post("/api/setup/league", {"name": "Actor League"})
        _, season = self._post("/api/setup/season",
                               {"league_id": league["id"], "name": "S"})
        _, division = self._post("/api/setup/division",
                                {"season_id": season["id"], "name": "D"})
        _, club = self._post("/api/setup/club", {"name": "C"})
        _, team = self._post("/api/setup/team",
                            {"club_id": club["id"], "division_id": division["id"],
                             "name": "A"})
        # The registration route now carries a strict write schema (#271): a
        # forged top-level actor_id is an unknown field and is rejected outright
        # (400) rather than silently ignored — an even stronger guarantee that
        # it can never reach the audit trail, and no registration is written.
        audits_before = len(STATE.api.store.all_setup_audit())
        status, body = self._post(
            f"/api/setup/seasons/{season['id']}/team-registrations",
            {"team_id": team["id"], "division_id": division["id"],
             "actor_id": "hacker"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"]["reason"], "unknown_field")
        self.assertIn("actor_id", body["error"]["details"]["fields"])
        # No registration and no audit row were written.
        self.assertEqual(len(STATE.api.store.all_setup_audit()), audits_before)


if __name__ == "__main__":
    unittest.main()
