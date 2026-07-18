"""Production persistence restart proof (#174 PR A).

Proves the core client promise: an operator can stand up a fresh durable
database, configure the whole hierarchy plus staff accounts, restart the
application, and find *everything* — records, relationships, accounts, and the
audit trail — still there.

Unlike test_sql_store.py's reload test (which reopens the demo seed), this
starts from a completely EMPTY durable database and configures it exclusively
through the public API facade, exactly as a client would. It runs against a
SQLite temp file locally and against PostgreSQL in CI (TEST_DATABASE_URL).
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.store import SqlStore

UTC = timezone.utc
ADMIN = "installer_admin"


class ProductionRestartTest(unittest.TestCase):
    """A durable database configured from empty must survive a full restart."""

    def setUp(self):
        # PostgreSQL in CI (reset for isolation), else a local SQLite temp file.
        self._tmp = None
        url = os.environ.get("TEST_DATABASE_URL")
        if url:
            SqlStore(url).reset_schema()
            self.url = url
        else:
            fd, self._tmp = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            self.url = self._tmp

    def tearDown(self):
        if self._tmp:
            os.remove(self._tmp)

    def _fresh_api(self):
        """A brand-new store + facade against the same durable database —
        i.e. a fresh application process pointed at existing data."""
        return ApiService(SqlStore(self.url))

    def _configure(self, api):
        """Configure the full client hierarchy + a staff account from empty.
        Returns the ids captured at creation time so a reopened process can be
        checked against them exactly."""
        org = api.create_organization("Canlon", short_name="CAN", actor_id=ADMIN)
        league = api.create_program("Over 55", country="US",
                                    operator_organization_id=org["id"], actor_id=ADMIN)
        venue = api.create_venue("Plainfield", address="1 Rink Rd",
                                 league_id=league["id"], actor_id=ADMIN)
        rink = api.create_rink(venue["id"], "Rink 1", actor_id=ADMIN)
        slot = api.create_ice_slot(
            rink["id"], "2026-09-01T18:30:00+00:00", "2026-09-01T20:00:00+00:00",
            actor_id=ADMIN)
        season = api.create_season(league["id"], "Fall 2026", actor_id=ADMIN)
        division = api.create_division(season["id"], "Div A", actor_id=ADMIN)
        club = api.create_club("Club X", country="US", actor_id=ADMIN)
        team_a = api.create_team(club["id"], division["id"], "Team A", actor_id=ADMIN)
        team_b = api.create_team(club["id"], division["id"], "Team B", actor_id=ADMIN)
        player = api.create_player(team_a["id"], "Vince Skater", "forward",
                                   jersey_number=9, actor_id=ADMIN)
        official = api.create_official("Riley Whistle", actor_id=ADMIN)
        account = api.create_user_account(
            "arena_staff", "s3cret-pass", "arena_manager", actor_id=ADMIN)
        for name, obj in [("organization", org), ("league", league),
                          ("venue", venue), ("rink", rink), ("ice_slot", slot),
                          ("season", season), ("division", division),
                          ("club", club), ("team_a", team_a), ("team_b", team_b),
                          ("player", player), ("official", official),
                          ("account", account)]:
            self.assertNotIn("error", obj, f"{name} creation failed: {obj}")
        return {
            "org": org["id"], "league": league["id"], "venue": venue["id"],
            "rink": rink["id"], "slot": slot["id"], "season": season["id"],
            "division": division["id"], "club": club["id"],
            "team_a": team_a["id"], "team_b": team_b["id"],
            "player": player["id"], "official": official["id"],
            "account": account["id"],
        }

    def test_full_configuration_survives_restart(self):
        # 1. Configure from an empty durable database, then drop the process.
        first = self._fresh_api()
        ids = self._configure(first)
        audit_before = len(first.store.all_setup_audit())
        del first

        # 2. A fresh process against the same database sees every record.
        api = self._fresh_api()
        store = api.store
        self.assertIsNotNone(store.get_organization(ids["org"]))
        self.assertIsNotNone(store.get_program(ids["league"]))
        self.assertIsNotNone(store.get_venue(ids["venue"]))
        self.assertIsNotNone(store.get_rink(ids["rink"]))
        self.assertIsNotNone(store.get_ice_slot(ids["slot"]))
        self.assertIsNotNone(store.get_season(ids["season"]))
        self.assertIsNotNone(store.get_division(ids["division"]))
        self.assertIsNotNone(store.get_club(ids["club"]))
        self.assertIsNotNone(store.get_team(ids["team_a"]))
        self.assertIsNotNone(store.get_team(ids["team_b"]))
        self.assertIsNotNone(store.get_player(ids["player"]))
        self.assertIsNotNone(store.get_official(ids["official"]))

    def test_relationships_survive_restart(self):
        ids = self._configure(self._fresh_api())
        store = self._fresh_api().store
        # Facility chain: Organization → League → Venue → Rink → IceSlot.
        self.assertEqual(store.get_program(ids["league"]).operator_organization_id,
                         ids["org"])
        venue = store.get_venue(ids["venue"])
        self.assertEqual(venue.league_id, ids["league"])
        self.assertEqual(venue.organization_id, ids["org"])  # derived + persisted
        self.assertEqual(store.get_rink(ids["rink"]).venue_id, ids["venue"])
        self.assertEqual(store.get_ice_slot(ids["slot"]).rink_id, ids["rink"])
        # Competition chain: League → Season → Division → Team; Club → Team.
        self.assertEqual(store.get_season(ids["season"]).program_id, ids["league"])
        division = store.get_division(ids["division"])
        self.assertEqual(
            store.get_league_season(division.league_season_id).season_id,
            ids["season"])
        team = store.get_team(ids["team_a"])
        self.assertEqual(team.club_id, ids["club"])
        # #180: the surviving competition relationship is the permanent league,
        # not the legacy Team.division_id (which is no longer written).
        self.assertEqual(team.program_id, ids["league"])
        self.assertEqual(store.get_player(ids["player"]).team_id, ids["team_a"])

    def test_account_and_login_survive_restart(self):
        ids = self._configure(self._fresh_api())
        api = self._fresh_api()
        # The account row persists…
        self.assertIsNotNone(api.store.get_user_account(ids["account"]))
        self.assertIn("arena_staff",
                      [a["username"] for a in api.list_user_accounts()["user_accounts"]])
        # …and its hashed credential still authenticates after restart.
        self.assertIsNone(api.verify_login("arena_staff", "wrong-pass"))
        verified = api.verify_login("arena_staff", "s3cret-pass")
        self.assertIsNotNone(verified)
        self.assertEqual(verified["role"], "arena_manager")

    def test_audit_trail_survives_restart(self):
        first = self._fresh_api()
        self._configure(first)
        actions_before = {a.action for a in first.store.all_setup_audit()}
        count_before = len(first.store.all_setup_audit())
        del first

        store = self._fresh_api().store
        after = store.all_setup_audit()
        self.assertEqual(len(after), count_before)
        actions_after = {a.action for a in after}
        self.assertEqual(actions_after, actions_before)
        # The configuration steps left their audit fingerprints.
        for action in ("organization_created", "league_created", "venue_created",
                       "season_created", "division_created", "team_created"):
            self.assertIn(action, actions_after)
        # Actor attribution persisted (never anonymous).
        self.assertTrue(all(a.actor_id == ADMIN
                            for a in after if a.action == "organization_created"))


if __name__ == "__main__":
    unittest.main()
