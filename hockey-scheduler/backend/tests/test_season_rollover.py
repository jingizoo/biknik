"""Season rollover copy-forward (#180 PR E).

When a new season starts, an operator carries the prior season's participation
forward — the same permanent teams, into the new season's divisions — without
recreating Team records or touching the prior season. These tests cover the
service (copy-all and selective, cross-league/wrong-division rejection,
idempotent skip, Team reuse, audit), the #197 corrective integrity gate
(orphan / cross-league source teams and malformed selections rejected before
any write), and the HTTP route's authorization.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import SeasonTeamRegistration
from hockey_scheduler.store import InMemoryStore
from hockey_scheduler.web.server import STATE, Handler

ADMIN = "setup_admin"


class SeasonRolloverServiceTest(unittest.TestCase):
    def setUp(self):
        self.api = ApiService(InMemoryStore())
        api = self.api
        self.league = api.create_league("L", actor_id=ADMIN)
        self.s1 = api.create_season(self.league["id"], "2026-27", actor_id=ADMIN)
        self.s2 = api.create_season(self.league["id"], "2027-28", actor_id=ADMIN)
        self.d1 = api.create_division(self.s1["id"], "Div A", actor_id=ADMIN)
        self.d2 = api.create_division(self.s2["id"], "Div A", actor_id=ADMIN)
        club = api.create_club("C", actor_id=ADMIN)
        self.lions = api.create_team(club["id"], self.d1["id"], "Lions", actor_id=ADMIN)
        self.bears = api.create_team(club["id"], self.d1["id"], "Bears", actor_id=ADMIN)
        api.register_team_for_season(self.s1["id"], self.lions["id"], self.d1["id"], actor_id=ADMIN)
        api.register_team_for_season(self.s1["id"], self.bears["id"], self.d1["id"], actor_id=ADMIN)

    def _active(self, season_id):
        return [r for r in self.api.store.registrations_for_season(season_id) if r.active]

    def _audit_count(self):
        return len(self.api.store.all_setup_audit())

    def _inject_source_reg(self, team_id, division_id=None):
        """Force an inconsistent participation row into the source season.

        The rollover gate exists precisely because the registration store can
        hold legacy/orphaned/cross-league rows that never passed
        ``register_team_for_season``'s own guard. These rows can't be created
        through the service (it would reject them), so the tests inject them
        directly to reproduce the data the gate must defend against.
        """
        reg = SeasonTeamRegistration(
            id=self.api.store.next_id("streg"), season_id=self.s1["id"],
            team_id=team_id, division_id=division_id, active=True)
        self.api.store.add_season_team_registration(reg)
        return reg

    # -- #197 corrective gate: orphan / cross-league / malformed input -----
    # roll_forward_registrations previously trusted source registration
    # team_ids without resolving the Team or checking its league, and threw an
    # AttributeError on malformed selections. Every failure below must be a
    # structured validation error that writes zero registrations and zero
    # audits (the store transaction is a lock, not a rollback, so the gate has
    # to run entirely before the first write).

    def test_orphan_source_registration_is_rejected_with_no_writes(self):
        # A source row pointing at a Team that no longer exists must abort the
        # whole batch — not silently materialize a registration for a ghost.
        self._inject_source_reg("team_ghost")
        audits_before = self._audit_count()
        res = self.api.roll_forward_registrations(
            self.s1["id"], self.s2["id"], actor_id=ADMIN)
        self.assertEqual(res["error"]["code"], "validation_error")
        # All-or-nothing: the valid Lions/Bears were NOT written either.
        self.assertEqual(self._active(self.s2["id"]), [])
        self.assertEqual(self._audit_count(), audits_before)

    def test_cross_league_source_team_is_rejected_with_no_writes(self):
        # A source row whose Team permanently belongs to another league must
        # abort rather than carry a foreign team into this league's season.
        other = self.api.create_league("Other", actor_id=ADMIN)
        other_season = self.api.create_season(other["id"], "OS", actor_id=ADMIN)
        other_div = self.api.create_division(other_season["id"], "OD", actor_id=ADMIN)
        foreign = self.api.create_team(
            self.api.create_club("FC", actor_id=ADMIN)["id"],
            other_div["id"], "Foreign", actor_id=ADMIN)
        self.assertEqual(foreign["league_id"], other["id"])
        self._inject_source_reg(foreign["id"])
        audits_before = self._audit_count()
        res = self.api.roll_forward_registrations(
            self.s1["id"], self.s2["id"], actor_id=ADMIN)
        self.assertEqual(res["error"]["code"], "validation_error")
        self.assertEqual(self._active(self.s2["id"]), [])
        self.assertEqual(self._audit_count(), audits_before)

    def test_cross_league_team_via_selection_is_rejected(self):
        # Same defense on the selective path: a hand-picked team whose Team is
        # cross-league is rejected before any write.
        other = self.api.create_league("Other", actor_id=ADMIN)
        other_season = self.api.create_season(other["id"], "OS", actor_id=ADMIN)
        other_div = self.api.create_division(other_season["id"], "OD", actor_id=ADMIN)
        foreign = self.api.create_team(
            self.api.create_club("FC", actor_id=ADMIN)["id"],
            other_div["id"], "Foreign", actor_id=ADMIN)
        self._inject_source_reg(foreign["id"])
        audits_before = self._audit_count()
        res = self.api.roll_forward_registrations(
            self.s1["id"], self.s2["id"],
            selections=[{"team_id": foreign["id"]}], actor_id=ADMIN)
        self.assertEqual(res["error"]["code"], "validation_error")
        self.assertEqual(self._active(self.s2["id"]), [])
        self.assertEqual(self._audit_count(), audits_before)

    def test_malformed_selections_return_structured_validation_errors(self):
        # Each malformed shape must return a structured validation_error rather
        # than raise an AttributeError (which catch does not translate → 500).
        malformed = [
            "not-a-list",                          # not a list at all
            {"team_id": self.lions["id"]},          # a bare object, not a list
            ["just-a-string"],                      # element is not an object
            [None],                                 # element is None
            [{"division_id": self.d2["id"]}],       # missing team_id
            [{"team_id": ""}],                      # blank team_id
            [{"team_id": "   "}],                   # whitespace-only team_id
            [{"team_id": None}],                    # null team_id
        ]
        audits_before = self._audit_count()
        for bad in malformed:
            res = self.api.roll_forward_registrations(
                self.s1["id"], self.s2["id"], selections=bad, actor_id=ADMIN)
            self.assertIn("error", res, f"expected an error for selections={bad!r}")
            self.assertEqual(res["error"]["code"], "validation_error",
                             f"expected validation_error for selections={bad!r}")
        # Every rejection was pre-write: target season empty, no audits written.
        self.assertEqual(self._active(self.s2["id"]), [])
        self.assertEqual(self._audit_count(), audits_before)

    def test_copy_all_forward_without_division(self):
        res = self.api.roll_forward_registrations(self.s1["id"], self.s2["id"], actor_id=ADMIN)
        self.assertEqual(res["rolled_forward"], 2)
        self.assertEqual(res["skipped"], 0)
        teams = {r["team_id"] for r in res["registrations"]}
        self.assertEqual(teams, {self.lions["id"], self.bears["id"]})
        # Carried with no division (operator assigns later); source untouched.
        self.assertTrue(all(r["division_id"] is None for r in res["registrations"]))
        self.assertEqual(len(self._active(self.s1["id"])), 2)

    def test_selective_forward_with_target_division(self):
        res = self.api.roll_forward_registrations(
            self.s1["id"], self.s2["id"],
            selections=[{"team_id": self.lions["id"], "division_id": self.d2["id"]}],
            actor_id=ADMIN)
        self.assertEqual(res["rolled_forward"], 1)
        self.assertEqual(res["registrations"][0]["team_id"], self.lions["id"])
        self.assertEqual(res["registrations"][0]["division_id"], self.d2["id"])
        self.assertEqual(len(self._active(self.s2["id"])), 1)

    def test_reuses_permanent_team_records(self):
        before = len(self.api.list_league_teams(self.league["id"])["teams"])
        self.api.roll_forward_registrations(self.s1["id"], self.s2["id"], actor_id=ADMIN)
        after = len(self.api.list_league_teams(self.league["id"])["teams"])
        self.assertEqual(before, after)  # rollover copies participation, not teams

    def test_idempotent_skip_when_already_registered(self):
        self.api.register_team_for_season(self.s2["id"], self.lions["id"], self.d2["id"], actor_id=ADMIN)
        res = self.api.roll_forward_registrations(self.s1["id"], self.s2["id"], actor_id=ADMIN)
        self.assertEqual(res["rolled_forward"], 1)  # only Bears
        self.assertEqual(res["skipped"], 1)         # Lions already there
        self.assertEqual(len(self._active(self.s2["id"])), 2)

    def test_cross_league_rollover_is_rejected(self):
        other = self.api.create_league("Other", actor_id=ADMIN)
        other_season = self.api.create_season(other["id"], "X", actor_id=ADMIN)
        res = self.api.roll_forward_registrations(self.s1["id"], other_season["id"], actor_id=ADMIN)
        self.assertEqual(res["error"]["code"], "validation_error")

    def test_target_division_from_wrong_season_is_rejected(self):
        res = self.api.roll_forward_registrations(
            self.s1["id"], self.s2["id"],
            selections=[{"team_id": self.lions["id"], "division_id": self.d1["id"]}],
            actor_id=ADMIN)
        self.assertEqual(res["error"]["code"], "validation_error")
        # Rejected before any write — target season still empty.
        self.assertEqual(len(self._active(self.s2["id"])), 0)

    def test_selecting_a_team_not_in_source_is_rejected(self):
        stray = self.api.create_team(self.api.create_club("C2", actor_id=ADMIN)["id"],
                                     self.d1["id"], "Stray", actor_id=ADMIN)
        res = self.api.roll_forward_registrations(
            self.s1["id"], self.s2["id"],
            selections=[{"team_id": stray["id"]}], actor_id=ADMIN)
        self.assertEqual(res["error"]["code"], "validation_error")

    def test_rollover_is_audited_with_actor_and_source(self):
        self.api.roll_forward_registrations(self.s1["id"], self.s2["id"], actor_id=ADMIN)
        actions = {a.action: a for a in self.api.store.all_setup_audit()}
        batch = actions["season_registrations_rolled_forward"]
        self.assertEqual(batch.detail["from_season_id"], self.s1["id"])
        self.assertEqual(batch.detail["rolled_forward"], 2)
        self.assertEqual(batch.actor_id, ADMIN)


class SeasonRolloverHttpTest(unittest.TestCase):
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

    def _post(self, path, body, role):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(),
            method="POST", headers={"Content-Type": "application/json",
                                    "X-Demo-Role": role})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_admin_can_roll_forward_and_viewer_cannot(self):
        _, league = self._post("/api/setup/league", {"name": "RL"}, "league_admin")
        _, s1 = self._post("/api/setup/season", {"league_id": league["id"], "name": "A"}, "league_admin")
        _, s2 = self._post("/api/setup/season", {"league_id": league["id"], "name": "B"}, "league_admin")
        _, d1 = self._post("/api/setup/division", {"season_id": s1["id"], "name": "D"}, "league_admin")
        _, club = self._post("/api/setup/club", {"name": "C"}, "league_admin")
        _, team = self._post("/api/setup/team",
                            {"club_id": club["id"], "division_id": d1["id"], "name": "T"}, "league_admin")
        self._post(f"/api/setup/seasons/{s1['id']}/team-registrations",
                   {"team_id": team["id"], "division_id": d1["id"]}, "league_admin")
        # Viewer is forbidden.
        status, body = self._post(f"/api/setup/seasons/{s2['id']}/roll-forward",
                                  {"from_season_id": s1["id"]}, "viewer")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["details"]["required"], "manage_setup")
        # League Admin succeeds.
        status, res = self._post(f"/api/setup/seasons/{s2['id']}/roll-forward",
                                 {"from_season_id": s1["id"]}, "league_admin")
        self.assertEqual(status, 200)
        self.assertEqual(res["rolled_forward"], 1)


if __name__ == "__main__":
    unittest.main()
