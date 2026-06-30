"""Shared test helpers: a deterministic clock and a small game builder."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make ``hockey_scheduler`` importable when tests run from any directory.
BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from hockey_scheduler.domain import Game, Player, Position, Team  # noqa: E402
from hockey_scheduler.services import RosterService  # noqa: E402
from hockey_scheduler.store import InMemoryStore  # noqa: E402


class FakeClock:
    """Monotonic, deterministic clock for reproducible timestamps."""

    def __init__(self) -> None:
        self._t = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        self._t += timedelta(seconds=1)
        return self._t


def make_service(target_goalies: int = 1, target_skaters: int = 4):
    """Build a service with one team, a goalie + skaters, and one game.

    Returns (service, store, game_id). Players created:
      - player_goalie_1, player_goalie_2 (goalies)
      - player_skater_1 .. player_skater_8 (skaters)
    """
    store = InMemoryStore()
    team = Team(id="team_1", name="Test Team", division="U16")
    store.add_team(team)

    store.add_player(Player(id="player_goalie_1", team_id=team.id,
                            name="Goalie One", position=Position.GOALIE))
    store.add_player(Player(id="player_goalie_2", team_id=team.id,
                            name="Goalie Two", position=Position.GOALIE))
    for i in range(1, 9):
        pos = Position.DEFENSE if i % 2 == 0 else Position.FORWARD
        store.add_player(Player(id=f"player_skater_{i}", team_id=team.id,
                                name=f"Skater {i}", position=pos))

    game = Game(
        id="game_1",
        home_team_id=team.id,
        start_time=datetime(2026, 2, 1, 18, 30, tzinfo=timezone.utc),
        target_goalies=target_goalies,
        target_skaters=target_skaters,
        max_skaters=target_skaters + 3,
    )
    store.add_game(game)

    service = RosterService(store, clock=FakeClock())
    return service, store, game.id


def select_and_confirm(service, game_id, player_ids, coach="coach_1"):
    """Select the given players and mark them all confirmed."""
    service.select_roster(game_id, player_ids, actor_id=coach)
    from hockey_scheduler.domain import AvailabilityStatus

    for pid in player_ids:
        service.set_availability(game_id, pid, AvailabilityStatus.AVAILABLE)
