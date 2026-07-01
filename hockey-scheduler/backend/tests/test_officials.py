import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import OfficialRole
from hockey_scheduler.domain.errors import (
    NotEligibleError,
    ScheduleConflictError,
    ValidationError,
)
from hockey_scheduler.api import ApiService
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.services import SetupService
from hockey_scheduler.store import InMemoryStore

UTC = timezone.utc


def dt(h, m=0):
    return datetime(2026, 9, 1, h, m, tzinfo=UTC)


class OfficialsServiceTest(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore()
        self.svc = SetupService(self.store)
        league = self.svc.create_league("L")
        season = self.svc.create_season(league.id, "S")
        self.div = self.svc.create_division(season.id, "U16")
        self.club_a = self.svc.create_club("Aces HC")
        self.club_b = self.svc.create_club("Bees HC")
        self.home = self.svc.create_team(self.club_a.id, self.div.id, "Aces")
        self.away = self.svc.create_team(self.club_b.id, self.div.id, "Bees")
        venue = self.svc.create_venue("Arena")
        self.rink = self.svc.create_rink(venue.id, "Rink 1")
        self.rink2 = self.svc.create_rink(venue.id, "Rink 2")
        # A second matchup (distinct teams) so an overlapping second game does
        # not trip the create_game team-overlap rule.
        self.club_c = self.svc.create_club("Cats HC")
        self.club_d = self.svc.create_club("Dogs HC")
        self.home2 = self.svc.create_team(self.club_c.id, self.div.id, "Cats")
        self.away2 = self.svc.create_team(self.club_d.id, self.div.id, "Dogs")
        self.slot = self.svc.create_ice_slot(self.rink.id, dt(18), dt(20))
        self.game = self.svc.create_game(season.id, self.div.id,
                                         self.home.id, self.away.id, self.slot.id)
        self.season = season

    def test_assign_and_accept(self):
        off = self.svc.create_official("Ref One")
        a = self.svc.assign_official(self.game.id, off.id, OfficialRole.REFEREE)
        self.assertEqual(a.status.value, "proposed")
        accepted = self.svc.respond_assignment(a.id, accept=True)
        self.assertEqual(accepted.status.value, "accepted")

    def test_decline(self):
        off = self.svc.create_official("Ref Two")
        a = self.svc.assign_official(self.game.id, off.id, OfficialRole.LINESPERSON)
        declined = self.svc.respond_assignment(a.id, accept=False)
        self.assertEqual(declined.status.value, "declined")

    def test_respond_twice_rejected(self):
        off = self.svc.create_official("Ref Three")
        a = self.svc.assign_official(self.game.id, off.id, OfficialRole.REFEREE)
        self.svc.respond_assignment(a.id, accept=True)
        with self.assertRaises(ValidationError):
            self.svc.respond_assignment(a.id, accept=False)

    def test_duplicate_active_assignment_rejected(self):
        off = self.svc.create_official("Ref Dup")
        self.svc.assign_official(self.game.id, off.id, OfficialRole.REFEREE)
        with self.assertRaises(ScheduleConflictError):
            self.svc.assign_official(self.game.id, off.id, OfficialRole.LINESPERSON)

    def test_overlapping_game_rejected(self):
        # A second game overlapping in time on another rink; same official.
        slot2 = self.svc.create_ice_slot(self.rink2.id, dt(19), dt(21))
        game2 = self.svc.create_game(self.season.id, self.div.id,
                                     self.home2.id, self.away2.id, slot2.id)
        off = self.svc.create_official("Busy Ref")
        self.svc.assign_official(self.game.id, off.id, OfficialRole.REFEREE)
        with self.assertRaises(ScheduleConflictError):
            self.svc.assign_official(game2.id, off.id, OfficialRole.REFEREE)

    def test_declined_assignment_frees_the_slot(self):
        # After declining, the official is free for an overlapping game.
        slot2 = self.svc.create_ice_slot(self.rink2.id, dt(19), dt(21))
        game2 = self.svc.create_game(self.season.id, self.div.id,
                                     self.home2.id, self.away2.id, slot2.id)
        off = self.svc.create_official("Free Ref")
        a = self.svc.assign_official(self.game.id, off.id, OfficialRole.REFEREE)
        self.svc.respond_assignment(a.id, accept=False)
        # No longer active → the overlapping assignment is allowed.
        a2 = self.svc.assign_official(game2.id, off.id, OfficialRole.REFEREE)
        self.assertEqual(a2.status.value, "proposed")

    def test_unassign_removes_and_frees_official(self):
        off = self.svc.create_official("Temp Ref")
        a = self.svc.assign_official(self.game.id, off.id, OfficialRole.REFEREE)
        self.assertEqual(len(self.store.assignments_for_game(self.game.id)), 1)
        self.svc.unassign_official(a.id)
        self.assertEqual(len(self.store.assignments_for_game(self.game.id)), 0)
        # The official can be reassigned after being unassigned.
        a2 = self.svc.assign_official(self.game.id, off.id, OfficialRole.LINESPERSON)
        self.assertEqual(a2.status.value, "proposed")

    def test_cancelled_game_does_not_block_assignment(self):
        # An official on a now-cancelled overlapping game is still assignable.
        slot2 = self.svc.create_ice_slot(self.rink2.id, dt(19), dt(21))
        game2 = self.svc.create_game(self.season.id, self.div.id,
                                     self.home2.id, self.away2.id, slot2.id)
        off = self.svc.create_official("Ref X")
        self.svc.assign_official(self.game.id, off.id, OfficialRole.REFEREE)
        # Cancel the first game; the official is now free for the overlap.
        self.game.cancelled = True
        self.store.save_game(self.game)
        a2 = self.svc.assign_official(game2.id, off.id, OfficialRole.REFEREE)
        self.assertEqual(a2.status.value, "proposed")

    def test_club_conflict_rejected(self):
        # An official from club_a can't officiate a game where Aces (club_a) play.
        off = self.svc.create_official("Biased Ref", home_club_id=self.club_a.id)
        with self.assertRaises(NotEligibleError):
            self.svc.assign_official(self.game.id, off.id, OfficialRole.REFEREE)

    def test_neutral_official_allowed(self):
        club_c = self.svc.create_club("Neutral HC")
        off = self.svc.create_official("Neutral Ref", home_club_id=club_c.id)
        a = self.svc.assign_official(self.game.id, off.id, OfficialRole.REFEREE)
        self.assertEqual(a.status.value, "proposed")


class OfficialsFacadeTest(unittest.TestCase):
    def setUp(self):
        self.store, self.game_id, self.ids = build_full_demo_store()
        self.api = ApiService(self.store)

    def test_seeded_official_shows_on_lineups(self):
        officials = self.api.get_lineups(self.game_id)["officials"]
        self.assertTrue(any(o["role"] == "referee" for o in officials))
        self.assertTrue(any(o["official_name"] == "Riley Whistle" for o in officials))

    def test_assign_and_get_officials_for_game(self):
        pool = self.api.get_officials()
        scorekeeper = next(o for o in pool if o["name"] == "Sky Tally")
        res = self.api.assign_official(self.game_id, scorekeeper["id"], "scorekeeper")
        self.assertNotIn("error", res)
        rows = self.api.get_officials_for_game(self.game_id)
        self.assertTrue(any(r["role"] == "scorekeeper" for r in rows))

    def test_respond_via_facade(self):
        a_id = self.ids["ref_assignment_id"]
        res = self.api.respond_assignment(a_id, accept=True)
        self.assertEqual(res["status"], "accepted")

    def test_assign_unknown_official_errors(self):
        res = self.api.assign_official(self.game_id, "official_missing", "referee")
        self.assertEqual(res["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
