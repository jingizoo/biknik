"""Mock data builder for the first slice.

Uses obviously-fictional names — no real PII, no production data.
"""

from typing import Tuple

from .domain import Game, Player, Position, Team
from .full_demo import demo_day_zero
from .services import RosterService
from .store import InMemoryStore


def build_seeded_store(seed_instant=None) -> Tuple[InMemoryStore, str]:
    """Return an in-memory store seeded with one team, players, and a game.

    The returned game id targets 1 goalie and 15 skaters.

    The game is placed RELATIVE to ``seed_instant`` on the demo's shared day
    zero (:func:`~hockey_scheduler.full_demo.demo_day_zero`), never on a fixed
    calendar date (#387). It used to sit at 2026-07-04, which real time had
    already passed: ``hockey_scheduler.demo`` then walked an operator through
    enrolling and accepting a substitute for a game that had ALREADY BEEN
    PLAYED — a state ``accept_substitute_offer`` refuses outright ("This game
    is no longer upcoming"), so the script demonstrated a flow the product
    does not actually permit.

    Omitted, the instant comes from the RosterService clock this seed's own
    store is about to be driven by — the same single source of "now" the
    engine reads, not a second one (CLAUDE.md: times are passed in
    explicitly).
    """
    store = InMemoryStore()

    team = Team(id="team_lions", name="U16 Lions")
    store.add_team(team)

    if seed_instant is None:
        seed_instant = RosterService(store).clock()
    start = demo_day_zero(seed_instant).replace(hour=18, minute=30)
    game = Game(
        id="game_1",
        home_team_id=team.id,
        away_team_id="team_falcons",
        rink="Rink 2",
        start_time=start,
        target_goalies=1,
        target_skaters=15,
        max_skaters=18,
    )
    store.add_game(game)

    # 1 goalie + 16 skaters available (15 will be selected, extras can sub).
    store.add_player(
        Player(id="player_goalie_1", team_id=team.id, name="Goalie Gabe",
               position=Position.GOALIE, jersey_number=30)
    )
    store.add_player(
        Player(id="player_goalie_2", team_id=team.id, name="Backup Bruno",
               position=Position.GOALIE, jersey_number=31)
    )
    fictional = [
        "Aarav M.", "Kabir S.", "Rohan P.", "Dev K.", "Neil R.", "Sam T.",
        "Leo V.", "Max W.", "Ivan O.", "Theo L.", "Finn B.", "Zane H.",
        "Owen C.", "Cole D.", "Jude E.", "Reid F.",
    ]
    for i, name in enumerate(fictional, start=1):
        position = Position.DEFENSE if i % 3 == 0 else Position.FORWARD
        store.add_player(
            Player(id=f"player_skater_{i}", team_id=team.id, name=name,
                   position=position, jersey_number=i)
        )

    return store, game.id
