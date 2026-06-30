"""Full end-to-end demo dataset.

Builds the Alpine Ice Hockey League scenario using the **real** setup service
(not orphan game data): league → season → divisions, clubs → teams, venue →
rinks → ice slots, and a manual game on an allocated slot. Then seeds the home
team's players and confirms a full roster, leaving one player available to act
as a substitute so the live roster/substitute flow can be demonstrated.

No real PII — all names are obviously fictional.
"""

from datetime import datetime, timezone
from typing import Tuple

from .domain import AvailabilityStatus, Position
from .services import RosterService, SetupService
from .store import InMemoryStore

# A fixed demo "Saturday".
_DAY = datetime(2026, 9, 5, tzinfo=timezone.utc)

_SKATERS = [
    "Aarav M.", "Kabir S.", "Rohan P.", "Dev K.", "Neil R.", "Sam T.",
    "Leo V.", "Max W.", "Ivan O.", "Theo L.", "Finn B.", "Zane H.",
    "Owen C.", "Cole D.", "Jude E.", "Reid F.",
]


def build_full_demo_store() -> Tuple[InMemoryStore, str, dict]:
    """Return (store, game_id, ids) for the full E2E demo scenario."""
    store = InMemoryStore()
    setup = SetupService(store)
    roster = RosterService(store)
    admin = "league_admin"

    league = setup.create_league("Alpine Ice Hockey League", country="AT",
                                 actor_id=admin)
    season = setup.create_season(league.id, "2026–27 Winter Season",
                                 actor_id=admin)
    d_u16 = setup.create_division(season.id, "U16 Elite", age_group="U16",
                                  actor_id=admin)
    d_u18 = setup.create_division(season.id, "U18 Development", age_group="U18",
                                  actor_id=admin)
    d_sen = setup.create_division(season.id, "Senior A", actor_id=admin)

    club_lions = setup.create_club("Lions HC", country="AT", actor_id=admin)
    club_falcons = setup.create_club("Falcons HC", country="AT", actor_id=admin)
    setup.create_club("Bears HC", country="AT", actor_id=admin)

    u16_lions = setup.create_team(club_lions.id, d_u16.id, "U16 Lions",
                                  actor_id=admin)
    u16_falcons = setup.create_team(club_falcons.id, d_u16.id, "U16 Falcons",
                                    actor_id=admin)
    setup.create_team(club_lions.id, d_u18.id, "U18 Lions", actor_id=admin)
    setup.create_team(club_lions.id, d_sen.id, "Senior Lions", actor_id=admin)

    venue = setup.create_venue("Nord Arena", address="Alpine Way 1",
                               actor_id=admin)
    main_rink = setup.create_rink(venue.id, "Main Rink", actor_id=admin)
    setup.create_rink(venue.id, "Training Rink", actor_id=admin)

    setup.create_ice_slot(main_rink.id, _DAY.replace(hour=16),
                          _DAY.replace(hour=17, minute=30), actor_id=admin)
    slot_game = setup.create_ice_slot(main_rink.id, _DAY.replace(hour=18, minute=30),
                                      _DAY.replace(hour=20), actor_id=admin)
    setup.create_ice_slot(main_rink.id, _DAY.replace(hour=20, minute=30),
                          _DAY.replace(hour=22), actor_id=admin)

    game = setup.create_game(season.id, d_u16.id, u16_lions.id, u16_falcons.id,
                             slot_game.id, actor_id=admin)

    # Seed BOTH U16 teams with a full squad so any U16 game (either team as
    # home) has a rosterable roster. The Falcons squad uses distinct names.
    def seed_squad(team_id, prefix):
        goalie = setup.add_player(team_id, f"{prefix} Goalie", Position.GOALIE,
                                  jersey_number=30, actor_id=admin)
        out = []
        for i, name in enumerate(_SKATERS, start=1):
            pos = Position.DEFENSE if i % 3 == 0 else Position.FORWARD
            out.append(setup.add_player(team_id, f"{name}", pos,
                                        jersey_number=i, actor_id=admin))
        return goalie, out

    goalie, skaters = seed_squad(u16_lions.id, "Lions")
    seed_squad(u16_falcons.id, "Falcons")

    selected = [goalie.id] + [s.id for s in skaters[:15]]
    roster.select_roster(game.id, selected, actor_id="coach_lions")
    for pid in selected:
        roster.set_availability(game.id, pid, AvailabilityStatus.AVAILABLE)

    ids = {
        "league_id": league.id,
        "season_id": season.id,
        "division_id": d_u16.id,
        "game_id": game.id,
        "home_team_id": u16_lions.id,
        "away_team_id": u16_falcons.id,
        "main_rink_id": main_rink.id,
        "venue_id": venue.id,
        "substitute_player_id": skaters[15].id,   # unselected → can sub
        "selected_player_id": skaters[0].id,
    }
    return store, game.id, ids
