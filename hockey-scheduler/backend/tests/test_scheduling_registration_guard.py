"""Scheduling & standings resolve participation via SeasonTeamRegistration (#180).

A team is a permanent league member; the division it plays in each season lives
in a SeasonTeamRegistration, not on Team.division_id. These tests pin the shared
season-registration guard that game creation, moves, publishing, standings, and
draft scheduling all go through: a team may only be scheduled in a season+
division it is actively registered in, and the standings roster is the set of
registered teams — the legacy Team.division_id is no longer consulted.
"""

import unittest
from datetime import datetime, timezone

from helpers import FakeClock

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import IceSlotStatus, SeasonTeamRegistration
from hockey_scheduler.domain.errors import DivisionMismatchError
from hockey_scheduler.services import SetupService
from hockey_scheduler.services.league_scoped_setup_service import (
    SetupService as LeagueScopedSetupService)
from hockey_scheduler.services.scheduler import draft_schedule
from hockey_scheduler.store import InMemoryStore

UTC = timezone.utc


def dt(h, m=0):
    return datetime(2026, 9, 1, h, m, tzinfo=UTC)


class _Base(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore()
        self.svc = SetupService(self.store, clock=FakeClock())
        self.league = self.svc.create_league("L")
        self.season = self.svc.create_season(self.league.id, "S")
        self.div = self.svc.create_division(self.season.id, "U16")
        self.other_div = self.svc.create_division(self.season.id, "U18")
        club = self.svc.create_club("C")
        self.home = self.svc.create_team(club.id, self.div.id, "Home")
        self.away = self.svc.create_team(club.id, self.div.id, "Away")
        self.venue = self.svc.create_venue("V", league_id=self.league.id)
        self.rink = self.svc.create_rink(self.venue.id, "R")

    def _slot(self, h=18):
        return self.svc.create_ice_slot(self.rink.id, dt(h), dt(h + 2))

    def _register_both(self, division=None):
        division = division or self.div
        self.svc.register_team_for_season(self.season.id, self.home.id, division.id)
        self.svc.register_team_for_season(self.season.id, self.away.id, division.id)

    def _assert_no_write(self, slot):
        # Every rejection is pre-write: no game exists and the slot is untouched.
        self.assertEqual(self.store.all_games(), [])
        self.assertEqual(self.store.get_ice_slot(slot.id).status,
                         IceSlotStatus.AVAILABLE)


class CreateGameGuardTest(_Base):
    def test_unregistered_teams_cannot_be_scheduled(self):
        # Neither team is registered — create_game reads the registration, not
        # Team.division_id (which IS set), so it is rejected.
        with self.assertRaises(DivisionMismatchError):
            self.svc.create_game(self.season.id, self.div.id,
                                 self.home.id, self.away.id, self._slot().id)

    def test_registered_teams_can_be_scheduled(self):
        self._register_both()
        game = self.svc.create_game(self.season.id, self.div.id,
                                    self.home.id, self.away.id, self._slot().id)
        self.assertEqual(game.home_team_id, self.home.id)
        self.assertEqual(game.division_id, self.div.id)

    def test_team_registered_in_a_different_division_is_rejected(self):
        # Registered — but in U18, while the game is in U16.
        self.svc.register_team_for_season(self.season.id, self.home.id, self.div.id)
        self.svc.register_team_for_season(self.season.id, self.away.id, self.other_div.id)
        with self.assertRaises(DivisionMismatchError):
            self.svc.create_game(self.season.id, self.div.id,
                                 self.home.id, self.away.id, self._slot().id)

    def test_override_relaxes_division_but_requires_registration(self):
        # A cross-division exhibition: both teams ARE registered this season,
        # just in different divisions. Override permits the game.
        self.svc.register_team_for_season(self.season.id, self.home.id, self.div.id)
        self.svc.register_team_for_season(self.season.id, self.away.id, self.other_div.id)
        game = self.svc.create_game(self.season.id, self.div.id,
                                    self.home.id, self.away.id, self._slot().id,
                                    allow_division_override=True)
        self.assertEqual(game.away_team_id, self.away.id)

    def test_override_still_rejects_unregistered_teams(self):
        # #200 review: the override relaxes only the division match — an
        # unregistered team can never be scheduled, and nothing is written.
        slot = self._slot()
        with self.assertRaises(DivisionMismatchError):
            self.svc.create_game(self.season.id, self.div.id,
                                 self.home.id, self.away.id, slot.id,
                                 allow_division_override=True)
        self._assert_no_write(slot)

    def test_inactive_registration_does_not_count(self):
        self._register_both()
        reg = self.store.registration_for_team_in_season(self.season.id, self.away.id)
        self.svc.unregister_team_from_season(reg.id)  # away leaves the season
        slot = self._slot()
        with self.assertRaises(DivisionMismatchError):
            self.svc.create_game(self.season.id, self.div.id,
                                 self.home.id, self.away.id, slot.id)
        self._assert_no_write(slot)

    def test_cross_league_registration_row_is_rejected(self):
        # An injected/legacy registration whose Team belongs to another league
        # must not be trusted (#199/#200): the game is rejected.
        other_league = self.svc.create_league("Other")
        other_season = self.svc.create_season(other_league.id, "OS")
        other_div = self.svc.create_division(other_season.id, "OD")
        foreign = self.svc.create_team(
            self.svc.create_club("FC").id, other_div.id, "Foreign")
        # Foreign is home's opponent; register home legitimately, and inject a
        # cross-league row for foreign into THIS season.
        self.svc.register_team_for_season(self.season.id, self.home.id, self.div.id)
        self.store.add_season_team_registration(SeasonTeamRegistration(
            id="streg_injected", season_id=self.season.id, team_id=foreign.id,
            division_id=self.div.id, active=True))
        slot = self._slot()
        with self.assertRaises(DivisionMismatchError):
            self.svc.create_game(self.season.id, self.div.id,
                                 self.home.id, foreign.id, slot.id)
        self._assert_no_write(slot)


class MoveAndPublishGuardTest(_Base):
    def _game(self):
        self._register_both()
        return self.svc.create_game(self.season.id, self.div.id,
                                    self.home.id, self.away.id, self._slot(18).id)

    def test_move_requires_both_teams_still_registered(self):
        game = self._game()
        target = self._slot(21)
        # Away is unregistered after the game exists — the move is refused.
        reg = self.store.registration_for_team_in_season(self.season.id, self.away.id)
        reg.active = False
        self.store.save_season_team_registration(reg)
        with self.assertRaises(DivisionMismatchError):
            self.svc.move_game(game.id, target.id)

    def test_move_succeeds_while_registered(self):
        game = self._game()
        target = self._slot(21)
        moved = self.svc.move_game(game.id, target.id)
        self.assertEqual(moved.ice_slot_id, target.id)

    def test_publish_requires_both_teams_registered(self):
        game = self._game()
        reg = self.store.registration_for_team_in_season(self.season.id, self.home.id)
        reg.active = False
        self.store.save_season_team_registration(reg)
        with self.assertRaises(DivisionMismatchError):
            self.svc.publish_game(game.id)

    def test_unpublish_is_not_guarded(self):
        # A fixture that became invalid must still be pullable from public view.
        game = self._game()
        self.svc.publish_game(game.id)
        reg = self.store.registration_for_team_in_season(self.season.id, self.home.id)
        reg.active = False
        self.store.save_season_team_registration(reg)
        unpub = self.svc.publish_game(game.id, published=False)
        self.assertFalse(unpub.published)


class StandingsRosterTest(_Base):
    def test_standings_roster_is_the_registered_teams_not_division_id(self):
        api = ApiService(self.store)
        # Both teams have Team.division_id == div (from create_team) but only
        # home is registered — standings must list exactly the registered team.
        self.svc.register_team_for_season(self.season.id, self.home.id, self.div.id)
        standings = api.get_standings(self.div.id)["standings"]
        ids = {r["team_id"] for r in standings}
        self.assertEqual(ids, {self.home.id})

    def test_registering_a_team_adds_it_to_standings(self):
        api = ApiService(self.store)
        self._register_both()
        ids = {r["team_id"] for r in api.get_standings(self.div.id)["standings"]}
        self.assertEqual(ids, {self.home.id, self.away.id})

    def test_orphan_and_cross_league_rows_are_excluded_from_standings(self):
        # Injected orphan (no Team) and cross-league rows must not be trusted
        # into the standings roster (#199/#200).
        api = ApiService(self.store)
        self._register_both()
        self.store.add_season_team_registration(SeasonTeamRegistration(
            id="streg_orphan", season_id=self.season.id, team_id="ghost",
            division_id=self.div.id, active=True))
        other = self.svc.create_league("Other")
        foreign = self.svc.create_team(
            self.svc.create_club("FC").id,
            self.svc.create_division(
                self.svc.create_season(other.id, "OS").id, "OD").id, "Foreign")
        self.store.add_season_team_registration(SeasonTeamRegistration(
            id="streg_foreign", season_id=self.season.id, team_id=foreign.id,
            division_id=self.div.id, active=True))
        ids = {r["team_id"] for r in api.get_standings(self.div.id)["standings"]}
        self.assertEqual(ids, {self.home.id, self.away.id})


class DraftScheduleGuardTest(_Base):
    def test_draft_pairs_only_registered_teams(self):
        # A third team sits in the division by legacy Team.division_id but is
        # not registered; the draft round-robin must ignore it.
        self.svc.create_team(self.svc.create_club("C3").id, self.div.id, "Ghost")
        self._register_both()
        self._slot(18)
        result = draft_schedule(self.store, self.div.id, constraints=None)
        self.assertEqual(result["team_count"], 2)
        paired = {result["draft_games"][0]["home_team_id"],
                  result["draft_games"][0]["away_team_id"]}
        self.assertEqual(paired, {self.home.id, self.away.id})

    def test_orphan_and_cross_league_rows_do_not_enter_drafts(self):
        self._register_both()
        self._slot(18)
        self.store.add_season_team_registration(SeasonTeamRegistration(
            id="streg_orphan", season_id=self.season.id, team_id="ghost",
            division_id=self.div.id, active=True))
        result = draft_schedule(self.store, self.div.id, constraints=None)
        self.assertEqual(result["team_count"], 2)
        scheduled = set()
        for g in result["draft_games"]:
            scheduled |= {g["home_team_id"], g["away_team_id"]}
        self.assertNotIn("ghost", scheduled)


class LeagueScopedIceGuardTest(unittest.TestCase):
    """A league-first team (Team.division_id is None) that is correctly
    registered via SeasonTeamRegistration must still receive the cross-league
    ice guard on create (#200 review, finding 2)."""

    def setUp(self):
        self.store = InMemoryStore()
        self.svc = LeagueScopedSetupService(self.store, clock=FakeClock())
        self.la = self.svc.create_league("A")
        self.sa = self.svc.create_season(self.la.id, "SA")
        self.da = self.svc.create_division(self.sa.id, "DA")
        club = self.svc.create_club("C")
        # League-first: no division on the Team, participation via registration.
        self.home = self.svc.create_team(club.id, name="Home", league_id=self.la.id)
        self.away = self.svc.create_team(club.id, name="Away", league_id=self.la.id)
        self.assertIsNone(self.home.division_id)
        self.svc.register_team_for_season(self.sa.id, self.home.id, self.da.id)
        self.svc.register_team_for_season(self.sa.id, self.away.id, self.da.id)
        # League B owns a different rink; League A owns its own.
        rb = self.svc.create_rink(
            self.svc.create_venue("VB", league_id=self.svc.create_league("B").id).id,
            "RB")
        self.foreign_slot = self.svc.create_ice_slot(rb.id, dt(18), dt(20))
        ra = self.svc.create_rink(
            self.svc.create_venue("VA", league_id=self.la.id).id, "RA")
        self.home_slot = self.svc.create_ice_slot(ra.id, dt(18), dt(20))

    def test_cross_league_ice_rejected_for_league_first_team(self):
        from hockey_scheduler.domain.errors import DomainError
        with self.assertRaises(DomainError):
            self.svc.create_game(self.sa.id, self.da.id, self.home.id,
                                 self.away.id, self.foreign_slot.id)
        self.assertEqual(self.store.all_games(), [])

    def test_same_league_ice_allowed(self):
        game = self.svc.create_game(self.sa.id, self.da.id, self.home.id,
                                    self.away.id, self.home_slot.id)
        self.assertIsNotNone(game.id)


if __name__ == "__main__":
    unittest.main()
