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

from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api.service import ApiService
from hockey_scheduler.domain import (
    AvailabilityStatus, CalendarFeedToken, ContactDestination, DeviceToken,
    GameAvailability, GameRosterEntry, GuardianLink, IceSlotStatus, League,
    Notification, NotificationAudience, NotificationChannel, NotificationKind,
    NotificationPreference, OfficialAssignment, OfficialAssignmentStatus,
    OfficialAvailability, OfficialAvailabilityStatus, OfficialRole, Position,
    Role, RosterEntryStatus, RosterRole, SelectionSource,
    SubstituteEnrollment, SubstituteStatus,
)
from hockey_scheduler.store import InMemoryStore, SqlStore


class DeletionContract:
    """Store-agnostic deletion behavior; concrete cases pick the store."""

    ACTOR = "user_admin"

    def setUp(self):
        self.store = self.make_store()
        self.api = ApiService(self.store)

    # -- fixture helpers ---------------------------------------------------
    def _league(self, name="League"):
        return self.api.create_program(name, actor_id=self.ACTOR)["id"]

    def _season(self, league_id, name="Season"):
        return self.api.create_season(league_id, name, actor_id=self.ACTOR)["id"]

    def _division(self, season_id, name="Division"):
        return self.api.create_division(season_id, name, actor_id=self.ACTOR)["id"]

    def _club(self, name="Club"):
        return self.api.create_club(name, actor_id=self.ACTOR)["id"]

    def _team(self, club_id, league_id, name="Team"):
        # #283 Slice E: every Team must resolve a permanent League (rule 2).
        # The second arg here is the Program; create_team resolves the
        # Program's SOLE League when only a program is given. Family-A fixtures
        # let a Division auto-provision that League and Family-B fixtures create
        # a Level, so the Program already has exactly one League by the time a
        # team is made. Bare-team fixtures (no season/division/level) have none,
        # so provision a single permanent League under the Program here — as an
        # unbound League (no LeagueSeason), so it neither blocks a later
        # delete_season (which counts only a Season's LeagueSeasons' leagues)
        # nor is reported by delete_program (which does not block on leagues),
        # keeping every existing deletion assertion intact.
        program_id = league_id
        if not self.store.leagues_for_program(program_id):
            self.store.add_league(League(
                id=self.store.next_id("league"), program_id=program_id,
                name="League", sort_order=0))
        return self.api.create_team(
            club_id, None, name, actor_id=self.ACTOR, program_id=program_id)["id"]

    def _register(self, season_id, team_id, division_id):
        return self.api.register_team_for_season(
            season_id, team_id, division_id, actor_id=self.ACTOR)["id"]

    def _level(self, season_id, name="Level"):
        return self.api.create_league(season_id, name, actor_id=self.ACTOR)["id"]

    def _register_v2(self, season_id, team_id, division_id, league_id):
        return self.api.register_team_for_season(
            season_id, team_id, division_id, actor_id=self.ACTOR,
            league_id=league_id)["id"]

    def _venue(self, league_id=None, name="Venue", season_id=None):
        vid = self.api.create_venue(
            name, league_id=league_id, actor_id=self.ACTOR)["id"]
        if season_id:
            self.api.grant_season_venue_access(
                season_id, vid, actor_id=self.ACTOR)
        return vid

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

    def _official(self, name="Official"):
        return self.api.create_official(name, actor_id=self.ACTOR)["id"]

    def _player(self, team_id, name="Player"):
        return self.api.create_player(
            team_id, name, "skater", actor_id=self.ACTOR)["id"]

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
            # Each blocker carries a capped {id, name} pair so it can be
            # located even when names collide (#215 review 4).
            self.assertIsInstance(group["items"], list)
            self.assertTrue(group["items"])
            for item in group["items"]:
                self.assertIn("id", item)
                self.assertIn("name", item)
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
        blocked = self.api.delete_program(lg, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"season", "team", "venue"})
        self.assertIsNotNone(self.store.get_program(lg))  # zero-write
        self.assertEqual(self._audits("league_deleted"), [])
        # Clear the dependents, then the league deletes cleanly.
        for v in list(self.store.all_venues()):
            self.api.delete_venue(v.id, actor_id=self.ACTOR)
        self.api.delete_team(team, actor_id=self.ACTOR)
        self.api.delete_season(s, actor_id=self.ACTOR)
        self.assertDeleted(self.api.delete_program(lg, actor_id=self.ACTOR),
                           self.store.get_program, lg, "league_deleted")

    # -- season ------------------------------------------------------------
    def test_season_blocked_by_division_and_registration(self):
        lg = self._league()
        s = self._season(lg)
        d = self._division(s)
        club = self._club()
        team = self._team(club, lg)
        self._register(s, team, d)
        blocked = self.api.delete_season(s, actor_id=self.ACTOR)
        # The division auto-provisions a League (#283), whose LeagueSeason
        # also references this Season, so the season delete now additionally
        # blocks on that level alongside the division and registration.
        self.assertBlocked(
            blocked,
            expect_types={"division", "team registration", "level"})
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

    # -- division: inactive registrations never block, active ones do (#233
    # D1 bundled fix) — the UI can show 0 active teams under a Division while
    # a hidden inactive SeasonTeamRegistration row still points at it; that
    # row must not silently block deletion forever.
    def test_division_with_only_inactive_registration_deletes_and_clears_it(self):
        lg = self._league()
        s = self._season(lg)
        d = self._division(s)
        club = self._club()
        team = self._team(club, lg)
        reg = self._register(s, team, d)
        self.api.unregister_team_from_season(reg, actor_id=self.ACTOR)
        stored = self.store.get_season_team_registration(reg)
        self.assertFalse(stored.active)
        self.assertEqual(stored.division_id, d)  # retained until now (the bug)

        audits_before = len(self.store.all_setup_audit())
        result = self.api.delete_division(d, actor_id=self.ACTOR)
        self.assertNotIn("error", result, result)
        self.assertEqual(result["inactive_registrations_cleaned"], 1)
        self.assertIsNone(self.store.get_division(d))

        # The registration row itself survives — inactive, division cleared.
        stored = self.store.get_season_team_registration(reg)
        self.assertIsNotNone(stored)
        self.assertFalse(stored.active)
        self.assertIsNone(stored.division_id)
        self.assertEqual(
            self.store.get_league_season(stored.league_season_id).season_id, s)
        self.assertEqual(stored.team_id, team)

        cleared = self._audits("season_team_division_cleared")
        self.assertEqual(len(cleared), 1)
        self.assertEqual(cleared[0].entity_id, reg)
        self.assertEqual(cleared[0].detail["from"], d)
        self.assertIsNone(cleared[0].detail["to"])
        deleted = self._audits("division_deleted")
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0].detail["inactive_registrations_cleaned"], 1)
        # Exactly two new audit rows: the per-registration cleanup, then the
        # division deletion itself.
        self.assertEqual(len(self.store.all_setup_audit()), audits_before + 2)

    def test_division_with_active_registration_still_blocks_even_with_inactive(self):
        lg = self._league()
        s = self._season(lg)
        d = self._division(s)
        club = self._club()
        gone_team = self._team(club, lg, "Gone")
        gone_reg = self._register(s, gone_team, d)
        self.api.unregister_team_from_season(gone_reg, actor_id=self.ACTOR)
        stay_team = self._team(club, lg, "Stays")
        self._register(s, stay_team, d)

        blocked = self.api.delete_division(d, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"team registration"})
        # Zero-write on block: even the inactive row is untouched.
        self.assertIsNotNone(self.store.get_division(d))
        stored = self.store.get_season_team_registration(gone_reg)
        self.assertEqual(stored.division_id, d)
        self.assertEqual(self._audits("season_team_division_cleared"), [])

    def test_division_with_game_history_still_blocks_even_with_inactive_reg(self):
        lg = self._league()
        s = self._season(lg)
        d = self._division(s)
        club = self._club()
        home = self._team(club, lg, "Home")
        away = self._team(club, lg, "Away")
        home_reg = self._register(s, home, d)
        away_reg = self._register(s, away, d)
        slot = self._slot(self._rink(self._venue(league_id=lg, season_id=s)))
        game = self._game(s, d, home, away, slot)
        # unregister_team_from_season refuses to strand a team with a
        # scheduled game, so the only realistic path to "inactive
        # registration + Game history, zero active registrations" is: cancel
        # the game first (a cancelled game no longer counts as "scheduled"
        # and stops blocking unregister), THEN remove both teams from the
        # season. The cancelled Game itself is permanent history and must
        # still block deletion even though no ACTIVE registration remains.
        self.api.cancel_game(game, actor_id=self.ACTOR)
        self.api.unregister_team_from_season(home_reg, actor_id=self.ACTOR)
        self.api.unregister_team_from_season(away_reg, actor_id=self.ACTOR)

        blocked = self.api.delete_division(d, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"game"})
        self.assertIsNotNone(self.store.get_division(d))
        self.assertEqual(self._audits("season_team_division_cleared"), [])

    def test_division_delete_rolls_back_on_forced_mid_operation_failure(self):
        # Two inactive registrations to clear — force the SECOND save to
        # raise, so the operation fails after partial progress. The store's
        # transaction must roll back everything: the first registration's
        # clear, the audits, and the division itself.
        lg = self._league()
        s = self._season(lg)
        d = self._division(s)
        club = self._club()
        team1 = self._team(club, lg, "T1")
        team2 = self._team(club, lg, "T2")
        reg1 = self._register(s, team1, d)
        reg2 = self._register(s, team2, d)
        self.api.unregister_team_from_season(reg1, actor_id=self.ACTOR)
        self.api.unregister_team_from_season(reg2, actor_id=self.ACTOR)

        audits_before = len(self.store.all_setup_audit())
        original = self.store.save_season_team_registration
        calls = {"n": 0}

        def boom(reg):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("forced mid-operation failure")
            return original(reg)

        self.store.save_season_team_registration = boom
        try:
            with self.assertRaises(RuntimeError):
                self.api.delete_division(d, actor_id=self.ACTOR)
        finally:
            self.store.save_season_team_registration = original

        # Nothing persisted: the division, both registrations, and the audit
        # log are exactly as they were before the call — including the FIRST
        # registration's clear, which must not survive as a partial write.
        self.assertIsNotNone(self.store.get_division(d))
        stored1 = self.store.get_season_team_registration(reg1)
        stored2 = self.store.get_season_team_registration(reg2)
        self.assertEqual(stored1.division_id, d)
        self.assertEqual(stored2.division_id, d)
        self.assertFalse(stored1.active)
        self.assertFalse(stored2.active)
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)

    # -- explicit inactive-registration cleanup (#251) ---------------------
    # unregister_team_from_season only deactivates a row (Season/Team/League/
    # Division identity is retained for history) — delete_season_team_
    # registration is the separate, explicit operation that permanently
    # removes that row once it's inactive AND no Game (draft, scheduled,
    # cancelled, or historical) still references it.
    def test_registration_delete_succeeds_when_inactive_and_game_free(self):
        lg = self._league()
        s = self._season(lg)
        level = self._level(s)
        d = self.api.create_division(
            s, "Division", league_id=level, actor_id=self.ACTOR)["id"]
        club = self._club()
        team = self._team(club, lg)
        reg = self._register_v2(s, team, d, level)
        self.api.unregister_team_from_season(reg, actor_id=self.ACTOR)

        audits_before = len(self.store.all_setup_audit())
        result = self.api.delete_season_team_registration(reg, actor_id=self.ACTOR)
        self.assertNotIn("error", result, result)
        self.assertEqual(result["id"], reg)
        self.assertEqual(result["season_id"], s)
        self.assertEqual(result["team_id"], team)
        self.assertEqual(result["league_id"], level)
        self.assertEqual(result["division_id"], d)
        self.assertEqual(result["season_name"], "Season")
        self.assertEqual(result["team_name"], "Team")
        self.assertEqual(result["league_name"], "Level")
        self.assertEqual(result["division_name"], "Division")
        self.assertIsNone(self.store.get_season_team_registration(reg))

        deleted = self._audits("season_team_registration_deleted")
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0].entity_id, reg)
        self.assertEqual(deleted[0].actor_id, self.ACTOR)
        self.assertEqual(deleted[0].detail["season_id"], s)
        self.assertEqual(deleted[0].detail["team_id"], team)
        self.assertEqual(deleted[0].detail["division_id"], d)
        self.assertEqual(len(self.store.all_setup_audit()), audits_before + 1)

    def test_registration_delete_blocked_when_active(self):
        lg = self._league()
        s = self._season(lg)
        level = self._level(s)
        d = self.api.create_division(
            s, "Division", league_id=level, actor_id=self.ACTOR)["id"]
        club = self._club()
        team = self._team(club, lg)
        reg = self._register_v2(s, team, d, level)

        result = self.api.delete_season_team_registration(reg, actor_id=self.ACTOR)
        self.assertEqual(result["error"]["code"], "validation_error")
        self.assertEqual(result["error"]["details"]["reason"], "registration_active")
        self.assertIsNotNone(self.store.get_season_team_registration(reg))
        self.assertEqual(self._audits("season_team_registration_deleted"), [])

    def test_registration_delete_blocked_by_invalid_league(self):
        # A v1-derived (or otherwise legacy) registration with no resolvable
        # League can't be safely purged through this v2-only cleanup op —
        # it must be reported, not silently ignored or deleted.
        lg = self._league()
        s = self._season(lg)
        d = self._division(s)
        club = self._club()
        team = self._team(club, lg)
        reg = self._register(s, team, d)  # no explicit league_id
        self.api.unregister_team_from_season(reg, actor_id=self.ACTOR)
        # Break the League this registration resolves to (#283): its
        # LeagueSeason now points at a League that no longer exists — the
        # legacy "no resolvable League" case — so the v2-only cleanup op must
        # report it rather than purge the row blindly.
        stored = self.store.get_season_team_registration(reg)
        ls = self.store.get_league_season(stored.league_season_id)
        ls.league_id = "league_missing"
        self.store.save_league_season(ls)

        result = self.api.delete_season_team_registration(reg, actor_id=self.ACTOR)
        self.assertEqual(result["error"]["code"], "validation_error")
        self.assertEqual(result["error"]["details"]["reason"], "invalid_league")
        self.assertIsNotNone(self.store.get_season_team_registration(reg))
        self.assertEqual(self._audits("season_team_registration_deleted"), [])

    def test_registration_delete_blocked_by_draft_game(self):
        lg = self._league()
        s = self._season(lg)
        level = self._level(s)
        d = self.api.create_division(
            s, "Division", league_id=level, actor_id=self.ACTOR)["id"]
        club = self._club()
        home = self._team(club, lg, "Home")
        away = self._team(club, lg, "Away")
        home_reg = self._register_v2(s, home, d, level)
        away_reg = self._register_v2(s, away, d, level)
        slot = self._slot(self._rink(self._venue(league_id=lg, season_id=s)))
        game = self._game(s, d, home, away, slot)
        self._make_draft(game)
        # A draft game holds neither team's registration off "scheduled", so
        # both can be deactivated even while the draft still references them.
        self.api.unregister_team_from_season(home_reg, actor_id=self.ACTOR)
        self.api.unregister_team_from_season(away_reg, actor_id=self.ACTOR)

        blocked = self.api.delete_season_team_registration(
            home_reg, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"game"})
        self.assertIsNotNone(self.store.get_season_team_registration(home_reg))

    def test_registration_delete_blocked_by_cancelled_game(self):
        lg = self._league()
        s = self._season(lg)
        level = self._level(s)
        d = self.api.create_division(
            s, "Division", league_id=level, actor_id=self.ACTOR)["id"]
        club = self._club()
        home = self._team(club, lg, "Home")
        away = self._team(club, lg, "Away")
        home_reg = self._register_v2(s, home, d, level)
        away_reg = self._register_v2(s, away, d, level)
        slot = self._slot(self._rink(self._venue(league_id=lg, season_id=s)))
        game = self._game(s, d, home, away, slot)
        self.api.cancel_game(game, actor_id=self.ACTOR)
        self.api.unregister_team_from_season(home_reg, actor_id=self.ACTOR)
        self.api.unregister_team_from_season(away_reg, actor_id=self.ACTOR)

        blocked = self.api.delete_season_team_registration(
            home_reg, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"game"})
        self.assertIsNotNone(self.store.get_season_team_registration(home_reg))
        self.assertEqual(self._audits("season_team_registration_deleted"), [])

    def test_registration_delete_blocked_by_published_game_history(self):
        # A game that was actually played (published, then later cancelled —
        # the only way a committed game stops blocking unregister) is
        # permanent history and must keep blocking the registration cleanup
        # too, just like a plain cancelled game.
        lg = self._league()
        s = self._season(lg)
        level = self._level(s)
        d = self.api.create_division(
            s, "Division", league_id=level, actor_id=self.ACTOR)["id"]
        club = self._club()
        home = self._team(club, lg, "Home")
        away = self._team(club, lg, "Away")
        home_reg = self._register_v2(s, home, d, level)
        away_reg = self._register_v2(s, away, d, level)
        slot = self._slot(self._rink(self._venue(league_id=lg, season_id=s)))
        game = self._game(s, d, home, away, slot)
        stored_game = self.store.get_game(game)
        stored_game.published = True
        self.store.save_game(stored_game)
        self.api.cancel_game(game, actor_id=self.ACTOR)
        self.api.unregister_team_from_season(home_reg, actor_id=self.ACTOR)
        self.api.unregister_team_from_season(away_reg, actor_id=self.ACTOR)

        blocked = self.api.delete_season_team_registration(
            away_reg, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"game"})
        self.assertIsNotNone(self.store.get_season_team_registration(away_reg))

    def test_registration_delete_rolls_back_on_forced_mid_operation_failure(self):
        lg = self._league()
        s = self._season(lg)
        level = self._level(s)
        d = self.api.create_division(
            s, "Division", league_id=level, actor_id=self.ACTOR)["id"]
        club = self._club()
        team = self._team(club, lg)
        reg = self._register_v2(s, team, d, level)
        self.api.unregister_team_from_season(reg, actor_id=self.ACTOR)

        audits_before = len(self.store.all_setup_audit())
        original = self.store.add_setup_audit

        def boom(entry):
            raise RuntimeError("forced mid-operation failure")

        self.store.add_setup_audit = boom
        try:
            with self.assertRaises(RuntimeError):
                self.api.delete_season_team_registration(reg, actor_id=self.ACTOR)
        finally:
            self.store.add_setup_audit = original

        # The store-level delete already ran before the forced failure; the
        # transaction must still roll it back so nothing is lost.
        self.assertIsNotNone(self.store.get_season_team_registration(reg))
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)

    def test_league_season_team_deletable_after_cleaning_inactive_registration(self):
        # #251 item 8: League (formerly Level)/Season/Team delete are
        # unchanged — they still block on ANY registration, active or not —
        # but once the new cleanup op removes the inactive row, the existing
        # block naturally resolves and the previously-stuck parent deletes
        # succeed. Program is proven too, for the full chain.
        lg = self._league()
        s = self._season(lg)
        level = self._level(s)
        d = self.api.create_division(
            s, "Division", league_id=level, actor_id=self.ACTOR)["id"]
        club = self._club()
        team = self._team(club, lg)
        reg = self._register_v2(s, team, d, level)
        self.api.unregister_team_from_season(reg, actor_id=self.ACTOR)
        # Clear the Division dependency first (#249) so League/Season/Team
        # blocking is isolated to the hidden inactive registration itself.
        self.api.delete_division(d, actor_id=self.ACTOR)

        self.assertBlocked(
            self.api.delete_league(level, actor_id=self.ACTOR),
            expect_types={"team registration"})
        self.assertBlocked(
            self.api.delete_season(s, actor_id=self.ACTOR),
            expect_types={"level", "team registration"})
        self.assertBlocked(
            self.api.delete_team(team, actor_id=self.ACTOR),
            expect_types={"season registration"})

        self.assertDeleted(
            self.api.delete_season_team_registration(reg, actor_id=self.ACTOR),
            self.store.get_season_team_registration, reg,
            "season_team_registration_deleted")

        self.assertDeleted(self.api.delete_league(level, actor_id=self.ACTOR),
                           self.store.get_league, level, "level_deleted")
        self.assertDeleted(self.api.delete_team(team, actor_id=self.ACTOR),
                           self.store.get_team, team, "team_deleted")
        self.assertDeleted(self.api.delete_season(s, actor_id=self.ACTOR),
                           self.store.get_season, s, "season_deleted")
        self.assertDeleted(self.api.delete_program(lg, actor_id=self.ACTOR),
                           self.store.get_program, lg, "league_deleted")

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

    def test_team_blocked_by_coach_account(self):
        # A coach account scoped to the team is a live identity pointing at it,
        # so the team can't be hard-deleted out from under it (#215 r4).
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        self.api.accounts.create_account(
            "coach_x", "a-real-password", Role.COACH,
            scope={"team_id": team}, actor_id=self.ACTOR)
        blocked = self.api.delete_team(team, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"account"})
        self.assertIsNotNone(self.store.get_team(team))  # zero-write

    def test_team_blocked_by_live_calendar_feed(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        self.store.add_calendar_feed_token(CalendarFeedToken(
            id=self.store.next_id("cft"), token_hash="hash_x",
            actor_type="team", actor_ref=team,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
        blocked = self.api.delete_team(team, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"calendar feed"})
        self.assertIsNotNone(self.store.get_team(team))

    def test_revoked_feed_does_not_block_team_delete(self):
        # A revoked feed is inert history, not a live pointer, so it must not
        # block an otherwise-bare team.
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        self.store.add_calendar_feed_token(CalendarFeedToken(
            id=self.store.next_id("cft"), token_hash="hash_y",
            actor_type="team", actor_ref=team,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            revoked_at=datetime(2026, 2, 1, tzinfo=timezone.utc)))
        self.assertDeleted(self.api.delete_team(team, actor_id=self.ACTOR),
                           self.store.get_team, team, "team_deleted")

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
        v = self._venue(league_id=lg, season_id=s)
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
        slot = self._slot(self._rink(self._venue(league_id=lg, season_id=s)))
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

    # -- dependency details carry identifiers ------------------------------
    def test_blocked_details_distinguish_duplicate_named_blockers(self):
        # Two teams with the SAME name block a club delete; the details must
        # still expose two distinct ids so the UI can locate each (#215 r4).
        lg = self._league()
        club = self._club()
        self._team(club, lg, "Dup")
        self._team(club, lg, "Dup")
        blocked = self.api.delete_club(club, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"team"})
        group = blocked["error"]["details"]["dependencies"][0]
        self.assertEqual(group["count"], 2)
        ids = {item["id"] for item in group["items"]}
        self.assertEqual(len(ids), 2)
        self.assertEqual({item["name"] for item in group["items"]}, {"Dup"})

    # -- organization ------------------------------------------------------
    def test_organization_blocked_by_league_and_venue(self):
        org = self.api.create_organization("Org", actor_id=self.ACTOR)["id"]
        self.api.create_program("Owned", operator_organization_id=org, actor_id=self.ACTOR)
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
        level = self.api.create_league(s, "Level A", actor_id=self.ACTOR)["id"]
        self.api.create_division(s, "In Level", league_id=level, actor_id=self.ACTOR)
        blocked = self.api.delete_league(level, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"division"})
        self.assertIsNotNone(self.store.get_league(level))
        empty = self.api.create_league(s, "Empty Level", actor_id=self.ACTOR)["id"]
        self.assertDeleted(self.api.delete_league(empty, actor_id=self.ACTOR),
                           self.store.get_league, empty, "level_deleted")

    def test_level_blocked_by_registration(self):
        """#233 B2b review r2: a v2 registration's REQUIRED league_id can
        point directly at a grouping League with no Division (division-less
        participation) — deleting that League must not silently orphan a
        required field. Covers both a division-less registration and one
        with a Division under the same League, and proves zero mutation
        (record, division, and registration all survive; no audit written)
        on every blocked attempt."""
        lg = self._league()
        s = self._season(lg)
        level = self.api.create_league(s, "Level A", actor_id=self.ACTOR)["id"]
        club = self._club()

        # Division-less registration parked directly under the League.
        bare_team = self._team(club, lg, "Bare Team")
        reg = self.api.register_team_for_season(
            s, bare_team, None, actor_id=self.ACTOR, league_id=level)
        self.assertNotIn("error", reg, reg)
        regs_before = len(self.store.all_season_team_registrations())
        audits_before = len(self.store.all_setup_audit())
        blocked = self.api.delete_league(level, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"team registration"})
        self.assertIsNotNone(self.store.get_league(level))
        self.assertIsNotNone(
            self.store.get_season_team_registration(reg["id"]))
        self.assertEqual(len(self.store.all_season_team_registrations()),
                         regs_before, "blocked delete must not mutate registrations")
        self.assertEqual(len(self.store.all_setup_audit()), audits_before,
                         "blocked delete must not write an audit row")

        # A registration WITH a Division under the same League also blocks —
        # both the division and the registration are reported, so neither
        # can be missed if an operator clears one dependent but not the other.
        # (A registration keeps blocking its League even once "removed" —
        # unregister_team_from_season deactivates rather than deletes the
        # row, and this dependency check counts every registration
        # referencing the League regardless of active state, mirroring
        # delete_division's identical, unfiltered convention just above —
        # so this test proves the blocked path only, like its sibling
        # test_level_blocked_by_division, which proves the empty-deletes-
        # cleanly path on a SEPARATE League rather than this one.)
        div = self.api.create_division_v2(level, "In Level", actor_id=self.ACTOR)["id"]
        div_team = self._team(club, lg, "Div Team")
        reg2 = self.api.register_team_for_season(
            s, div_team, div, actor_id=self.ACTOR, league_id=level)
        self.assertNotIn("error", reg2, reg2)
        blocked2 = self.api.delete_league(level, actor_id=self.ACTOR)
        self.assertBlocked(blocked2,
                           expect_types={"division", "team registration"})
        self.assertIsNotNone(self.store.get_league(level))

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
        slot = self._slot(self._rink(self._venue(league_id=lg, season_id=s)))
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

    # -- official (#232) -----------------------------------------------------
    def test_official_blocked_by_assignment(self):
        official = self._official()
        gid = self._built_game()
        self.store.add_official_assignment(OfficialAssignment(
            id=self.store.next_id("official_assignment"), game_id=gid,
            official_id=official, role=OfficialRole.REFEREE,
            status=OfficialAssignmentStatus.PROPOSED))
        blocked = self.api.delete_official(official, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"assignment"})
        self.assertIsNotNone(self.store.get_official(official))

    def test_official_blocked_by_availability_window(self):
        official = self._official()
        self.store.add_official_availability(OfficialAvailability(
            id=self.store.next_id("official_availability"),
            official_id=official,
            start_time=datetime(2027, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2027, 1, 2, tzinfo=timezone.utc),
            status=OfficialAvailabilityStatus.UNAVAILABLE))
        blocked = self.api.delete_official(official, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"availability window"})
        self.assertIsNotNone(self.store.get_official(official))

    def test_official_blocked_by_account(self):
        official = self._official()
        self.api.accounts.create_account(
            "official_x", "a-real-password", Role.OFFICIAL,
            scope={"official_id": official}, actor_id=self.ACTOR)
        blocked = self.api.delete_official(official, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"account"})
        self.assertIsNotNone(self.store.get_official(official))

    def test_official_blocked_by_live_calendar_feed(self):
        official = self._official()
        self.store.add_calendar_feed_token(CalendarFeedToken(
            id=self.store.next_id("cft"), token_hash="hash_o",
            actor_type="official", actor_ref=official,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
        blocked = self.api.delete_official(official, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"calendar feed"})
        self.assertIsNotNone(self.store.get_official(official))

    def test_official_blocked_by_device_token(self):
        official = self._official()
        self.store.add_device_token(DeviceToken(
            id=self.store.next_id("device"), recipient_ref=f"official:{official}",
            provider="fcm", token="tok_o"))
        blocked = self.api.delete_official(official, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"device token"})
        self.assertIsNotNone(self.store.get_official(official))

    def test_official_deletable_when_bare(self):
        official = self._official()
        self.assertDeleted(self.api.delete_official(official, actor_id=self.ACTOR),
                           self.store.get_official, official, "official_deleted")

    # -- official dependency lifecycle (#232 review) ------------------------
    # An account/device token has an existing supported deactivation route
    # (#67/#65); an inactive one is inert history, not a live pointer, so it
    # must not block — mirroring #251's own "revoked ≠ live" precedent.
    def test_official_inactive_account_does_not_block(self):
        official = self._official()
        acc = self.api.accounts.create_account(
            "official_inactive", "a-real-password", Role.OFFICIAL,
            scope={"official_id": official}, actor_id=self.ACTOR)
        self.api.accounts.set_active(acc.id, False, actor_id=self.ACTOR)
        self.assertDeleted(self.api.delete_official(official, actor_id=self.ACTOR),
                           self.store.get_official, official, "official_deleted")

    def test_official_inactive_device_token_does_not_block(self):
        official = self._official()
        self.store.add_device_token(DeviceToken(
            id=self.store.next_id("device"), recipient_ref=f"official:{official}",
            provider="fcm", token="tok_o_inactive", active=False))
        self.assertDeleted(self.api.delete_official(official, actor_id=self.ACTOR),
                           self.store.get_official, official, "official_deleted")

    def test_reactivating_account_after_official_deleted_is_blocked(self):
        # Scoping the delete blocker to active accounts only (above) must not
        # reopen a dangling-identity hole: once the official is gone, the
        # inactive account can never come back without being rebound.
        official = self._official()
        acc = self.api.accounts.create_account(
            "official_rebind", "a-real-password", Role.OFFICIAL,
            scope={"official_id": official}, actor_id=self.ACTOR)
        self.api.accounts.set_active(acc.id, False, actor_id=self.ACTOR)
        self.assertDeleted(self.api.delete_official(official, actor_id=self.ACTOR),
                           self.store.get_official, official, "official_deleted")
        result = self.api.set_user_account_active(acc.id, True, actor_id=self.ACTOR)
        self.assertEqual(result["error"]["code"], "validation_error")
        self.assertFalse(self.store.get_user_account(acc.id).active)

    def test_creating_account_for_deleted_official_is_rejected(self):
        # #232 review 7: the reactivation guard above only protects an
        # account that already existed before the official was deleted — a
        # brand-new account scoped to the (now-gone) official id must be
        # refused just as firmly, or delete-then-create recreates the exact
        # dangling live identity #232 exists to prevent. Zero account/audit
        # mutation on rejection.
        official = self._official()
        self.assertDeleted(self.api.delete_official(official, actor_id=self.ACTOR),
                           self.store.get_official, official, "official_deleted")
        accounts_before = len(self.store.all_user_accounts())
        audits_before = len(self.store.all_setup_audit())
        result = self.api.create_user_account(
            "official_ghost", "a-real-password", "official",
            scope={"official_id": official}, actor_id=self.ACTOR)
        self.assertEqual(result["error"]["code"], "validation_error")
        self.assertEqual(result["error"]["details"]["reason"], "scope_subject_missing")
        self.assertEqual(len(self.store.all_user_accounts()), accounts_before)
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)
        self.assertIsNone(self.store.get_user_account_by_username("official_ghost"))

    def test_creating_account_for_existing_official_still_works(self):
        official = self._official()
        result = self.api.create_user_account(
            "official_live", "a-real-password", "official",
            scope={"official_id": official}, actor_id=self.ACTOR)
        self.assertNotIn("error", result, result)
        self.assertTrue(result["active"])

    def test_official_blocked_by_contact_destination(self):
        official = self._official()
        self.store.add_contact_destination(ContactDestination(
            id=self.store.next_id("contact"), recipient_ref=f"official:{official}",
            channel=NotificationChannel.EMAIL, destination="ref@example.com"))
        blocked = self.api.delete_official(official, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"contact destination"})
        self.assertIsNotNone(self.store.get_official(official))

    def test_official_blocked_by_notification_preference(self):
        official = self._official()
        self.store.save_notification_preference(NotificationPreference(
            id=self.store.next_id("notif_pref"), recipient_ref=f"official:{official}",
            channel=NotificationChannel.PUSH, enabled=False))
        blocked = self.api.delete_official(official, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"notification preference"})
        self.assertIsNotNone(self.store.get_official(official))

    # -- official contact/preference retirement (#232 review 4) --------------
    # #232 requires Player/Official deletion never silently cascade or erase
    # a dependent record. A contact destination/notification preference is
    # therefore never deleted at all — it is retired (active=False) through
    # its own explicit, audited action, exactly like an account/device token
    # is deactivated, preserving the stored destination/opt-out and its
    # history. Only an ACTIVE row blocks the delete.
    def test_official_inactive_contact_destination_does_not_block(self):
        official = self._official()
        self.store.add_contact_destination(ContactDestination(
            id=self.store.next_id("contact"), recipient_ref=f"official:{official}",
            channel=NotificationChannel.EMAIL, destination="inactive@example.com",
            active=False))
        self.assertDeleted(self.api.delete_official(official, actor_id=self.ACTOR),
                           self.store.get_official, official, "official_deleted")

    def test_official_inactive_notification_preference_does_not_block(self):
        official = self._official()
        self.store.save_notification_preference(NotificationPreference(
            id=self.store.next_id("notif_pref"), recipient_ref=f"official:{official}",
            channel=NotificationChannel.PUSH, enabled=False, active=False))
        self.assertDeleted(self.api.delete_official(official, actor_id=self.ACTOR),
                           self.store.get_official, official, "official_deleted")

    def test_official_blocked_by_contact_destination_then_retired_then_deletes(self):
        official = self._official()
        contact = self.store.add_contact_destination(ContactDestination(
            id=self.store.next_id("contact"), recipient_ref=f"official:{official}",
            channel=NotificationChannel.EMAIL, destination="retire-me@example.com"))
        audits_before = len(self.store.all_setup_audit())
        blocked = self.api.delete_official(official, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"contact destination"})
        self.assertIsNotNone(self.store.get_official(official))
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)

        retired = self.api.set_contact_destination_active(
            contact.id, False, actor_id=self.ACTOR)
        self.assertNotIn("error", retired, retired)
        self.assertFalse(retired["active"])
        self.assertEqual(retired["destination"], "retire-me@example.com",
                         "retiring must not touch the stored destination")

        before_delete_audits = len(self.store.all_setup_audit())
        self.assertDeleted(self.api.delete_official(official, actor_id=self.ACTOR),
                           self.store.get_official, official, "official_deleted")
        self.assertEqual(len(self.store.all_setup_audit()), before_delete_audits + 1,
                         "a successful delete writes exactly one audit entry")

        # History preserved, queryable, unchanged (#232's no-erase contract):
        still_there = next(
            (c for c in self.store.all_contact_destinations() if c.id == contact.id),
            None)
        self.assertIsNotNone(still_there)
        self.assertFalse(still_there.active)
        self.assertEqual(still_there.destination, "retire-me@example.com")

    def test_official_blocked_by_notification_preference_then_retired_then_deletes(self):
        official = self._official()
        pref = self.store.save_notification_preference(NotificationPreference(
            id=self.store.next_id("notif_pref"), recipient_ref=f"official:{official}",
            channel=NotificationChannel.PUSH, enabled=False))
        blocked = self.api.delete_official(official, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"notification preference"})

        retired = self.api.set_notification_preference_active(
            pref.id, False, actor_id=self.ACTOR)
        self.assertNotIn("error", retired, retired)
        self.assertFalse(retired["active"])
        self.assertFalse(retired["enabled"], "retiring must not touch the opt-out")

        self.assertDeleted(self.api.delete_official(official, actor_id=self.ACTOR),
                           self.store.get_official, official, "official_deleted")

        still_there = next(
            (p for p in self.store.all_notification_preferences() if p.id == pref.id),
            None)
        self.assertIsNotNone(still_there, "retired history must remain queryable")
        self.assertFalse(still_there.active)
        self.assertFalse(still_there.enabled)

    def test_official_blocked_delete_preserves_contact_and_preference(self):
        # The scenario the reviewer flagged directly: a delete blocked by
        # something else entirely (here, an active device token) must leave
        # the recipient's opt-out state completely untouched — no silent
        # re-enable, zero mutation, zero audit.
        official = self._official()
        ref = f"official:{official}"
        self.store.add_device_token(DeviceToken(
            id=self.store.next_id("device"), recipient_ref=ref,
            provider="fcm", token="tok_o_block"))
        pref = self.store.save_notification_preference(NotificationPreference(
            id=self.store.next_id("notif_pref"), recipient_ref=ref,
            channel=NotificationChannel.EMAIL, enabled=False))
        audits_before = len(self.store.all_setup_audit())
        blocked = self.api.delete_official(official, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"device token", "notification preference"})
        self.assertIsNotNone(self.store.get_official(official))
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)
        stored = self.store.get_notification_preference(
            ref, NotificationChannel.EMAIL)
        self.assertIsNotNone(stored, "a blocked delete must not touch the "
                             "recipient's stored opt-out")
        self.assertFalse(stored.enabled)
        self.assertTrue(stored.active)
        self.assertEqual(stored.id, pref.id)

    def test_official_delete_rolls_back_on_forced_audit_failure(self):
        # #232's "exactly one audit on success" contract implies the delete
        # itself is still a single atomic write: a forced failure on that
        # one audit call must roll back the identity delete too.
        official = self._official()
        audits_before = len(self.store.all_setup_audit())
        original = self.store.add_setup_audit

        def boom(entry):
            raise RuntimeError("forced audit failure")

        self.store.add_setup_audit = boom
        try:
            with self.assertRaises(RuntimeError):
                self.api.delete_official(official, actor_id=self.ACTOR)
        finally:
            self.store.add_setup_audit = original

        self.assertIsNotNone(self.store.get_official(official))
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)

    # -- official dangling-recipient guard (#232 review 2) -------------------
    # Scoping the delete blocker to active tokens/accounts must not reopen a
    # dangling-identity hole via deactivate -> delete -> reactivate/
    # re-register — the same class of hole the account reactivation guard
    # closes, for every other recipient-scoped route.
    def test_reactivating_device_token_after_official_deleted_is_blocked(self):
        official = self._official()
        ref = f"official:{official}"
        tok = self.api.register_device_token(ref, "fcm", "tok_o_react")
        self.api.set_device_token_active(tok["id"], False)
        self.assertDeleted(self.api.delete_official(official, actor_id=self.ACTOR),
                           self.store.get_official, official, "official_deleted")
        result = self.api.set_device_token_active(tok["id"], True)
        self.assertEqual(result["error"]["code"], "validation_error", result)
        self.assertFalse(self.store.get_device_token(tok["id"]).active)

    def test_reregistering_device_token_after_official_deleted_is_blocked(self):
        official = self._official()
        ref = f"official:{official}"
        tok = self.api.register_device_token(ref, "fcm", "tok_o_reg")
        self.api.set_device_token_active(tok["id"], False)
        self.assertDeleted(self.api.delete_official(official, actor_id=self.ACTOR),
                           self.store.get_official, official, "official_deleted")
        result = self.api.register_device_token(ref, "fcm", "tok_o_reg")
        self.assertEqual(result["error"]["code"], "validation_error", result)
        self.assertFalse(self.store.get_device_token(tok["id"]).active)
        result = self.api.register_device_token(ref, "fcm", "tok_o_brand_new")
        self.assertEqual(result["error"]["code"], "validation_error", result)

    def test_device_token_lifecycle_allowed_for_existing_official(self):
        official = self._official()
        ref = f"official:{official}"
        tok = self.api.register_device_token(ref, "fcm", "tok_o_live")
        self.assertNotIn("error", tok)
        self.assertTrue(self.api.set_device_token_active(tok["id"], False)["active"] is False)
        reactivated = self.api.set_device_token_active(tok["id"], True)
        self.assertNotIn("error", reactivated)
        self.assertTrue(reactivated["active"])

    # -- official contact/preference re-creation guard (#232 review 3) ------
    # The dangling-recipient guard extends to the setters themselves: once an
    # official is deleted, nothing may re-point a contact destination or a
    # notification preference at the now-dead recipient_ref, closing the same
    # hole the device-token guard closes for register/reactivate.
    def test_contact_destination_rejects_recreation_after_official_deleted(self):
        official = self._official()
        ref = f"official:{official}"
        self.assertDeleted(self.api.delete_official(official, actor_id=self.ACTOR),
                           self.store.get_official, official, "official_deleted")
        before = len(self.store.all_contact_destinations())
        result = self.api.set_contact_destination(ref, "email", "dead@example.com")
        self.assertEqual(result["error"]["code"], "validation_error", result)
        self.assertEqual(result["error"]["details"]["reason"], "scope_subject_missing")
        self.assertEqual(len(self.store.all_contact_destinations()), before)

    def test_notification_preference_rejects_recreation_after_official_deleted(self):
        official = self._official()
        ref = f"official:{official}"
        self.assertDeleted(self.api.delete_official(official, actor_id=self.ACTOR),
                           self.store.get_official, official, "official_deleted")
        before = len(self.store.all_notification_preferences())
        result = self.api.set_notification_preference(ref, "email", False)
        self.assertEqual(result["error"]["code"], "validation_error", result)
        self.assertEqual(result["error"]["details"]["reason"], "scope_subject_missing")
        self.assertEqual(len(self.store.all_notification_preferences()), before)

    def test_contact_destination_still_settable_for_existing_official(self):
        official = self._official()
        ref = f"official:{official}"
        result = self.api.set_contact_destination(ref, "email", "live@example.com")
        self.assertNotIn("error", result, result)
        self.assertEqual(result["destination"], "live@example.com")

    def test_notification_preference_still_settable_for_existing_official(self):
        official = self._official()
        ref = f"official:{official}"
        result = self.api.set_notification_preference(ref, "email", False)
        self.assertNotIn("error", result, result)
        self.assertFalse(result["enabled"])

    def test_ordinary_setters_do_not_silently_reactivate_a_retired_row(self):
        # #232 review 6: only the MANAGE_SETUP-gated set_..._active can
        # reactivate a retired row — an ordinary value edit (a wider
        # permission than retire/reactivate) must not be a back door around
        # that gate.
        official = self._official()
        ref = f"official:{official}"
        contact = self.store.add_contact_destination(ContactDestination(
            id=self.store.next_id("contact"), recipient_ref=ref,
            channel=NotificationChannel.EMAIL, destination="original@example.com"))
        pref = self.store.save_notification_preference(NotificationPreference(
            id=self.store.next_id("notif_pref"), recipient_ref=ref,
            channel=NotificationChannel.PUSH, enabled=False))
        self.api.set_contact_destination_active(contact.id, False, actor_id=self.ACTOR)
        self.api.set_notification_preference_active(pref.id, False, actor_id=self.ACTOR)

        edited_contact = self.api.set_contact_destination(
            ref, "email", "edited@example.com")
        self.assertNotIn("error", edited_contact, edited_contact)
        self.assertEqual(edited_contact["destination"], "edited@example.com")
        self.assertFalse(edited_contact["active"],
                         "editing the value must not reactivate a retired contact")

        edited_pref = self.api.set_notification_preference(ref, "push", True)
        self.assertNotIn("error", edited_pref, edited_pref)
        self.assertTrue(edited_pref["enabled"])
        stored_pref = next(p for p in self.store.all_notification_preferences()
                          if p.id == pref.id)
        self.assertFalse(stored_pref.active,
                         "editing the value must not reactivate a retired preference")

    # -- official contact/preference retire-reactivate guard (#232 review 4) -
    # The same deactivate -> delete subject -> reactivate hole the device-
    # token guard closes applies identically to retiring a contact/
    # preference ahead of deletion: reactivating one whose Official is gone
    # must be rejected, exactly like set_device_token_active/
    # register_device_token.
    def test_reactivating_retired_contact_after_official_deleted_is_blocked(self):
        official = self._official()
        ref = f"official:{official}"
        contact = self.store.add_contact_destination(ContactDestination(
            id=self.store.next_id("contact"), recipient_ref=ref,
            channel=NotificationChannel.EMAIL, destination="react@example.com"))
        self.api.set_contact_destination_active(contact.id, False, actor_id=self.ACTOR)
        self.assertDeleted(self.api.delete_official(official, actor_id=self.ACTOR),
                           self.store.get_official, official, "official_deleted")
        result = self.api.set_contact_destination_active(
            contact.id, True, actor_id=self.ACTOR)
        self.assertEqual(result["error"]["code"], "validation_error", result)
        self.assertEqual(result["error"]["details"]["reason"], "scope_subject_missing")
        self.assertFalse(next(c for c in self.store.all_contact_destinations()
                              if c.id == contact.id).active)

    def test_reactivating_retired_preference_after_official_deleted_is_blocked(self):
        official = self._official()
        ref = f"official:{official}"
        pref = self.store.save_notification_preference(NotificationPreference(
            id=self.store.next_id("notif_pref"), recipient_ref=ref,
            channel=NotificationChannel.PUSH, enabled=False))
        self.api.set_notification_preference_active(pref.id, False, actor_id=self.ACTOR)
        self.assertDeleted(self.api.delete_official(official, actor_id=self.ACTOR),
                           self.store.get_official, official, "official_deleted")
        result = self.api.set_notification_preference_active(
            pref.id, True, actor_id=self.ACTOR)
        self.assertEqual(result["error"]["code"], "validation_error", result)
        self.assertEqual(result["error"]["details"]["reason"], "scope_subject_missing")
        self.assertFalse(next(p for p in self.store.all_notification_preferences()
                              if p.id == pref.id).active)

    def test_contact_retire_reactivate_lifecycle_allowed_for_existing_official(self):
        official = self._official()
        ref = f"official:{official}"
        c = self.api.set_contact_destination(ref, "email", "lifecycle@example.com")
        self.assertNotIn("error", c, c)
        contact_id = self.store.get_contact_destination(
            ref, NotificationChannel.EMAIL).id
        retired = self.api.set_contact_destination_active(contact_id, False)
        self.assertFalse(retired["active"])
        reactivated = self.api.set_contact_destination_active(contact_id, True)
        self.assertNotIn("error", reactivated, reactivated)
        self.assertTrue(reactivated["active"])

    def test_preference_retire_reactivate_lifecycle_allowed_for_existing_official(self):
        official = self._official()
        ref = f"official:{official}"
        self.api.set_notification_preference(ref, "email", False)
        pref_id = self.store.get_notification_preference(
            ref, NotificationChannel.EMAIL).id
        retired = self.api.set_notification_preference_active(pref_id, False)
        self.assertFalse(retired["active"])
        reactivated = self.api.set_notification_preference_active(pref_id, True)
        self.assertNotIn("error", reactivated, reactivated)
        self.assertTrue(reactivated["active"])

    def test_retire_contact_rolls_back_on_forced_audit_failure(self):
        # #232 review 6: retire/reactivate + its audit must be one atomic
        # unit — a forced audit failure must leave the lifecycle flag
        # exactly as it was, not silently changed with no audit record.
        official = self._official()
        ref = f"official:{official}"
        contact = self.store.add_contact_destination(ContactDestination(
            id=self.store.next_id("contact"), recipient_ref=ref,
            channel=NotificationChannel.EMAIL, destination="atomic@example.com"))
        audits_before = len(self.store.all_setup_audit())
        original = self.store.add_setup_audit

        def boom(entry):
            raise RuntimeError("forced audit failure")

        self.store.add_setup_audit = boom
        try:
            with self.assertRaises(RuntimeError):
                self.api.set_contact_destination_active(
                    contact.id, False, actor_id=self.ACTOR)
        finally:
            self.store.add_setup_audit = original

        self.assertEqual(len(self.store.all_setup_audit()), audits_before)
        stored = next(c for c in self.store.all_contact_destinations()
                     if c.id == contact.id)
        self.assertTrue(stored.active, "the forced failure must roll back "
                        "the active flag, not just skip the audit")

    def test_retire_preference_rolls_back_on_forced_audit_failure(self):
        official = self._official()
        ref = f"official:{official}"
        pref = self.store.save_notification_preference(NotificationPreference(
            id=self.store.next_id("notif_pref"), recipient_ref=ref,
            channel=NotificationChannel.PUSH, enabled=False))
        audits_before = len(self.store.all_setup_audit())
        original = self.store.add_setup_audit

        def boom(entry):
            raise RuntimeError("forced audit failure")

        self.store.add_setup_audit = boom
        try:
            with self.assertRaises(RuntimeError):
                self.api.set_notification_preference_active(
                    pref.id, False, actor_id=self.ACTOR)
        finally:
            self.store.add_setup_audit = original

        self.assertEqual(len(self.store.all_setup_audit()), audits_before)
        stored = next(p for p in self.store.all_notification_preferences()
                     if p.id == pref.id)
        self.assertTrue(stored.active, "the forced failure must roll back "
                        "the active flag, not just skip the audit")

    def test_retired_contact_is_never_resolved_even_while_subject_still_exists(self):
        # #232 review 6: retirement must make a contact genuinely non-live —
        # not merely stop it from blocking the delete — independent of
        # whether the subject has been deleted yet.
        from hockey_scheduler.services.delivery import resolve_destination

        official = self._official()
        ref = f"official:{official}"
        contact = self.store.add_contact_destination(ContactDestination(
            id=self.store.next_id("contact"), recipient_ref=ref,
            channel=NotificationChannel.EMAIL, destination="live@example.com"))
        self.assertEqual(
            resolve_destination(self.store, ref, NotificationChannel.EMAIL),
            "live@example.com")
        self.api.set_contact_destination_active(contact.id, False, actor_id=self.ACTOR)
        resolved = resolve_destination(self.store, ref, NotificationChannel.EMAIL)
        self.assertNotEqual(resolved, "live@example.com")
        self.assertTrue(resolved.endswith("@notify.invalid"))

    def test_pending_delivery_cannot_be_redirected_after_subject_deleted(self):
        """#232 review 6: a queued delivery re-resolves its destination on
        every send attempt (services/delivery.py). A retired contact
        (active=False) is never resolved to — retiring one to clear a
        Player/Official delete's dependency must make it actually
        non-live, not just stop it from blocking the delete — so the
        sequence enqueue -> retire contact -> delete subject ->
        process/retry delivery must fall back to the synthesized
        placeholder, never the retired real address and never a NEW,
        attacker-controlled one."""
        from hockey_scheduler.services.delivery import enqueue

        official = self._official()
        ref = f"official:{official}"
        contact = self.store.add_contact_destination(ContactDestination(
            id=self.store.next_id("contact"), recipient_ref=ref,
            channel=NotificationChannel.EMAIL, destination="original@example.com"))
        notification = Notification(
            id=self.store.next_id("notif"), kind=NotificationKind.ASSIGNMENT_OFFERED,
            audience=NotificationAudience.OFFICIAL, audience_ref=official,
            title="Offer", message="You have an assignment offer",
            at=datetime(2027, 1, 1, tzinfo=timezone.utc))
        self.store.add_notification_feed(notification)
        deliveries = enqueue(self.store, notification)
        email_delivery = next(d for d in deliveries
                              if d.channel == NotificationChannel.EMAIL)
        self.assertEqual(email_delivery.destination, "original@example.com")

        # Retire (never delete) the contact to clear the way, then delete.
        self.api.set_contact_destination_active(contact.id, False, actor_id=self.ACTOR)
        self.assertDeleted(self.api.delete_official(official, actor_id=self.ACTOR),
                           self.store.get_official, official, "official_deleted")

        # The hijack attempt: point a fresh contact at the now-dead ref.
        before = len(self.store.all_contact_destinations())
        hijack = self.api.set_contact_destination(
            ref, "email", "attacker@example.com")
        self.assertEqual(hijack["error"]["code"], "validation_error", hijack)
        self.assertEqual(len(self.store.all_contact_destinations()), before)

        # Draining the queue re-resolves the destination (delivery.py's
        # retry-time re-resolution): the retired contact is skipped entirely
        # now, so this falls back to the placeholder — never the retired
        # real address and never the attacker's.
        self.api.delivery.process_pending()
        redelivered = self.store.get_notification_delivery(email_delivery.id)
        self.assertNotEqual(redelivered.destination, "original@example.com")
        self.assertNotEqual(redelivered.destination, "attacker@example.com")
        self.assertTrue(redelivered.destination.endswith("@notify.invalid"))

    # -- player (#232) --------------------------------------------------------
    def test_player_blocked_by_roster_entry(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self._player(team)
        gid = self._built_game()
        self.store.add_roster_entry(GameRosterEntry(
            id=self.store.next_id("roster_entry"), game_id=gid, player_id=player,
            roster_role=RosterRole.SELECTED,
            selection_source=SelectionSource.COACH_SELECTED,
            status=RosterEntryStatus.SELECTED,
            selected_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2027, 1, 1, tzinfo=timezone.utc)))
        blocked = self.api.delete_player(player, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"roster entry"})
        self.assertIsNotNone(self.store.get_player(player))

    def test_player_blocked_by_availability_response(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self._player(team)
        gid = self._built_game()
        self.store.upsert_availability(GameAvailability(
            id=self.store.next_id("availability"), game_id=gid, player_id=player,
            availability_status=AvailabilityStatus.AVAILABLE))
        blocked = self.api.delete_player(player, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"availability response"})
        self.assertIsNotNone(self.store.get_player(player))

    def test_player_blocked_by_substitute_enrollment(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self._player(team)
        gid = self._built_game()
        self.store.add_substitute(SubstituteEnrollment(
            id=self.store.next_id("substitute"), game_id=gid, player_id=player,
            position=Position.SKATER, status=SubstituteStatus.ENROLLED,
            enrolled_at=datetime(2027, 1, 1, tzinfo=timezone.utc)))
        blocked = self.api.delete_player(player, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"substitute enrolment"})
        self.assertIsNotNone(self.store.get_player(player))

    def test_player_blocked_by_guardian_link(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self._player(team)
        self.store.add_guardian_link(GuardianLink(
            id=self.store.next_id("guardian_link"), guardian_user_id="user_guardian",
            player_id=player, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            verified=True))
        blocked = self.api.delete_player(player, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"guardian link"})
        self.assertIsNotNone(self.store.get_player(player))

    def test_player_blocked_by_account(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self._player(team)
        self.api.accounts.create_account(
            "player_x", "a-real-password", Role.PLAYER,
            scope={"team_id": team, "player_id": player}, actor_id=self.ACTOR)
        blocked = self.api.delete_player(player, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"account"})
        self.assertIsNotNone(self.store.get_player(player))

    def test_player_blocked_by_live_calendar_feed(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self._player(team)
        self.store.add_calendar_feed_token(CalendarFeedToken(
            id=self.store.next_id("cft"), token_hash="hash_p",
            actor_type="player", actor_ref=player,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
        blocked = self.api.delete_player(player, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"calendar feed"})
        self.assertIsNotNone(self.store.get_player(player))

    def test_player_blocked_by_contact_destination(self):
        # The real production path: an optional email on manual player create
        # writes a "player:<id>"-scoped ContactDestination (#232 review of
        # #215's recipient_ref convention).
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self.api.create_player(
            team, "Emailed Player", "skater", email="p@example.com",
            actor_id=self.ACTOR)["id"]
        blocked = self.api.delete_player(player, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"contact destination"})
        self.assertIsNotNone(self.store.get_player(player))

    def test_player_blocked_by_notification_preference(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self._player(team)
        self.store.save_notification_preference(NotificationPreference(
            id=self.store.next_id("notif_pref"), recipient_ref=f"player:{player}",
            channel=NotificationChannel.PUSH, enabled=False))
        blocked = self.api.delete_player(player, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"notification preference"})
        self.assertIsNotNone(self.store.get_player(player))

    def test_player_emailed_contact_retired_then_deletes(self):
        # The common real-world shape (#232 review): the emailed Player's
        # contact is retired — never deleted — to clear the way; the delete
        # then succeeds and the retired row remains queryable history.
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self.api.create_player(
            team, "Emailed Player", "skater", email="p@example.com",
            actor_id=self.ACTOR)["id"]
        ref = f"player:{player}"
        contact = self.store.get_contact_destination(ref, NotificationChannel.EMAIL)
        self.assertIsNotNone(contact)
        self.api.set_contact_destination_active(contact.id, False, actor_id=self.ACTOR)
        result = self.api.delete_player(player, actor_id=self.ACTOR)
        self.assertNotIn("error", result, result)
        self.assertIsNone(self.store.get_player(player))
        still_there = next(
            (c for c in self.store.all_contact_destinations() if c.id == contact.id),
            None)
        self.assertIsNotNone(still_there, "retired history must remain queryable")
        self.assertFalse(still_there.active)
        self.assertEqual(still_there.destination, "p@example.com")

    def test_player_inactive_contact_destination_does_not_block(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self._player(team)
        self.store.add_contact_destination(ContactDestination(
            id=self.store.next_id("contact"), recipient_ref=f"player:{player}",
            channel=NotificationChannel.EMAIL, destination="inactive@example.com",
            active=False))
        self.assertDeleted(self.api.delete_player(player, actor_id=self.ACTOR),
                           self.store.get_player, player, "player_deleted")

    def test_player_inactive_notification_preference_does_not_block(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self._player(team)
        self.store.save_notification_preference(NotificationPreference(
            id=self.store.next_id("notif_pref"), recipient_ref=f"player:{player}",
            channel=NotificationChannel.PUSH, enabled=False, active=False))
        self.assertDeleted(self.api.delete_player(player, actor_id=self.ACTOR),
                           self.store.get_player, player, "player_deleted")

    def test_player_blocked_by_device_token(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self._player(team)
        self.store.add_device_token(DeviceToken(
            id=self.store.next_id("device"), recipient_ref=f"player:{player}",
            provider="fcm", token="tok_p"))
        blocked = self.api.delete_player(player, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"device token"})
        self.assertIsNotNone(self.store.get_player(player))

    def test_player_deletable_when_bare(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self._player(team)
        self.assertDeleted(self.api.delete_player(player, actor_id=self.ACTOR),
                           self.store.get_player, player, "player_deleted")

    # -- player dependency lifecycle (#232 review) ---------------------------
    def test_player_inactive_account_does_not_block(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self._player(team)
        acc = self.api.accounts.create_account(
            "player_inactive", "a-real-password", Role.PLAYER,
            scope={"team_id": team, "player_id": player}, actor_id=self.ACTOR)
        self.api.accounts.set_active(acc.id, False, actor_id=self.ACTOR)
        self.assertDeleted(self.api.delete_player(player, actor_id=self.ACTOR),
                           self.store.get_player, player, "player_deleted")

    def test_player_inactive_device_token_does_not_block(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self._player(team)
        self.store.add_device_token(DeviceToken(
            id=self.store.next_id("device"), recipient_ref=f"player:{player}",
            provider="fcm", token="tok_p_inactive", active=False))
        self.assertDeleted(self.api.delete_player(player, actor_id=self.ACTOR),
                           self.store.get_player, player, "player_deleted")

    def test_reactivating_account_after_player_deleted_is_blocked(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self._player(team)
        acc = self.api.accounts.create_account(
            "player_rebind", "a-real-password", Role.PLAYER,
            scope={"team_id": team, "player_id": player}, actor_id=self.ACTOR)
        self.api.accounts.set_active(acc.id, False, actor_id=self.ACTOR)
        self.assertDeleted(self.api.delete_player(player, actor_id=self.ACTOR),
                           self.store.get_player, player, "player_deleted")
        result = self.api.set_user_account_active(acc.id, True, actor_id=self.ACTOR)
        self.assertEqual(result["error"]["code"], "validation_error")
        self.assertFalse(self.store.get_user_account(acc.id).active)

    def test_creating_account_for_deleted_player_is_rejected(self):
        # Mirrors test_creating_account_for_deleted_official_is_rejected
        # (#232 review 7) for the Player side of the same hole.
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self._player(team)
        self.assertDeleted(self.api.delete_player(player, actor_id=self.ACTOR),
                           self.store.get_player, player, "player_deleted")
        accounts_before = len(self.store.all_user_accounts())
        audits_before = len(self.store.all_setup_audit())
        result = self.api.create_user_account(
            "player_ghost", "a-real-password", "player",
            scope={"team_id": team, "player_id": player}, actor_id=self.ACTOR)
        self.assertEqual(result["error"]["code"], "validation_error")
        self.assertEqual(result["error"]["details"]["reason"], "scope_subject_missing")
        self.assertEqual(len(self.store.all_user_accounts()), accounts_before)
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)
        self.assertIsNone(self.store.get_user_account_by_username("player_ghost"))

    def test_creating_account_for_existing_player_still_works(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self._player(team)
        result = self.api.create_user_account(
            "player_live", "a-real-password", "player",
            scope={"team_id": team, "player_id": player}, actor_id=self.ACTOR)
        self.assertNotIn("error", result, result)
        self.assertTrue(result["active"])

    def test_player_notification_preference_retired_then_deletes(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self._player(team)
        ref = f"player:{player}"
        pref = self.store.save_notification_preference(NotificationPreference(
            id=self.store.next_id("notif_pref"), recipient_ref=ref,
            channel=NotificationChannel.PUSH, enabled=False))
        blocked = self.api.delete_player(player, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"notification preference"})

        self.api.set_notification_preference_active(pref.id, False, actor_id=self.ACTOR)
        before_delete_audits = len(self.store.all_setup_audit())
        result = self.api.delete_player(player, actor_id=self.ACTOR)
        self.assertNotIn("error", result, result)
        self.assertEqual(len(self.store.all_setup_audit()), before_delete_audits + 1,
                         "a successful delete writes exactly one audit entry")
        self.assertIsNone(self.store.get_player(player))
        still_there = next(
            (p for p in self.store.all_notification_preferences() if p.id == pref.id),
            None)
        self.assertIsNotNone(still_there, "retired history must remain queryable")
        self.assertFalse(still_there.active)
        self.assertFalse(still_there.enabled)

    def test_player_blocked_delete_preserves_notification_preference(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self._player(team)
        ref = f"player:{player}"
        self.store.add_device_token(DeviceToken(
            id=self.store.next_id("device"), recipient_ref=ref,
            provider="fcm", token="tok_p_block"))
        pref = self.store.save_notification_preference(NotificationPreference(
            id=self.store.next_id("notif_pref"), recipient_ref=ref,
            channel=NotificationChannel.PUSH, enabled=False))
        audits_before = len(self.store.all_setup_audit())
        blocked = self.api.delete_player(player, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={"device token", "notification preference"})
        self.assertIsNotNone(self.store.get_player(player))
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)
        stored = self.store.get_notification_preference(
            ref, NotificationChannel.PUSH)
        self.assertIsNotNone(stored, "a blocked delete must not touch the "
                             "recipient's stored opt-out")
        self.assertFalse(stored.enabled)
        self.assertTrue(stored.active)
        self.assertEqual(stored.id, pref.id)

    def test_player_full_dependency_lifecycle_then_succeeds(self):
        """#232 review: the real emailed-Player path — the common case a
        League Admin actually hits — has a genuine supported resolution for
        EVERY dependency type it can accumulate simultaneously, not just a
        separate bare Player deleted instead. #232 review 4: contact/
        preference dependencies are cleared by retiring them, never by
        deleting them."""
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self.api.create_player(
            team, "Lifecycle Player", "skater", email="lifecycle@example.com",
            actor_id=self.ACTOR)["id"]
        ref = f"player:{player}"
        acc = self.api.accounts.create_account(
            "player_lifecycle", "a-real-password", Role.PLAYER,
            scope={"team_id": team, "player_id": player}, actor_id=self.ACTOR)
        tok = self.store.add_device_token(DeviceToken(
            id=self.store.next_id("device"), recipient_ref=ref,
            provider="fcm", token="tok_lifecycle"))
        pref = self.store.save_notification_preference(NotificationPreference(
            id=self.store.next_id("notif_pref"), recipient_ref=ref,
            channel=NotificationChannel.PUSH, enabled=False))

        audits_before = len(self.store.all_setup_audit())
        blocked = self.api.delete_player(player, actor_id=self.ACTOR)
        self.assertBlocked(blocked, expect_types={
            "account", "device token", "contact destination",
            "notification preference"})
        self.assertIsNotNone(self.store.get_player(player))
        self.assertEqual(len(self.store.all_setup_audit()), audits_before,
                         "a blocked delete must not append any audit row")

        # Clear each dependency through its own supported route — never a
        # silent cascade or erasure from delete_player itself.
        self.api.accounts.set_active(acc.id, False, actor_id=self.ACTOR)
        self.api.set_device_token_active(tok.id, False)
        contact = next(c for c in self.store.all_contact_destinations()
                      if c.recipient_ref == ref)
        self.api.set_contact_destination_active(contact.id, False, actor_id=self.ACTOR)
        self.api.set_notification_preference_active(pref.id, False, actor_id=self.ACTOR)

        self.assertDeleted(self.api.delete_player(player, actor_id=self.ACTOR),
                           self.store.get_player, player, "player_deleted")
        # Nothing erased — every dependent row survives as retired history.
        self.assertIsNotNone(next(
            (c for c in self.store.all_contact_destinations() if c.id == contact.id),
            None))
        self.assertIsNotNone(next(
            (p for p in self.store.all_notification_preferences() if p.id == pref.id),
            None))

    # -- player dangling-recipient guard (#232 review 2) ---------------------
    def test_reactivating_device_token_after_player_deleted_is_blocked(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self._player(team)
        ref = f"player:{player}"
        tok = self.api.register_device_token(ref, "fcm", "tok_p_react")
        self.api.set_device_token_active(tok["id"], False)
        self.assertDeleted(self.api.delete_player(player, actor_id=self.ACTOR),
                           self.store.get_player, player, "player_deleted")
        result = self.api.set_device_token_active(tok["id"], True)
        self.assertEqual(result["error"]["code"], "validation_error", result)
        self.assertFalse(self.store.get_device_token(tok["id"]).active)

    def test_reregistering_device_token_after_player_deleted_is_blocked(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self._player(team)
        ref = f"player:{player}"
        tok = self.api.register_device_token(ref, "fcm", "tok_p_reg")
        self.api.set_device_token_active(tok["id"], False)
        self.assertDeleted(self.api.delete_player(player, actor_id=self.ACTOR),
                           self.store.get_player, player, "player_deleted")
        result = self.api.register_device_token(ref, "fcm", "tok_p_reg")
        self.assertEqual(result["error"]["code"], "validation_error", result)
        self.assertFalse(self.store.get_device_token(tok["id"]).active)
        result = self.api.register_device_token(ref, "fcm", "tok_p_brand_new")
        self.assertEqual(result["error"]["code"], "validation_error", result)

    def test_device_token_lifecycle_allowed_for_existing_player(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self._player(team)
        ref = f"player:{player}"
        tok = self.api.register_device_token(ref, "fcm", "tok_p_live")
        self.assertNotIn("error", tok)
        self.assertFalse(self.api.set_device_token_active(tok["id"], False)["active"])
        reactivated = self.api.set_device_token_active(tok["id"], True)
        self.assertNotIn("error", reactivated)
        self.assertTrue(reactivated["active"])

    # -- player contact/preference re-creation guard (#232 review 3) --------
    def test_contact_destination_rejects_recreation_after_player_deleted(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self._player(team)
        ref = f"player:{player}"
        self.assertDeleted(self.api.delete_player(player, actor_id=self.ACTOR),
                           self.store.get_player, player, "player_deleted")
        before = len(self.store.all_contact_destinations())
        result = self.api.set_contact_destination(ref, "email", "dead@example.com")
        self.assertEqual(result["error"]["code"], "validation_error", result)
        self.assertEqual(result["error"]["details"]["reason"], "scope_subject_missing")
        self.assertEqual(len(self.store.all_contact_destinations()), before)

    def test_notification_preference_rejects_recreation_after_player_deleted(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self._player(team)
        ref = f"player:{player}"
        self.assertDeleted(self.api.delete_player(player, actor_id=self.ACTOR),
                           self.store.get_player, player, "player_deleted")
        before = len(self.store.all_notification_preferences())
        result = self.api.set_notification_preference(ref, "email", False)
        self.assertEqual(result["error"]["code"], "validation_error", result)
        self.assertEqual(result["error"]["details"]["reason"], "scope_subject_missing")
        self.assertEqual(len(self.store.all_notification_preferences()), before)

    def test_contact_destination_still_settable_for_existing_player(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self._player(team)
        ref = f"player:{player}"
        result = self.api.set_contact_destination(ref, "email", "live@example.com")
        self.assertNotIn("error", result, result)
        self.assertEqual(result["destination"], "live@example.com")

    def test_other_recipient_shapes_unaffected_by_dangling_guard(self):
        # team:/guardian:-scoped refs never pass through the Player/Official
        # existence check at all (#232 review 3) — only structured
        # player:<id>/official:<id> refs are validated.
        result = self.api.set_contact_destination("team:no-such-team", "email",
                                                   "team@example.com")
        self.assertNotIn("error", result, result)
        result2 = self.api.set_notification_preference("guardian:no-such-user",
                                                        "email", False)
        self.assertNotIn("error", result2, result2)

    # -- player contact/preference retire-reactivate guard (#232 review 4) ---
    def test_reactivating_retired_contact_after_player_deleted_is_blocked(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self._player(team)
        ref = f"player:{player}"
        contact = self.store.add_contact_destination(ContactDestination(
            id=self.store.next_id("contact"), recipient_ref=ref,
            channel=NotificationChannel.EMAIL, destination="react@example.com"))
        self.api.set_contact_destination_active(contact.id, False, actor_id=self.ACTOR)
        self.assertDeleted(self.api.delete_player(player, actor_id=self.ACTOR),
                           self.store.get_player, player, "player_deleted")
        result = self.api.set_contact_destination_active(
            contact.id, True, actor_id=self.ACTOR)
        self.assertEqual(result["error"]["code"], "validation_error", result)
        self.assertEqual(result["error"]["details"]["reason"], "scope_subject_missing")

    def test_reactivating_retired_preference_after_player_deleted_is_blocked(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self._player(team)
        ref = f"player:{player}"
        pref = self.store.save_notification_preference(NotificationPreference(
            id=self.store.next_id("notif_pref"), recipient_ref=ref,
            channel=NotificationChannel.PUSH, enabled=False))
        self.api.set_notification_preference_active(pref.id, False, actor_id=self.ACTOR)
        self.assertDeleted(self.api.delete_player(player, actor_id=self.ACTOR),
                           self.store.get_player, player, "player_deleted")
        result = self.api.set_notification_preference_active(
            pref.id, True, actor_id=self.ACTOR)
        self.assertEqual(result["error"]["code"], "validation_error", result)
        self.assertEqual(result["error"]["details"]["reason"], "scope_subject_missing")

    def test_player_delete_rolls_back_on_forced_audit_failure(self):
        lg = self._league()
        club = self._club()
        team = self._team(club, lg)
        player = self._player(team)
        audits_before = len(self.store.all_setup_audit())
        original = self.store.add_setup_audit

        def boom(entry):
            raise RuntimeError("forced audit failure")

        self.store.add_setup_audit = boom
        try:
            with self.assertRaises(RuntimeError):
                self.api.delete_player(player, actor_id=self.ACTOR)
        finally:
            self.store.add_setup_audit = original

        self.assertIsNotNone(self.store.get_player(player))
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)

    # -- not found ---------------------------------------------------------
    def test_missing_ids_report_not_found(self):
        for fn in (self.api.delete_organization, self.api.delete_program,
                   self.api.delete_season, self.api.delete_league,
                   self.api.delete_division, self.api.delete_club,
                   self.api.delete_team, self.api.delete_venue,
                   self.api.delete_rink, self.api.delete_ice_slot,
                   self.api.delete_game,
                   self.api.delete_season_team_registration,
                   self.api.delete_official, self.api.delete_player):
            result = fn("nope_1", actor_id=self.ACTOR)
            self.assertEqual(result["error"]["code"], "not_found")

    # -- audit content -----------------------------------------------------
    def test_blocked_delete_writes_no_audit(self):
        lg = self._league()
        self._season(lg)
        before = len(self.store.all_setup_audit())
        self.api.delete_program(lg, actor_id=self.ACTOR)
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
