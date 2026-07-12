"""Safe destructive deletion of setup records (#215).

Every entity delete must:
  * succeed and write an audit row when nothing depends on the record;
  * refuse with a structured ``has_dependencies`` error (code + a details
    breakdown of {type, count, names}) when any dependent record or history
    exists — and write NOTHING (the record and audit trail are untouched);
  * never silently cascade.

Draft (unpublished) games may be deleted; published or historical games must be
cancelled instead. The rules are identical across the in-memory store and the
SQL store (SQLite locally, PostgreSQL in CI via TEST_DATABASE_URL).
"""

import os
import unittest

from hockey_scheduler.api.service import ApiService
from hockey_scheduler.domain import IceSlotStatus
from hockey_scheduler.store import InMemoryStore, SqlStore


class DeletionContract:
    """Store-agnostic deletion behavior; concrete cases pick the store."""

    ACTOR = "user_admin"

    def setUp(self):
        self.store = self.make_store()
        self.api = ApiService(self.store)

    # -- fixture helpers ---------------------------------------------------
    def _league(self, name="League"):
        return self.api.create_league(name, actor_id=self.ACTOR)["id"]

    def _season(self, league_id, name="Season"):
        return self.api.create_season(league_id, name, actor_id=self.ACTOR)["id"]

    def _division(self, season_id, name="Division"):
        return self.api.create_division(season_id, name, actor_id=self.ACTOR)["id"]

    def _club(self, name="Club"):
        return self.api.create_club(name, actor_id=self.ACTOR)["id"]

    def _team(self, club_id, league_id, name="Team"):
        return self.api.create_team(
            club_id, None, name, actor_id=self.ACTOR, league_id=league_id)["id"]

    def _register(self, season_id, team_id, division_id):
        return self.api.register_team_for_season(
            season_id, team_id, division_id, actor_id=self.ACTOR)["id"]

    def _venue(self, league_id=None, name="Venue"):
        return self.api.create_venue(
            name, league_id=league_id, actor_id=self.ACTOR)["id"]

    def _rink(self, venue_id, name="Rink"):
        return self.api.create_rink(venue_id, name, actor_id=self.ACTOR)["id"]

    def _slot(self, rink_id):
        return self.api.create_ice_slot(
            rink_id, "2027-01-01T10:00:00+00:00", "2027-01-01T11:00:00+00:00",
            actor_id=self.ACTOR)["id"]

    def _game(self, season_id, division_id, home_id, away_id, slot_id):
        return self.api.create_game(
            season_id, division_id, home_id, away_id, slot_id,
            actor_id=self.ACTOR)["id"]

    # -- assertions --------------------------------------------------------
    def _audits(self, action):
        return [a for a in self.store.all_setup_audit() if a.action == action]

    def assertBlocked(self, result, *, expect_types=None):
        self.assertIn("error", result, f"expected a blocked delete, got {result}")
        self.assertEqual(result["error"]["code"], "has_dependencies")
        details = result["error"].get("details") or {}
        deps = details.get("dependencies")
        self.assertTrue(deps, "blocked delete must carry a dependency breakdown")
        for group in deps:
            self.assertIn("type", group)
            self.assertGreaterEqual(group["count"], 1)
            self.assertIsInstance(group["names"], list)
        if expect_types is not None:
            self.assertEqual({g["type"] for g in deps}, set(expect_types))

    def assertDeleted(self, result, getter, entity_id, audit_action):
        self.assertNotIn("error", result, f"expected success, got {result}")
        self.assertEqual(result.get("id"), entity_id)
        self.assertIsNone(getter(entity_id), "record should be gone after delete")
        audits = [a for a in self._audits(audit_action) if a.entity_id == entity_id]
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0].actor_id, self.ACTOR)

    # -- league ------------------------------------------------------------
    def test_league_blocked_by_children_then_deletable(self):
        lg = self._league()
        s = self._season(lg)
        club = self._club()
        team = self._team(club, lg)
        self._venue(league_id=lg)
        blocked = self.api.delete_league(lg, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"season", "team", "venue"})
        self.assertIsNotNone(self.store.get_league(lg))  # zero-write
        self.assertEqual(self._audits("league_deleted"), [])
        # Clear the dependents, then the league deletes cleanly.
        for v in list(self.store.all_venues()):
            self.api.delete_venue(v.id, actor_id=self.ACTOR)
        self.api.delete_team(team, actor_id=self.ACTOR)
        self.api.delete_season(s, actor_id=self.ACTOR)
        self.assertDeleted(self.api.delete_league(lg, actor_id=self.ACTOR),
                           self.store.get_league, lg, "league_deleted")

    # -- season ------------------------------------------------------------
    def test_season_blocked_by_division_and_registration(self):
        lg = self._league()
        s = self._season(lg)
        d = self._division(s)
        club = self._club()
        team = self._team(club, lg)
        self._register(s, team, d)
        blocked = self.api.delete_season(s, actor_id=self.ACTOR)
        self.assertBlocked(blocked,
                           expect_types={"division", "team registration"})
        self.assertIsNotNone(self.store.get_season(s))
        # Empty season is deletable.
        empty = self._season(lg, "Empty")
        self.assertDeleted(self.api.delete_season(empty, actor_id=self.ACTOR),
                           self.store.get_season, empty, "season_deleted")

    # -- division ----------------------------------------------------------
    def test_division_blocked_by_registration(self):
        lg = self._league()
        s = self._season(lg)
        d = self._division(s)
        club = self._club()
        team = self._team(club, lg)
        self._register(s, team, d)
        blocked = self.api.delete_division(d, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"team registration"})
        self.assertIsNotNone(self.store.get_division(d))
        empty = self._division(s, "Empty Div")
        self.assertDeleted(self.api.delete_division(empty, actor_id=self.ACTOR),
                           self.store.get_division, empty, "division_deleted")

    # -- club --------------------------------------------------------------
    def test_club_blocked_by_team(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        blocked = self.api.delete_club(club, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"team"})
        self.assertIsNotNone(self.store.get_club(club))
        self.api.delete_team(team, actor_id=self.ACTOR)
        self.assertDeleted(self.api.delete_club(club, actor_id=self.ACTOR),
                           self.store.get_club, club, "club_deleted")

    # -- team --------------------------------------------------------------
    def test_team_blocked_by_registration(self):
        lg = self._league()
        s = self._season(lg)
        d = self._division(s)
        club = self._club()
        team = self._team(club, lg)
        self._register(s, team, d)
        blocked = self.api.delete_team(team, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"season registration"})
        self.assertIsNotNone(self.store.get_team(team))

    def test_team_blocked_by_player(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        self.api.create_player(team, "Skater", "skater", actor_id=self.ACTOR)
        blocked = self.api.delete_team(team, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"player"})
        self.assertIsNotNone(self.store.get_team(team))

    def test_team_deletable_when_bare(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        self.assertDeleted(self.api.delete_team(team, actor_id=self.ACTOR),
                           self.store.get_team, team, "team_deleted")

    # -- venue / rink / ice slot -------------------------------------------
    def test_venue_rink_slot_chain(self):
        v = self._venue()
        r = self._rink(v)
        slot = self._slot(r)
        # Venue blocked by its rink; rink blocked by its slot.
        self.assertBlocked(self.api.delete_venue(v, actor_id=self.ACTOR),
                           expect_types={"rink"})
        self.assertBlocked(self.api.delete_rink(r, actor_id=self.ACTOR),
                           expect_types={"ice slot"})
        # Peel the chain from the leaf up.
        self.assertDeleted(self.api.delete_ice_slot(slot, actor_id=self.ACTOR),
                           self.store.get_ice_slot, slot, "ice_slot_deleted")
        self.assertDeleted(self.api.delete_rink(r, actor_id=self.ACTOR),
                           self.store.get_rink, r, "rink_deleted")
        self.assertDeleted(self.api.delete_venue(v, actor_id=self.ACTOR),
                           self.store.get_venue, v, "venue_deleted")

    def test_ice_slot_blocked_by_game(self):
        lg = self._league()
        s = self._season(lg)
        d = self._division(s)
        club = self._club()
        home = self._team(club, lg, "Home")
        away = self._team(club, lg, "Away")
        self._register(s, home, d)
        self._register(s, away, d)
        v = self._venue(league_id=lg)
        r = self._rink(v)
        slot = self._slot(r)
        self._game(s, d, home, away, slot)
        self.assertBlocked(self.api.delete_ice_slot(slot, actor_id=self.ACTOR),
                           expect_types={"game"})
        self.assertIsNotNone(self.store.get_ice_slot(slot))

    # -- games -------------------------------------------------------------
    def _built_game(self):
        lg = self._league()
        s = self._season(lg)
        d = self._division(s)
        club = self._club()
        home = self._team(club, lg, "Home")
        away = self._team(club, lg, "Away")
        self._register(s, home, d)
        self._register(s, away, d)
        slot = self._slot(self._rink(self._venue(league_id=lg)))
        return self._game(s, d, home, away, slot)

    def _make_draft(self, gid):
        """Flip a freshly created game into a scheduler draft in the store."""
        game = self.store.get_game(gid)
        game.is_draft = True
        self.store.save_game(game)
        return game

    def test_draft_game_deletable_and_releases_slot(self):
        gid = self._built_game()
        slot_id = self.store.get_game(gid).ice_slot_id
        # create_game allocated the slot; a draft holds it the same way.
        self.assertEqual(self.store.get_ice_slot(slot_id).status,
                         IceSlotStatus.ALLOCATED)
        self._make_draft(gid)
        self.assertDeleted(self.api.delete_game(gid, actor_id=self.ACTOR),
                           self.store.get_game, gid, "game_deleted")
        # The slot is returned to the available pool so it can be rebooked.
        self.assertEqual(self.store.get_ice_slot(slot_id).status,
                         IceSlotStatus.AVAILABLE)

    def test_ordinary_unpublished_game_not_deletable(self):
        # A manually created game is NOT a draft (is_draft=False), so even
        # unpublished it must be cancelled, never hard-deleted (#215).
        gid = self._built_game()
        self.assertFalse(self.store.get_game(gid).is_draft)
        blocked = self.api.delete_game(gid, actor_id=self.ACTOR)
        self.assertEqual(blocked["error"]["code"], "validation_error")
        self.assertIsNotNone(self.store.get_game(gid))  # zero-write

    def test_published_game_not_deletable(self):
        gid = self._built_game()
        game = self._make_draft(gid)
        game.published = True
        self.store.save_game(game)
        blocked = self.api.delete_game(gid, actor_id=self.ACTOR)
        self.assertEqual(blocked["error"]["code"], "validation_error")
        self.assertIsNotNone(self.store.get_game(gid))  # zero-write

    def test_cancelled_game_not_deletable(self):
        gid = self._built_game()
        self._make_draft(gid)
        self.api.cancel_game(gid, actor_id=self.ACTOR)
        blocked = self.api.delete_game(gid, actor_id=self.ACTOR)
        self.assertEqual(blocked["error"]["code"], "validation_error")
        self.assertIsNotNone(self.store.get_game(gid))

    # -- organization ------------------------------------------------------
    def test_organization_blocked_by_league_and_venue(self):
        org = self.api.create_organization("Org", actor_id=self.ACTOR)["id"]
        self.api.create_league("Owned", organization_id=org, actor_id=self.ACTOR)
        self.api.create_venue("Owned Venue", organization_id=org, actor_id=self.ACTOR)
        blocked = self.api.delete_organization(org, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"league", "venue"})
        self.assertIsNotNone(self.store.get_organization(org))
        empty = self.api.create_organization("Empty Org", actor_id=self.ACTOR)["id"]
        self.assertDeleted(self.api.delete_organization(empty, actor_id=self.ACTOR),
                           self.store.get_organization, empty, "organization_deleted")

    # -- level -------------------------------------------------------------
    def test_level_blocked_by_division(self):
        lg = self._league()
        s = self._season(lg)
        level = self.api.create_level(s, "Level A", actor_id=self.ACTOR)["id"]
        self.api.create_division(s, "In Level", level_id=level, actor_id=self.ACTOR)
        blocked = self.api.delete_level(level, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"division"})
        self.assertIsNotNone(self.store.get_level(level))
        empty = self.api.create_level(s, "Empty Level", actor_id=self.ACTOR)["id"]
        self.assertDeleted(self.api.delete_level(empty, actor_id=self.ACTOR),
                           self.store.get_level, empty, "level_deleted")

    # -- season blocked by a game (direct season_id reference) -------------
    def test_season_blocked_by_game(self):
        lg = self._league()
        s = self._season(lg)
        d = self._division(s)
        club = self._club()
        home = self._team(club, lg, "Home")
        away = self._team(club, lg, "Away")
        self._register(s, home, d)
        self._register(s, away, d)
        slot = self._slot(self._rink(self._venue(league_id=lg)))
        self._game(s, d, home, away, slot)
        blocked = self.api.delete_season(s, actor_id=self.ACTOR)
        deps = {g["type"] for g in blocked["error"]["details"]["dependencies"]}
        self.assertIn("game", deps)
        self.assertIsNotNone(self.store.get_season(s))

    # -- ice slot state rules: only an unused future available slot --------
    def test_past_slot_not_deletable(self):
        slot = self.api.create_ice_slot(
            self._rink(self._venue()),
            "2020-01-01T10:00:00+00:00", "2020-01-01T11:00:00+00:00",
            actor_id=self.ACTOR)["id"]
        blocked = self.api.delete_ice_slot(slot, actor_id=self.ACTOR)
        self.assertEqual(blocked["error"]["code"], "validation_error")
        self.assertIsNotNone(self.store.get_ice_slot(slot))

    def test_non_available_slot_not_deletable(self):
        # A future maintenance/blocked slot (slot_type != game → BLOCKED) is not
        # a free opening, so it can't be deleted even with no game on it.
        slot = self.api.create_ice_slot(
            self._rink(self._venue()),
            "2027-01-01T10:00:00+00:00", "2027-01-01T11:00:00+00:00",
            slot_type="maintenance", actor_id=self.ACTOR)["id"]
        self.assertNotEqual(self.store.get_ice_slot(slot).status,
                            IceSlotStatus.AVAILABLE)
        blocked = self.api.delete_ice_slot(slot, actor_id=self.ACTOR)
        self.assertEqual(blocked["error"]["code"], "validation_error")
        self.assertIsNotNone(self.store.get_ice_slot(slot))

    # -- not found ---------------------------------------------------------
    def test_missing_ids_report_not_found(self):
        for fn in (self.api.delete_organization, self.api.delete_league,
                   self.api.delete_season, self.api.delete_level,
                   self.api.delete_division, self.api.delete_club,
                   self.api.delete_team, self.api.delete_venue,
                   self.api.delete_rink, self.api.delete_ice_slot,
                   self.api.delete_game):
            result = fn("nope_1", actor_id=self.ACTOR)
            self.assertEqual(result["error"]["code"], "not_found")

    # -- audit content -----------------------------------------------------
    def test_blocked_delete_writes_no_audit(self):
        lg = self._league()
        self._season(lg)
        before = len(self.store.all_setup_audit())
        self.api.delete_league(lg, actor_id=self.ACTOR)
        self.assertEqual(len(self.store.all_setup_audit()), before,
                         "a blocked delete must not append any audit row")


class MemoryDeletionTest(DeletionContract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


class DurableDeletionTest(DeletionContract, unittest.TestCase):
    def make_store(self):
        # Honor TEST_DATABASE_URL so the PostgreSQL CI job exercises these
        # against Postgres; fall back to SQLite otherwise.
        url = os.environ.get("TEST_DATABASE_URL") or ":memory:"
        store = SqlStore(url)
        store.reset_schema()
        return store


if __name__ == "__main__":
    unittest.main()
