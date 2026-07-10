"""League-scoped SetupService extension (#173 PR C).

The base setup service remains responsible for game/slot state transitions. This
extension adds the one cross-cutting precondition: game ice must belong to the
same league as the game's season/division.
"""

from typing import Optional

from .league_scope import (
    require_game_league_id,
    require_slot_belongs_to_league,
)
from .setup_service import SetupService as _BaseSetupService


class SetupService(_BaseSetupService):
    """Setup service with league-isolated game-ice enforcement."""

    def create_game(self, season_id: str, division_id: str, home_team_id: str,
                    away_team_id: str, ice_slot_id: str,
                    target_goalies: int = 1, target_skaters: int = 15,
                    max_skaters: int = 18, allow_division_override: bool = False,
                    actor_id: Optional[str] = None):
        season = self.store.get_season(season_id) if season_id else None
        if season is not None:
            require_slot_belongs_to_league(
                self.store, ice_slot_id, season.league_id)
        return super().create_game(
            season_id, division_id, home_team_id, away_team_id, ice_slot_id,
            target_goalies=target_goalies, target_skaters=target_skaters,
            max_skaters=max_skaters,
            allow_division_override=allow_division_override,
            actor_id=actor_id,
        )

    def move_game(self, game_id: str, new_ice_slot_id: str, reason: str = "",
                  actor_id: Optional[str] = None):
        game = self.store.get_game(game_id)
        # Preserve the base service's established same-slot error/reason before
        # running the new scope check.
        if game is not None and new_ice_slot_id != game.ice_slot_id:
            league_id = require_game_league_id(self.store, game)
            require_slot_belongs_to_league(
                self.store, new_ice_slot_id, league_id)
        return super().move_game(
            game_id, new_ice_slot_id, reason=reason, actor_id=actor_id)

    def publish_game(self, game_id: str, published: bool = True,
                     actor_id: Optional[str] = None):
        game = self.store.get_game(game_id)
        if published and game is not None:
            league_id = require_game_league_id(self.store, game)
            require_slot_belongs_to_league(
                self.store, game.ice_slot_id, league_id)
        return super().publish_game(game_id, published, actor_id)
