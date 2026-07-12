"""Permanent teams + season-specific registrations (#180 PR A, foundation).

A team is now a permanent member of a league; its participation in a given
season — including that season's division/group — lives in a
SeasonTeamRegistration rather than on the Team. These tests cover the model,
the registration lifecycle service, persistence round-trip, and the
deterministic backfill migration. Scheduling/standings still read the legacy
Team.division_id in this slice; wiring games to registrations is a follow-up.
"""

import os
import tempfile
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.store import InMemoryStore, SqlStore

ADMIN = "setup_admin"


def _fixture(api, league_name="Over 55"):
    """A league with one season and one division, plus a club — the minimum to
    create a team and register it. Returns ids."""
    league = api.create_league(league_name, actor_id=ADMIN)
    season = api.create_season(league["id"], "Fall 2026", actor_id=ADMIN)
    division = api.create_division(season["id"], "Div A", actor_id=ADMIN)
    club = api.create_club("Club X", actor_id=ADMIN)
    return league, season, division, club


class TeamLeagueMembershipTest(unittest.TestCase):
    def setUp(self):
        self.api = ApiService(InMemoryStore())

    def test_create_team_derives_permanent_league_from_division(self):
        league, season, division, club = _fixture(self.api)
        team = self.api.create_team(club["id"], division["id"], "Lions",
                                    actor_id=ADMIN)
        self.assertEqual(team["league_id"], league["id"])

    def test_teams_for_league_lists_permanent_members(self):
        league, season, division, club = _fixture(self.api)
        self.api.create_team(club["id"], division["id"], "Lions", actor_id=ADMIN)
        self.api.create_team(club["id"], division["id"], "Falcons", actor_id=ADMIN)
        names = [t["name"]
                 for t in self.api.list_league_teams(league["id"])["teams"]]
        self.assertEqual(sorted(names), ["Falcons", "Lions"])

    def test_create_team_under_league_without_a_division(self):
        # #180 UI correction: a team is created under the LEAGUE directly; its
        # season/division comes later via registration, not at creation.
        league, season, division, club = _fixture(self.api)
        team = self.api.create_team(club["id"], name="Bears",
                                    league_id=league["id"], actor_id=ADMIN)
        self.assertEqual(team["league_id"], league["id"])
        self.assertIsNone(team["division_id"])

    def test_create_team_needs_a_league_or_division(self):
        _, _, _, club = _fixture(self.api)
        res = self.api.create_team(club["id"], name="Nowhere", actor_id=ADMIN)
        self.assertEqual(res["error"]["code"], "validation_error")

    def test_create_team_rejects_division_from_a_different_league(self):
        league_a, _, div_a, club = _fixture(self.api, "League A")
        league_b = self.api.create_league("League B", actor_id=ADMIN)
        res = self.api.create_team(club["id"], div_a["id"], "Mismatch",
                                   league_id=league_b["id"], actor_id=ADMIN)
        self.assertEqual(res["error"]["code"], "validation_error")


class SeasonRegistrationServiceTest(unittest.TestCase):
    def setUp(self):
        self.api = ApiService(InMemoryStore())
        self.league, self.season, self.division, self.club = _fixture(self.api)
        self.team = self.api.create_team(self.club["id"], self.division["id"],
                                         "Lions", actor_id=ADMIN)

    def _register(self, **kw):
        return self.api.register_team_for_season(
            kw.get("season_id", self.season["id"]),
            kw.get("team_id", self.team["id"]),
            kw.get("division_id", self.division["id"]), actor_id=ADMIN)

    def test_register_existing_team_without_duplicating_it(self):
        reg = self._register()
        self.assertNotIn("error", reg)
        self.assertEqual(reg["team_id"], self.team["id"])
        self.assertEqual(reg["division_id"], self.division["id"])
        self.assertTrue(reg["active"])
        # The Team was not duplicated by registering it.
        self.assertEqual(
            len(self.api.list_league_teams(self.league["id"])["teams"]), 1)

    def test_duplicate_registration_in_same_season_is_rejected(self):
        self._register()
        dup = self._register()
        self.assertEqual(dup["error"]["code"], "validation_error")

    def test_cross_league_registration_is_rejected(self):
        other_league = self.api.create_league("Other", actor_id=ADMIN)
        other_season = self.api.create_season(other_league["id"], "S", actor_id=ADMIN)
        reg = self.api.register_team_for_season(
            other_season["id"], self.team["id"], actor_id=ADMIN)
        self.assertEqual(reg["error"]["code"], "validation_error")

    def test_division_from_another_season_is_rejected(self):
        other_season = self.api.create_season(self.league["id"], "Spring",
                                              actor_id=ADMIN)
        other_div = self.api.create_division(other_season["id"], "Div Z",
                                             actor_id=ADMIN)
        reg = self.api.register_team_for_season(
            self.season["id"], self.team["id"], other_div["id"], actor_id=ADMIN)
        self.assertEqual(reg["error"]["code"], "validation_error")

    def test_same_team_different_division_across_seasons(self):
        season2 = self.api.create_season(self.league["id"], "Fall 2027",
                                         actor_id=ADMIN)
        div_b = self.api.create_division(season2["id"], "Div B", actor_id=ADMIN)
        reg1 = self._register()
        reg2 = self.api.register_team_for_season(
            season2["id"], self.team["id"], div_b["id"], actor_id=ADMIN)
        self.assertNotEqual(reg1["id"], reg2["id"])
        self.assertEqual(reg1["division_id"], self.division["id"])
        self.assertEqual(reg2["division_id"], div_b["id"])

    def test_reassign_division_updates_same_registration(self):
        div_b = self.api.create_division(self.season["id"], "Div B", actor_id=ADMIN)
        reg = self._register()
        moved = self.api.assign_season_team_division(reg["id"], div_b["id"],
                                                     actor_id=ADMIN)
        self.assertEqual(moved["id"], reg["id"])
        self.assertEqual(moved["division_id"], div_b["id"])
        # Still exactly one registration for the team this season.
        rows = self.api.list_season_team_registrations(self.season["id"])
        self.assertEqual(len(rows["registrations"]), 1)

    def test_unregister_keeps_team_and_allows_reactivation(self):
        reg = self._register()
        removed = self.api.unregister_team_from_season(reg["id"], actor_id=ADMIN)
        self.assertFalse(removed["active"])
        # The permanent Team survives removal from the season.
        self.assertEqual(
            len(self.api.list_league_teams(self.league["id"])["teams"]), 1)
        # Re-registering reactivates the same row rather than duplicating it.
        again = self._register()
        self.assertEqual(again["id"], reg["id"])
        self.assertTrue(again["active"])
        self.assertEqual(
            len(self.api.store.registrations_for_season(self.season["id"])), 1)

    def test_registration_and_reassignment_are_audited_with_actor(self):
        div_b = self.api.create_division(self.season["id"], "Div B", actor_id=ADMIN)
        reg = self._register()
        self.api.assign_season_team_division(reg["id"], div_b["id"], actor_id=ADMIN)
        audit = self.api.store.all_setup_audit()
        actions = {a.action: a for a in audit}
        self.assertIn("season_team_registered", actions)
        assigned = actions["season_team_division_assigned"]
        self.assertEqual(assigned.detail["from"], self.division["id"])
        self.assertEqual(assigned.detail["to"], div_b["id"])
        self.assertEqual(assigned.actor_id, ADMIN)


class SeasonRegistrationPersistenceTest(unittest.TestCase):
    def _durable(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.remove, path)
        return path

    def test_registration_and_league_membership_survive_reopen(self):
        path = self._durable()
        api = ApiService(SqlStore(path))
        league, season, division, club = _fixture(api)
        team = api.create_team(club["id"], division["id"], "Lions", actor_id=ADMIN)
        reg = api.register_team_for_season(season["id"], team["id"],
                                           division["id"], actor_id=ADMIN)
        del api

        reopened = SqlStore(path)
        self.assertEqual(reopened.get_team(team["id"]).league_id, league["id"])
        persisted = reopened.get_season_team_registration(reg["id"])
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.team_id, team["id"])
        self.assertEqual(persisted.division_id, division["id"])
        self.assertTrue(persisted.active)

    def test_legacy_team_backfills_league_and_one_registration(self):
        # A pre-#180 database has a team under a division but no league_id and no
        # registration. Re-running migration 021 must derive the league and
        # create exactly one registration, deterministically.
        path = self._durable()
        api = ApiService(SqlStore(path))
        league, season, division, club = _fixture(api)
        team = api.create_team(club["id"], division["id"], "Lions", actor_id=ADMIN)
        store = api.store
        cur = store.conn.cursor()
        # Simulate the legacy shape: a pre-#180 team carried a Team.division_id
        # (create_team no longer writes one). Set it directly so migration 021
        # has the legacy link to backfill league_id + a registration from.
        cur.execute("UPDATE teams SET division_id = ? WHERE id = ?",
                    (division["id"], team["id"]))
        # Then drop the #180 additions and their ledger entry, and reopen so
        # migration 021 re-runs its backfill over the legacy team/division rows.
        cur.execute("DROP INDEX IF EXISTS ix_teams_league")
        cur.execute("ALTER TABLE teams DROP COLUMN league_id")
        cur.execute("DROP TABLE IF EXISTS season_team_registrations")
        cur.execute("DELETE FROM schema_migrations WHERE version = "
                    "'021_permanent_teams'")
        store.conn.commit()
        del api, store

        reopened = SqlStore(path)
        self.assertEqual(reopened.get_team(team["id"]).league_id, league["id"])
        regs = reopened.registrations_for_season(season["id"])
        self.assertEqual(len(regs), 1)
        self.assertEqual(regs[0].team_id, team["id"])
        self.assertEqual(regs[0].division_id, division["id"])


if __name__ == "__main__":
    unittest.main()
