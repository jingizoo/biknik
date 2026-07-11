import unittest
from datetime import datetime, timedelta, timezone

from helpers import FakeClock

from hockey_scheduler.domain import IceSlotStatus, IceSlotType, Position
from hockey_scheduler.domain.errors import (
    DivisionMismatchError,
    NotFoundError,
    ScheduleConflictError,
    ValidationError,
)
from hockey_scheduler.services import RosterService, SetupService
from hockey_scheduler.store import InMemoryStore

UTC = timezone.utc
START = datetime(2026, 9, 1, 18, 30, tzinfo=UTC)
END = datetime(2026, 9, 1, 20, 0, tzinfo=UTC)


def dt(h, m=0):
    return datetime(2026, 9, 1, h, m, tzinfo=UTC)


def build():
    store = InMemoryStore()
    return SetupService(store, clock=FakeClock()), store


def full_universe(svc):
    """Create league→season→division, two teams in it, and one ice slot."""
    league = svc.create_league("EU Premier", country="DE")
    season = svc.create_season(league.id, "2026/27")
    division = svc.create_division(season.id, "U16", age_group="U16")
    club_a = svc.create_club("Lions Club")
    club_b = svc.create_club("Falcons Club")
    home = svc.create_team(club_a.id, division.id, "U16 Lions")
    away = svc.create_team(club_b.id, division.id, "U16 Falcons")
    # Participation now lives in a season registration (#180); a team must be
    # registered in the season+division to be scheduled there.
    svc.register_team_for_season(season.id, home.id, division.id)
    svc.register_team_for_season(season.id, away.id, division.id)
    venue = svc.create_venue("Ice Palace", league_id=league.id)
    rink = svc.create_rink(venue.id, "Rink 2")
    slot = svc.create_ice_slot(rink.id, START, END)
    return {"season": season, "division": division, "home": home,
            "away": away, "rink": rink, "slot": slot, "venue": venue}


class LeagueArenaSetupTest(unittest.TestCase):
    def test_full_happy_path(self):
        svc, store = build()
        u = full_universe(svc)
        game = svc.create_game(u["season"].id, u["division"].id,
                               u["home"].id, u["away"].id, u["slot"].id)
        self.assertEqual(game.home_team_id, u["home"].id)
        self.assertEqual(game.away_team_id, u["away"].id)
        self.assertEqual(game.start_time, START)
        self.assertEqual(game.rink, "Rink 2")
        self.assertEqual(game.ice_slot_id, u["slot"].id)
        # Slot is now allocated.
        self.assertEqual(store.get_ice_slot(u["slot"].id).status,
                         IceSlotStatus.ALLOCATED)

    def test_duplicate_slot_conflict(self):
        svc, _ = build()
        u = full_universe(svc)
        svc.create_game(u["season"].id, u["division"].id,
                        u["home"].id, u["away"].id, u["slot"].id)
        with self.assertRaises(ScheduleConflictError):
            svc.create_game(u["season"].id, u["division"].id,
                            u["home"].id, u["away"].id, u["slot"].id)

    def test_wrong_division_rejected_and_override_allows(self):
        svc, _ = build()
        u = full_universe(svc)
        # A team in a different division.
        other_div = svc.create_division(u["season"].id, "Senior")
        club_c = svc.create_club("Bears Club")
        outsider = svc.create_team(club_c.id, other_div.id, "Senior Bears")
        # Registered in the season (just a different division) — so the override
        # is a valid cross-division exhibition, not an unregistered team (#200).
        svc.register_team_for_season(u["season"].id, outsider.id, other_div.id)

        with self.assertRaises(DivisionMismatchError):
            svc.create_game(u["season"].id, u["division"].id,
                            u["home"].id, outsider.id, u["slot"].id)

        # Override flag forces the cross-division game.
        game = svc.create_game(u["season"].id, u["division"].id,
                               u["home"].id, outsider.id, u["slot"].id,
                               allow_division_override=True)
        self.assertEqual(game.away_team_id, outsider.id)

    def test_missing_entities_raise_not_found(self):
        svc, _ = build()
        with self.assertRaises(NotFoundError):
            svc.create_season("league_missing", "X")
        league = svc.create_league("L")
        with self.assertRaises(NotFoundError):
            svc.create_division("season_missing", "D")
        season = svc.create_season(league.id, "S")
        with self.assertRaises(NotFoundError):
            svc.create_team("club_missing", "division_missing", "T")
        with self.assertRaises(NotFoundError):
            svc.create_rink("venue_missing", "R")

    def test_division_must_belong_to_season(self):
        svc, _ = build()
        u = full_universe(svc)
        other_league = svc.create_league("Other")
        other_season = svc.create_season(other_league.id, "S2")
        with self.assertRaises(ValidationError):
            svc.create_game(other_season.id, u["division"].id,
                            u["home"].id, u["away"].id, u["slot"].id)

    def test_team_cannot_play_itself(self):
        svc, _ = build()
        u = full_universe(svc)
        with self.assertRaises(ValidationError):
            svc.create_game(u["season"].id, u["division"].id,
                            u["home"].id, u["home"].id, u["slot"].id)

    def test_ice_slot_requires_tz_aware_times(self):
        svc, _ = build()
        league = svc.create_league("L")
        venue = svc.create_venue("V")
        rink = svc.create_rink(venue.id, "R")
        naive = datetime(2026, 9, 1, 18, 30)  # no tzinfo
        with self.assertRaises(ValidationError):
            svc.create_ice_slot(rink.id, naive, naive + timedelta(hours=1))

    def test_ice_slot_end_after_start(self):
        svc, _ = build()
        venue = svc.create_venue("V")
        rink = svc.create_rink(venue.id, "R")
        with self.assertRaises(ValidationError):
            svc.create_ice_slot(rink.id, END, START)

    def test_created_game_works_with_roster_service(self):
        # The manual game must be usable by the existing roster/substitute flow.
        svc, store = build()
        u = full_universe(svc)
        game = svc.create_game(u["season"].id, u["division"].id,
                               u["home"].id, u["away"].id, u["slot"].id)
        roster = RosterService(store, clock=FakeClock())
        p1 = svc.add_player(u["home"].id, "Goalie One", Position.GOALIE)
        p2 = svc.add_player(u["home"].id, "Skater One", Position.FORWARD)
        roster.select_roster(game.id, [p1.id, p2.id], actor_id="coach_1")
        status = roster.compute_roster_status(game.id)
        self.assertEqual(status.game_id, game.id)
        self.assertEqual(status.target_skaters, 15)

    def test_every_create_is_audited(self):
        svc, store = build()
        full_universe(svc)
        actions = {a.action for a in store.setup_audit}
        for expected in ("league_created", "season_created", "division_created",
                         "club_created", "team_created", "venue_created",
                         "rink_created", "ice_slot_created"):
            self.assertIn(expected, actions)

    def test_blank_name_rejected(self):
        svc, _ = build()
        with self.assertRaises(ValidationError):
            svc.create_league("   ")

    # -- scheduling-rule fixes (review round 3) ---------------------------
    def test_game_in_second_season_division_succeeds(self):
        # The wizard/back end must accept a non-seeded season's division.
        svc, _ = build()
        lg = svc.create_league("L2")
        se = svc.create_season(lg.id, "S2")
        dv = svc.create_division(se.id, "U16 B")
        ta = svc.create_team(svc.create_club("CA").id, dv.id, "TA")
        tb = svc.create_team(svc.create_club("CB").id, dv.id, "TB")
        svc.register_team_for_season(se.id, ta.id, dv.id)
        svc.register_team_for_season(se.id, tb.id, dv.id)
        rink = svc.create_rink(
            svc.create_venue("VE", league_id=lg.id).id, "RI")
        slot = svc.create_ice_slot(rink.id, dt(18, 30), dt(20))
        game = svc.create_game(se.id, dv.id, ta.id, tb.id, slot.id)
        self.assertEqual(game.season_id, se.id)

    def test_overlapping_ice_slots_rejected(self):
        svc, _ = build()
        rink = svc.create_rink(svc.create_venue("V").id, "R")
        svc.create_ice_slot(rink.id, dt(18, 30), dt(20))
        with self.assertRaises(ScheduleConflictError):
            svc.create_ice_slot(rink.id, dt(19), dt(20, 30))

    def test_adjacent_ice_slots_allowed(self):
        svc, _ = build()
        rink = svc.create_rink(svc.create_venue("V").id, "R")
        svc.create_ice_slot(rink.id, dt(18, 30), dt(20))
        s = svc.create_ice_slot(rink.id, dt(20), dt(21, 30))  # starts at prev end
        self.assertIsNotNone(s.id)

    def test_same_time_different_rink_allowed(self):
        svc, _ = build()
        venue = svc.create_venue("V")
        r1 = svc.create_rink(venue.id, "R1")
        r2 = svc.create_rink(venue.id, "R2")
        svc.create_ice_slot(r1.id, dt(18, 30), dt(20))
        s = svc.create_ice_slot(r2.id, dt(18, 30), dt(20))
        self.assertIsNotNone(s.id)

    def test_non_game_slot_cannot_host_game(self):
        svc, _ = build()
        u = full_universe(svc)
        maint = svc.create_ice_slot(u["rink"].id, dt(20, 30), dt(22),
                                    slot_type=IceSlotType.MAINTENANCE)
        self.assertEqual(maint.status, IceSlotStatus.BLOCKED)
        with self.assertRaises(ValidationError):
            svc.create_game(u["season"].id, u["division"].id,
                            u["home"].id, u["away"].id, maint.id)

    def test_team_overlap_rejected(self):
        svc, _ = build()
        u = full_universe(svc)
        r2 = svc.create_rink(u["venue"].id, "R2")
        slot2 = svc.create_ice_slot(r2.id, dt(18, 30), dt(20))  # same time
        svc.create_game(u["season"].id, u["division"].id,
                        u["home"].id, u["away"].id, u["slot"].id)
        bears = svc.create_team(svc.create_club("Bears").id, u["division"].id, "Bears")
        svc.register_team_for_season(u["season"].id, bears.id, u["division"].id)
        with self.assertRaises(ScheduleConflictError):
            svc.create_game(u["season"].id, u["division"].id,
                            u["home"].id, bears.id, slot2.id)

    def test_team_overlap_adjacent_allowed(self):
        svc, _ = build()
        u = full_universe(svc)
        r2 = svc.create_rink(u["venue"].id, "R2")
        slot2 = svc.create_ice_slot(r2.id, dt(20), dt(21, 30))  # adjacent
        svc.create_game(u["season"].id, u["division"].id,
                        u["home"].id, u["away"].id, u["slot"].id)
        bears = svc.create_team(svc.create_club("Bears").id, u["division"].id, "Bears")
        svc.register_team_for_season(u["season"].id, bears.id, u["division"].id)
        game = svc.create_game(u["season"].id, u["division"].id,
                               u["home"].id, bears.id, slot2.id)
        self.assertIsNotNone(game.id)

    def test_different_teams_same_time_allowed(self):
        svc, _ = build()
        u = full_universe(svc)
        r2 = svc.create_rink(u["venue"].id, "R2")
        slot2 = svc.create_ice_slot(r2.id, dt(18, 30), dt(20))
        svc.create_game(u["season"].id, u["division"].id,
                        u["home"].id, u["away"].id, u["slot"].id)
        bears = svc.create_team(svc.create_club("Bears").id, u["division"].id, "Bears")
        eagles = svc.create_team(svc.create_club("Eagles").id, u["division"].id, "Eagles")
        svc.register_team_for_season(u["season"].id, bears.id, u["division"].id)
        svc.register_team_for_season(u["season"].id, eagles.id, u["division"].id)
        game = svc.create_game(u["season"].id, u["division"].id,
                               bears.id, eagles.id, slot2.id)
        self.assertIsNotNone(game.id)


if __name__ == "__main__":
    unittest.main()
