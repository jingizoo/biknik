"""#233 Slice D1: a Team's Club is optional across the API and persistence.

This is primarily a convergence fix — the domain model and DB schema already
allow a null ``club_id``; the only gaps were ``SetupService.create_team``'s
unconditional Club lookup and the v2-only guard on ``assign_team_club``. Both
are fixed in ``services/setup_service.py`` (#233 Slice D).

The contract runs against the in-memory store and a durable SQL store
(SQLite locally, PostgreSQL via TEST_DATABASE_URL in CI) so behavior is
proven consistent across all three backends.
"""

import os
import unittest

from hockey_scheduler.api.service import ApiService
from hockey_scheduler.store import InMemoryStore, SqlStore


class OptionalClubContract:
    """A Team may exist, be created, and be reassigned with no Club — and no
    placeholder Club is ever created as a side effect."""

    def make_store(self):
        raise NotImplementedError

    ACTOR = "user_admin"

    def setUp(self):
        self.store = self.make_store()
        self.api = ApiService(self.store)
        self.program = self.api.create_program("Adult Men", actor_id=self.ACTOR)["id"]
        self.club = self.api.create_club("Lions Club", actor_id=self.ACTOR)["id"]

    # -- create_team ---------------------------------------------------

    def test_create_team_without_club_succeeds(self):
        result = self.api.create_team(
            None, None, "No Club FC", actor_id=self.ACTOR, program_id=self.program)
        self.assertNotIn("error", result, result)
        self.assertIsNone(result["club_id"])
        stored = self.store.get_team(result["id"])
        self.assertIsNone(stored.club_id)

    def test_create_team_with_blank_club_id_treated_as_none(self):
        result = self.api.create_team(
            "", None, "Blank Club FC", actor_id=self.ACTOR, program_id=self.program)
        self.assertNotIn("error", result, result)
        self.assertIsNone(result["club_id"])

    def test_create_team_with_club_still_succeeds(self):
        result = self.api.create_team(
            self.club, None, "Lions U16", actor_id=self.ACTOR, program_id=self.program)
        self.assertNotIn("error", result, result)
        self.assertEqual(result["club_id"], self.club)

    def test_create_team_with_unknown_club_rejected_zero_mutation(self):
        teams_before = len(self.store.all_teams())
        result = self.api.create_team(
            "club_missing", None, "Ghost FC", actor_id=self.ACTOR,
            program_id=self.program)
        self.assertEqual(result["error"]["code"], "not_found", result)
        self.assertEqual(len(self.store.all_teams()), teams_before)

    def test_create_team_without_club_writes_audit_with_null_club(self):
        audits_before = len(self.store.all_setup_audit())
        result = self.api.create_team(
            None, None, "No Club FC", actor_id=self.ACTOR, program_id=self.program)
        audits = self.store.all_setup_audit()
        self.assertEqual(len(audits), audits_before + 1)
        entry = audits[-1]
        self.assertEqual(entry.action, "team_created")
        self.assertEqual(entry.entity_id, result["id"])
        self.assertIsNone(entry.detail["club_id"])

    # -- assign_team_club (unassign / reassign) -------------------------

    def test_assign_team_club_null_unassigns_with_audit(self):
        team = self.api.create_team(
            self.club, None, "Lions U16", actor_id=self.ACTOR,
            program_id=self.program)["id"]
        audits_before = len(self.store.all_setup_audit())
        result = self.api.assign_team_club(team, None, actor_id=self.ACTOR)
        self.assertNotIn("error", result, result)
        self.assertIsNone(result["club_id"])
        self.assertIsNone(self.store.get_team(team).club_id)
        audits = self.store.all_setup_audit()
        self.assertEqual(len(audits), audits_before + 1)
        entry = audits[-1]
        self.assertEqual(entry.action, "team_club_assigned")
        self.assertEqual(entry.detail["from"], self.club)
        self.assertIsNone(entry.detail["to"])

    def test_assign_team_club_reassign_between_clubs(self):
        other_club = self.api.create_club("Falcons Club", actor_id=self.ACTOR)["id"]
        team = self.api.create_team(
            self.club, None, "Lions U16", actor_id=self.ACTOR,
            program_id=self.program)["id"]
        result = self.api.assign_team_club(team, other_club, actor_id=self.ACTOR)
        self.assertNotIn("error", result, result)
        self.assertEqual(result["club_id"], other_club)
        entry = self.store.all_setup_audit()[-1]
        self.assertEqual(entry.detail["from"], self.club)
        self.assertEqual(entry.detail["to"], other_club)

    def test_assign_team_club_from_none_to_a_club(self):
        team = self.api.create_team(
            None, None, "No Club FC", actor_id=self.ACTOR,
            program_id=self.program)["id"]
        result = self.api.assign_team_club(team, self.club, actor_id=self.ACTOR)
        self.assertNotIn("error", result, result)
        self.assertEqual(result["club_id"], self.club)
        entry = self.store.all_setup_audit()[-1]
        self.assertIsNone(entry.detail["from"])
        self.assertEqual(entry.detail["to"], self.club)

    def test_assign_unknown_club_rejected_team_unchanged(self):
        team = self.api.create_team(
            self.club, None, "Lions U16", actor_id=self.ACTOR,
            program_id=self.program)["id"]
        audits_before = len(self.store.all_setup_audit())
        result = self.api.assign_team_club(team, "club_missing", actor_id=self.ACTOR)
        self.assertEqual(result["error"]["code"], "not_found", result)
        self.assertEqual(self.store.get_team(team).club_id, self.club)
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)

    def test_assign_missing_team_not_found(self):
        result = self.api.assign_team_club("team_missing", None, actor_id=self.ACTOR)
        self.assertEqual(result["error"]["code"], "not_found", result)

    # -- no placeholder Club is ever created -----------------------------

    def test_no_placeholder_club_created_for_clubless_teams(self):
        clubs_before = len(self.store.all_clubs())
        self.api.create_team(
            None, None, "No Club A", actor_id=self.ACTOR, program_id=self.program)
        self.api.create_team(
            "", None, "No Club B", actor_id=self.ACTOR, program_id=self.program)
        team = self.api.create_team(
            self.club, None, "Lions U16", actor_id=self.ACTOR,
            program_id=self.program)["id"]
        self.api.assign_team_club(team, None, actor_id=self.ACTOR)
        clubs = self.store.all_clubs()
        self.assertEqual(len(clubs), clubs_before)
        names = {c.name.strip().upper() for c in clubs}
        self.assertNotIn("NA", names)
        self.assertNotIn("", names)


class MemoryOptionalClubTest(OptionalClubContract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


class DurableOptionalClubTest(OptionalClubContract, unittest.TestCase):
    def make_store(self):
        url = os.environ.get("TEST_DATABASE_URL") or ":memory:"
        store = SqlStore(url)
        store.reset_schema()
        return store


if __name__ == "__main__":
    unittest.main()
