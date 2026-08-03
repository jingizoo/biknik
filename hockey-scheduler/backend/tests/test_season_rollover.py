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
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Game, SeasonTeamRegistration
from hockey_scheduler.store import InMemoryStore
from hockey_scheduler.web.server import STATE, Handler

ADMIN = "setup_admin"


class SeasonRolloverServiceTest(unittest.TestCase):
    def setUp(self):
        self.api = ApiService(InMemoryStore())
        api = self.api
        self.league = api.create_program("L", actor_id=ADMIN)
        self.s1 = api.create_season(self.league["id"], "2026-27", actor_id=ADMIN)
        self.s2 = api.create_season(self.league["id"], "2027-28", actor_id=ADMIN)
        # #283: one permanent competition League spanning both seasons, so a
        # permanent team (which belongs to that League) can roll forward within
        # its own League from s1 into s2 rather than hitting the cross-league
        # guard that a per-season auto-provisioned league would trip.
        self.comp = api.create_league(self.s1["id"], "Comp", actor_id=ADMIN)
        self.d1 = api.create_division(self.s1["id"], "Div A",
                                      league_id=self.comp["id"], actor_id=ADMIN)
        self.d2 = api.create_division(self.s2["id"], "Div A",
                                      league_id=self.comp["id"], actor_id=ADMIN)
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
        ls = self.api.store.league_season_for(self.comp["id"], self.s1["id"])
        reg = SeasonTeamRegistration(
            id=self.api.store.next_id("streg"), league_season_id=ls.id,
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
        other = self.api.create_program("Other", actor_id=ADMIN)
        other_season = self.api.create_season(other["id"], "OS", actor_id=ADMIN)
        other_div = self.api.create_division(other_season["id"], "OD", actor_id=ADMIN)
        foreign = self.api.create_team(
            self.api.create_club("FC", actor_id=ADMIN)["id"],
            other_div["id"], "Foreign", actor_id=ADMIN)
        self.assertEqual(foreign["program_id"], other["id"])
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
        other = self.api.create_program("Other", actor_id=ADMIN)
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
            [{"team_id": 123}],                     # non-string (int) team_id
            [{"team_id": ["t1"]}],                  # unhashable (list) team_id
            [{"team_id": {"x": 1}}],                # unhashable (dict) team_id
            # valid team but unhashable division_id (would TypeError on de-dup)
            [{"team_id": self.lions["id"], "division_id": {"x": 1}}],
            [{"team_id": self.lions["id"], "division_id": ["d1"]}],
        ]
        audits_before = self._audit_count()
        for bad in malformed:
            res = self.api.roll_forward_registrations(
                self.s1["id"], self.s2["id"], selections=bad, actor_id=ADMIN)
            self.assertIn("error", res, f"expected an error for selections={bad!r}")
            self.assertEqual(res["error"]["code"], "validation_error",
                             f"expected validation_error for selections={bad!r}")
        # A non-string season id must also be a structured 400, not a TypeError
        # from store.get_season(dict.get(unhashable)).
        for bad_season in (["s1"], {"id": "s1"}, 123, ""):
            res = self.api.roll_forward_registrations(
                bad_season, self.s2["id"], actor_id=ADMIN)
            self.assertEqual(res["error"]["code"], "validation_error",
                             f"expected validation_error for from_season_id={bad_season!r}")
        # Every rejection was pre-write: target season empty, no audits written.
        self.assertEqual(self._active(self.s2["id"]), [])
        self.assertEqual(self._audit_count(), audits_before)

    def test_orphan_selected_team_is_rejected_by_the_gate(self):
        # A hand-picked team that IS active in the source season but whose Team
        # record is missing must be caught by the existence gate (not merely by
        # the source-membership check), on the selective path.
        self._inject_source_reg("team_ghost")
        audits_before = self._audit_count()
        res = self.api.roll_forward_registrations(
            self.s1["id"], self.s2["id"],
            selections=[{"team_id": "team_ghost"}], actor_id=ADMIN)
        self.assertEqual(res["error"]["code"], "validation_error")
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
        before = len(self.api.list_program_teams(self.league["id"])["teams"])
        self.api.roll_forward_registrations(self.s1["id"], self.s2["id"], actor_id=ADMIN)
        after = len(self.api.list_program_teams(self.league["id"])["teams"])
        self.assertEqual(before, after)  # rollover copies participation, not teams

    def test_idempotent_skip_when_already_registered(self):
        self.api.register_team_for_season(self.s2["id"], self.lions["id"], self.d2["id"], actor_id=ADMIN)
        res = self.api.roll_forward_registrations(self.s1["id"], self.s2["id"], actor_id=ADMIN)
        self.assertEqual(res["rolled_forward"], 1)  # only Bears
        self.assertEqual(res["skipped"], 1)         # Lions already there
        self.assertEqual(len(self._active(self.s2["id"])), 2)

    def test_cross_league_rollover_is_rejected(self):
        other = self.api.create_program("Other", actor_id=ADMIN)
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


class RolloverExactIdentityTest(unittest.TestCase):
    """#331 review round 18: roll_forward_registrations (v1) must resolve the
    target registration by exact (team, target LeagueSeason) identity --
    never the first Season-wide row ``registrations_for_season`` happens to
    return, which could cannibalize an inactive HISTORICAL row a transfer
    deliberately preserved, or collide with an already-correct different
    active one. Also covers the resolver's "move" branch: reusing a Team's
    sole other active (wrong-league) registration by moving it onto the
    correct target must never silently strand a committed game still
    referencing its CURRENT league.
    """

    def setUp(self):
        self.api = ApiService(InMemoryStore())
        api = self.api
        self.program = api.create_program("P", actor_id=ADMIN)
        self.s1 = api.create_season(self.program["id"], "S1", actor_id=ADMIN)
        self.s2 = api.create_season(self.program["id"], "S2", actor_id=ADMIN)
        self.l1 = api.create_league(self.s1["id"], "L1", actor_id=ADMIN)
        self.l2 = api.create_league(self.s1["id"], "L2", actor_id=ADMIN)
        self.d1s1 = api.create_division(self.s1["id"], "D1",
                                        league_id=self.l1["id"], actor_id=ADMIN)
        # Link both Leagues into the TARGET season up front (via a Division
        # in each) so `league_season_for` resolves without a rollover write
        # having to create the binding first.
        api.create_division(self.s2["id"], "D1", league_id=self.l1["id"],
                            actor_id=ADMIN)
        api.create_division(self.s2["id"], "D2", league_id=self.l2["id"],
                            actor_id=ADMIN)
        club = api.create_club("C", actor_id=ADMIN)
        self.team = api.create_team(club["id"], self.d1s1["id"], "Lions",
                                    actor_id=ADMIN)
        self.assertEqual(self.api.store.get_team(self.team["id"]).league_id,
                         self.l1["id"])
        api.register_team_for_season(self.s1["id"], self.team["id"],
                                     self.d1s1["id"], actor_id=ADMIN)

    def _inject_reg(self, league_id, season_id, active, division_id=None,
                    team_id=None):
        """Plant a SeasonTeamRegistration directly (bypassing the service's
        own Rule 7 guard) to reproduce legacy/historical drift no current
        write path can produce -- the same technique
        SeasonRolloverServiceTest._inject_source_reg uses."""
        ls = self.api.store.league_season_for(league_id, season_id)
        reg = SeasonTeamRegistration(
            id=self.api.store.next_id("streg"), league_season_id=ls.id,
            team_id=team_id or self.team["id"], division_id=division_id,
            active=active)
        self.api.store.add_season_team_registration(reg)
        return reg

    def _registration_audit_count(self):
        return len([a for a in self.api.store.all_setup_audit()
                   if a.action in ("season_team_registered",
                                   "season_team_registration_updated")])

    def _snapshot(self):
        return sorted(
            (r.id, r.team_id, r.league_season_id, r.division_id, r.active)
            for r in self.api.store.registrations_for_season(self.s2["id"]))

    # -- inactive-only sibling -> distinct active row ------------------------

    def test_inactive_sibling_creates_distinct_active_row(self):
        stale = self._inject_reg(self.l2["id"], self.s2["id"], active=False)
        res = self.api.roll_forward_registrations(
            self.s1["id"], self.s2["id"], actor_id=ADMIN)
        self.assertEqual(res["rolled_forward"], 1, res)
        active = [r for r in self.api.store.registrations_for_season(self.s2["id"])
                 if r.active]
        self.assertEqual(len(active), 1)
        ls1_s2 = self.api.store.league_season_for(self.l1["id"], self.s2["id"])
        self.assertEqual(active[0].league_season_id, ls1_s2.id)
        # The inactive sibling is byte-for-byte untouched.
        untouched = self.api.store.get_season_team_registration(stale.id)
        self.assertFalse(untouched.active)
        self.assertEqual(untouched.league_season_id, stale.league_season_id)

    # -- true no-op, both insertion orders -----------------------------------

    def test_true_noop_when_sibling_planted_before_active_target(self):
        self._inject_reg(self.l2["id"], self.s2["id"], active=False)
        self.api.roll_forward_registrations(
            self.s1["id"], self.s2["id"], actor_id=ADMIN)
        audits_before = self._registration_audit_count()
        before = self._snapshot()
        res = self.api.roll_forward_registrations(
            self.s1["id"], self.s2["id"], actor_id=ADMIN)
        self.assertEqual(res["rolled_forward"], 0, res)
        self.assertEqual(res["skipped"], 1, res)
        self.assertEqual(self._registration_audit_count(), audits_before)
        self.assertEqual(self._snapshot(), before)

    def test_true_noop_when_sibling_planted_after_active_target(self):
        # The active target row is created FIRST (lower id); the inactive
        # sibling is planted AFTERWARD (higher id) -- the resolver must find
        # the correct target by identity, not by picking whichever row has
        # the lowest/highest/first id.
        self.api.roll_forward_registrations(
            self.s1["id"], self.s2["id"], actor_id=ADMIN)
        active_before = next(
            r for r in self.api.store.registrations_for_season(self.s2["id"])
            if r.active)
        stale = self._inject_reg(self.l2["id"], self.s2["id"], active=False)
        self.assertLess(active_before.id, stale.id)
        audits_before = self._registration_audit_count()
        before = self._snapshot()
        res = self.api.roll_forward_registrations(
            self.s1["id"], self.s2["id"], actor_id=ADMIN)
        self.assertEqual(res["rolled_forward"], 0, res)
        self.assertEqual(res["skipped"], 1, res)
        self.assertEqual(self._registration_audit_count(), audits_before)
        self.assertEqual(self._snapshot(), before)

    # -- active-stale-row: move onto the correct target ----------------------

    def _seed_stale_active_row(self):
        """The Team's ONLY Season-2 registration is active under L2 (the
        WRONG league -- a Rule 7 violation legacy data or a stale write path
        could leave behind). Rolling the team's real permanent-league (L1)
        registration forward must MOVE this row onto L1 -- never skip it as
        "already registered" (it is registered, just under the wrong
        league) and never leave it stranded under L2."""
        return self._inject_reg(self.l2["id"], self.s2["id"], active=True)

    def test_moves_stale_active_row_onto_correct_target_when_game_free(self):
        stale = self._seed_stale_active_row()
        res = self.api.roll_forward_registrations(
            self.s1["id"], self.s2["id"], actor_id=ADMIN)
        self.assertEqual(res["rolled_forward"], 1, res)
        self.assertEqual(res["skipped"], 0, res)
        moved = self.api.store.get_season_team_registration(stale.id)
        self.assertTrue(moved.active)
        ls1_s2 = self.api.store.league_season_for(self.l1["id"], self.s2["id"])
        self.assertEqual(moved.league_season_id, ls1_s2.id)
        # No second row was created -- the SAME row moved in place.
        self.assertEqual(
            len(self.api.store.registrations_for_season(self.s2["id"])), 1)

    def test_moves_stale_active_row_when_only_cancelled_games_reference_it(self):
        stale = self._seed_stale_active_row()
        game = Game(id=self.api.store.next_id("game"),
                   home_team_id=self.team["id"], away_team_id=None,
                   start_time=datetime(2026, 9, 1, tzinfo=timezone.utc),
                   season_id=self.s2["id"], league_id=self.l2["id"],
                   cancelled=True)
        self.api.store.add_game(game)
        res = self.api.roll_forward_registrations(
            self.s1["id"], self.s2["id"], actor_id=ADMIN)
        self.assertEqual(res["rolled_forward"], 1, res)
        moved = self.api.store.get_season_team_registration(stale.id)
        ls1_s2 = self.api.store.league_season_for(self.l1["id"], self.s2["id"])
        self.assertEqual(moved.league_season_id, ls1_s2.id)

    def test_rejects_moving_stale_active_row_with_committed_game_zero_mutation(self):
        stale = self._seed_stale_active_row()
        game = Game(id=self.api.store.next_id("game"),
                   home_team_id=self.team["id"], away_team_id=None,
                   start_time=datetime(2026, 9, 1, tzinfo=timezone.utc),
                   season_id=self.s2["id"], league_id=self.l2["id"],
                   cancelled=False)
        self.api.store.add_game(game)
        audits_before = self._registration_audit_count()
        before = self._snapshot()
        res = self.api.roll_forward_registrations(
            self.s1["id"], self.s2["id"], actor_id=ADMIN)
        self.assertEqual(res["error"]["code"], "validation_error", res)
        self.assertEqual(res["error"]["details"]["reason"],
                         "registration_league_change_strands_games")
        self.assertEqual(res["error"]["details"]["affected_game_ids"], [game.id])
        self.assertEqual(self._registration_audit_count(), audits_before)
        self.assertEqual(self._snapshot(), before)
        still_stale = self.api.store.get_season_team_registration(stale.id)
        self.assertTrue(still_stale.active)
        self.assertEqual(still_stale.league_season_id, stale.league_season_id)

    # -- two-active-row conflict, both orders --------------------------------

    def _seed_two_active_conflicts(self, first_name, second_name):
        """Neither of the two rows sits at the team's OWN permanent League
        (L1) -- both are "other active" candidates the resolver must treat
        as an unresolvable conflict, regardless of which was planted (and so
        got the lower id) first."""
        first = self.api.create_league(self.s1["id"], first_name, actor_id=ADMIN)
        second = self.api.create_league(self.s1["id"], second_name, actor_id=ADMIN)
        self.api.create_division(self.s2["id"], first_name + " Div",
                                 league_id=first["id"], actor_id=ADMIN)
        self.api.create_division(self.s2["id"], second_name + " Div",
                                 league_id=second["id"], actor_id=ADMIN)
        reg_first = self._inject_reg(first["id"], self.s2["id"], active=True)
        reg_second = self._inject_reg(second["id"], self.s2["id"], active=True)
        return reg_first, reg_second

    def _assert_two_active_conflict_rejected(self, reg_first, reg_second):
        audits_before = self._registration_audit_count()
        before = self._snapshot()
        res = self.api.roll_forward_registrations(
            self.s1["id"], self.s2["id"], actor_id=ADMIN)
        self.assertEqual(res["error"]["code"], "validation_error", res)
        self.assertEqual(res["error"]["details"]["reason"],
                         "team_registration_conflict")
        self.assertEqual(
            set(res["error"]["details"]["affected_registration_ids"]),
            {reg_first.id, reg_second.id})
        self.assertEqual(self._registration_audit_count(), audits_before)
        self.assertEqual(self._snapshot(), before)

    def test_rejects_two_active_rows_forward_insertion_order(self):
        reg_first, reg_second = self._seed_two_active_conflicts(
            "League X", "League Y")
        self._assert_two_active_conflict_rejected(reg_first, reg_second)

    def test_rejects_two_active_rows_reverse_insertion_order(self):
        # Swap creation order so the SAME assertion holds regardless of which
        # stray row happens to sort/iterate first -- the old
        # next(registrations_for_season(...)) lookup picked whichever came
        # first, silently treating the OTHER as if it didn't exist.
        reg_second, reg_first = self._seed_two_active_conflicts(
            "League Y", "League X")
        self._assert_two_active_conflict_rejected(reg_first, reg_second)


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
        cls.httpd.server_close()

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

    def test_malformed_body_returns_400_not_500(self):
        # Req #4 is a transport guarantee: malformed HTTP input must map to a
        # structured 400, never a 500 from an uncaught TypeError/AttributeError.
        # Exercise the full route -> facade -> ERROR_HTTP_STATUS path.
        _, league = self._post("/api/setup/league", {"name": "ML"}, "league_admin")
        _, s1 = self._post("/api/setup/season", {"league_id": league["id"], "name": "A"}, "league_admin")
        _, s2 = self._post("/api/setup/season", {"league_id": league["id"], "name": "B"}, "league_admin")
        _, d1 = self._post("/api/setup/division", {"season_id": s1["id"], "name": "D"}, "league_admin")
        _, club = self._post("/api/setup/club", {"name": "C"}, "league_admin")
        _, team = self._post("/api/setup/team",
                             {"club_id": club["id"], "division_id": d1["id"], "name": "T"}, "league_admin")
        self._post(f"/api/setup/seasons/{s1['id']}/team-registrations",
                   {"team_id": team["id"], "division_id": d1["id"]}, "league_admin")
        rf = f"/api/setup/seasons/{s2['id']}/roll-forward"
        for body in (
            {"from_season_id": s1["id"], "selections": "not-a-list"},
            {"from_season_id": s1["id"], "selections": [{"team_id": ["t1"]}]},
            {"from_season_id": s1["id"], "selections": [{"team_id": team["id"],
                                                         "division_id": {"x": 1}}]},
            {"from_season_id": ["x"]},               # unhashable season id
        ):
            status, resp = self._post(rf, body, "league_admin")
            self.assertEqual(status, 400, f"expected 400 for body={body!r}, got {status}")
            self.assertEqual(resp["error"]["code"], "validation_error")


if __name__ == "__main__":
    unittest.main()
