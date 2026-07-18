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
    league = api.create_program(league_name, actor_id=ADMIN)
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
        self.assertEqual(team["program_id"], league["id"])

    def test_teams_for_league_lists_permanent_members(self):
        league, season, division, club = _fixture(self.api)
        self.api.create_team(club["id"], division["id"], "Lions", actor_id=ADMIN)
        self.api.create_team(club["id"], division["id"], "Falcons", actor_id=ADMIN)
        names = [t["name"]
                 for t in self.api.list_program_teams(league["id"])["teams"]]
        self.assertEqual(sorted(names), ["Falcons", "Lions"])

    def test_create_team_under_league_without_a_division(self):
        # #180 UI correction: a team is created under the PROGRAM directly; its
        # season/division comes later via registration, not at creation.
        league, season, division, club = _fixture(self.api)
        team = self.api.create_team(club["id"], name="Bears",
                                    program_id=league["id"], actor_id=ADMIN)
        self.assertEqual(team["program_id"], league["id"])
        self.assertIsNone(team["division_id"])

    def test_create_team_needs_a_league_or_division(self):
        _, _, _, club = _fixture(self.api)
        res = self.api.create_team(club["id"], name="Nowhere", actor_id=ADMIN)
        self.assertEqual(res["error"]["code"], "validation_error")

    def test_create_team_rejects_division_from_a_different_league(self):
        league_a, _, div_a, club = _fixture(self.api, "League A")
        league_b = self.api.create_program("League B", actor_id=ADMIN)
        res = self.api.create_team(club["id"], div_a["id"], "Mismatch",
                                   program_id=league_b["id"], actor_id=ADMIN)
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
            len(self.api.list_program_teams(self.league["id"])["teams"]), 1)

    def test_duplicate_registration_in_same_season_is_rejected(self):
        self._register()
        dup = self._register()
        self.assertEqual(dup["error"]["code"], "validation_error")

    def test_cross_league_registration_is_rejected(self):
        other_league = self.api.create_program("Other", actor_id=ADMIN)
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
        # #283: a Team belongs to one permanent League, so its Division in
        # another season must live under that SAME League's participation
        # there (an auto-provisioned league would be a different League and
        # the team could not register into it — rule 7).
        team_league = self.api.store.get_team(self.team["id"]).league_id
        div_b = self.api.create_division(season2["id"], "Div B",
                                         league_id=team_league, actor_id=ADMIN)
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
            len(self.api.list_program_teams(self.league["id"])["teams"]), 1)
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
        self.assertEqual(reopened.get_team(team["id"]).program_id, league["id"])
        persisted = reopened.get_season_team_registration(reg["id"])
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.team_id, team["id"])
        self.assertEqual(persisted.division_id, division["id"])
        self.assertTrue(persisted.active)

    def test_legacy_team_backfills_program_and_one_registration(self):
        # A pre-#180 database has a team under a division but no permanent
        # program and no registration. Reversing the #233 competition-model
        # rename back to the pre-028 shape and re-running migrations 021
        # (permanent teams) then 028 (competition reset) must derive the team's
        # program and create exactly one registration, deterministically.
        path = self._durable()
        api = ApiService(SqlStore(path))
        league, season, division, club = _fixture(api)
        team = api.create_team(club["id"], division["id"], "Lions", actor_id=ADMIN)
        store = api.store
        cur = store.conn.cursor()
        # First reverse migration 035 (#283) back to the post-028 competition
        # shape (per-Season leagues; season_id + league_id on divisions and
        # registrations; no permanent Team.league_id), so the pre-028 reversal
        # below — written against that shape — still applies. 035 re-runs on
        # reopen alongside 021 and 028.
        cur.execute("DROP INDEX IF EXISTS ix_leagues_program")
        cur.execute("ALTER TABLE leagues ADD COLUMN season_id TEXT")
        cur.execute("UPDATE leagues SET season_id = (SELECT MIN(ls.season_id) "
                    "FROM league_seasons ls WHERE ls.league_id = leagues.id)")
        cur.execute("ALTER TABLE leagues DROP COLUMN program_id")
        cur.execute("DROP INDEX IF EXISTS ix_divisions_league_season")
        cur.execute("ALTER TABLE divisions ADD COLUMN season_id TEXT")
        cur.execute("ALTER TABLE divisions ADD COLUMN league_id TEXT")
        cur.execute("UPDATE divisions SET "
                    "league_id = (SELECT ls.league_id FROM league_seasons ls "
                    "WHERE ls.id = divisions.league_season_id), "
                    "season_id = (SELECT ls.season_id FROM league_seasons ls "
                    "WHERE ls.id = divisions.league_season_id)")
        cur.execute("ALTER TABLE divisions DROP COLUMN league_season_id")
        cur.execute("DROP INDEX IF EXISTS ux_team_league_season")
        cur.execute("DROP INDEX IF EXISTS ix_reg_league_season_division")
        cur.execute("ALTER TABLE season_team_registrations ADD COLUMN season_id TEXT")
        cur.execute("ALTER TABLE season_team_registrations ADD COLUMN league_id TEXT")
        cur.execute("UPDATE season_team_registrations SET "
                    "league_id = (SELECT ls.league_id FROM league_seasons ls "
                    "WHERE ls.id = season_team_registrations.league_season_id), "
                    "season_id = (SELECT ls.season_id FROM league_seasons ls "
                    "WHERE ls.id = season_team_registrations.league_season_id)")
        cur.execute("ALTER TABLE season_team_registrations DROP COLUMN league_season_id")
        cur.execute("DROP INDEX IF EXISTS ix_teams_league")
        cur.execute("ALTER TABLE teams DROP COLUMN league_id")
        cur.execute("DROP TABLE IF EXISTS league_seasons")
        # Reverse migration 028 (#233 C1b) to the pre-028 competition-model names
        # a legacy DB carried, so migration 021's backfill (which reads the old
        # names) can re-run, followed by 028 performing the rename again.
        cur.execute("ALTER TABLE games DROP COLUMN league_id")
        cur.execute("ALTER TABLE season_team_registrations DROP COLUMN league_id")
        cur.execute("ALTER TABLE divisions RENAME COLUMN league_id TO level_id")
        cur.execute("DROP INDEX IF EXISTS ix_leagues_external_ref")
        cur.execute("ALTER TABLE leagues RENAME TO levels")
        cur.execute("CREATE INDEX ix_levels_external_ref ON levels(external_ref)")
        cur.execute("DROP INDEX IF EXISTS ix_teams_program")
        cur.execute("ALTER TABLE teams RENAME COLUMN program_id TO league_id")
        cur.execute("DROP INDEX IF EXISTS ix_seasons_program")
        cur.execute("ALTER TABLE seasons RENAME COLUMN program_id TO league_id")
        cur.execute("DROP INDEX IF EXISTS ix_programs_operator_organization")
        cur.execute("DROP INDEX IF EXISTS ix_programs_external_ref")
        cur.execute("ALTER TABLE programs RENAME TO leagues")
        cur.execute("ALTER TABLE leagues "
                    "RENAME COLUMN operator_organization_id TO organization_id")
        cur.execute("CREATE INDEX ix_leagues_external_ref ON leagues(external_ref)")
        # Give the season a single grouping league (level) so the #233 reset gate
        # can deterministically reparent the division/registration on re-boot.
        cur.execute("INSERT INTO levels (id, season_id, name, sort_order) "
                    "VALUES (?, ?, 'L1', 0)", ("level_legacy", season["id"]))
        # Simulate the legacy shape: a pre-#180 team carried a Team.division_id
        # (create_team no longer writes one). Set it directly so migration 021
        # has the legacy link to backfill the permanent program + a registration.
        cur.execute("UPDATE teams SET division_id = ? WHERE id = ?",
                    (division["id"], team["id"]))
        # Then drop the #180 additions and their ledger entry plus the 028 entry,
        # and reopen so migrations 021 then 028 re-run over the legacy rows.
        cur.execute("DROP INDEX IF EXISTS ix_teams_league")
        cur.execute("ALTER TABLE teams DROP COLUMN league_id")
        cur.execute("DROP TABLE IF EXISTS season_team_registrations")
        cur.execute("DELETE FROM schema_migrations WHERE version IN "
                    "('021_permanent_teams', '028_competition_reset', "
                    "'035_competition_hierarchy_reset')")
        store.conn.commit()
        del api, store

        reopened = SqlStore(path)
        self.assertEqual(reopened.get_team(team["id"]).program_id, league["id"])
        regs = reopened.registrations_for_season(season["id"])
        self.assertEqual(len(regs), 1)
        self.assertEqual(regs[0].team_id, team["id"])
        self.assertEqual(regs[0].division_id, division["id"])


if __name__ == "__main__":
    unittest.main()
