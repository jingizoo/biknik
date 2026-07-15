"""League-scoped SetupService extension (#173 PR C).

The base setup service remains responsible for game/slot state transitions. This
extension adds the one cross-cutting precondition: game ice must belong to the
same league as the game's season/division.
"""

from typing import Optional

from .league_scope import (
    require_game_league_id,
    require_slot_belongs_to_season,
)
from .setup_service import SetupService as _BaseSetupService


class SetupService(_BaseSetupService):
    """Setup service with league-isolated game-ice enforcement."""

    def create_game(self, season_id: str, division_id: str, home_team_id: str,
                    away_team_id: str, ice_slot_id: str,
                    target_goalies: int = 1, target_skaters: int = 15,
                    max_skaters: int = 18, allow_division_override: bool = False,
                    actor_id: Optional[str] = None,
                    league_id: Optional[str] = None):
        # The scope check and all writes share one transaction. Call the base
        # method's undecorated body to avoid opening a nested SqlStore
        # transaction (SQLite rejects BEGIN inside BEGIN).
        with self.store.transaction():
            # Preserve the base service's established validation precedence.
            # Scope validation runs only after season/division/team structure is
            # valid; otherwise the base body reports its original error.
            season = self.store.get_season(season_id) if season_id else None
            division = self.store.get_division(division_id) if division_id else None
            home = self.store.get_team(home_team_id) if home_team_id else None
            away = self.store.get_team(away_team_id) if away_team_id else None
            # Participation is resolved through SeasonTeamRegistration (#180),
            # not Team.division_id (#200 review): a league-first team created
            # with division_id=None but correctly registered must still count as
            # a valid matchup so the cross-league ice guard runs. When v2 omits a
            # division (#233 Slice C2), participation relaxes to season-only.
            require_division = division_id is not None
            teams_match = (
                season is not None
                and self._team_participates(season, home_team_id, division_id,
                                            require_division)
                and self._team_participates(season, away_team_id, division_id,
                                            require_division)
            )
            # Division is optional in v2 (a supplied league_id scopes the game);
            # when a division IS given it must belong to the season, as in v1.
            division_ok = (division is not None and division.season_id == season_id) \
                if require_division else True
            structure_valid = (
                season is not None
                and division_ok
                and home is not None
                and away is not None
                and home_team_id != away_team_id
                and teams_match
            )
            if structure_valid:
                require_slot_belongs_to_season(
                    self.store, ice_slot_id, season_id)
            return _BaseSetupService.create_game.__wrapped__(
                self, season_id, division_id, home_team_id, away_team_id,
                ice_slot_id, target_goalies=target_goalies,
                target_skaters=target_skaters, max_skaters=max_skaters,
                allow_division_override=allow_division_override,
                actor_id=actor_id, league_id=league_id,
            )

    def move_game(self, game_id: str, new_ice_slot_id: str, reason: str = "",
                  actor_id: Optional[str] = None):
        with self.store.transaction():
            game = self.store.get_game(game_id)
            # Preserve the base service's established same-slot error/reason
            # before running the new scope check.
            if game is not None and new_ice_slot_id != game.ice_slot_id:
                require_game_league_id(self.store, game)
                require_slot_belongs_to_season(
                    self.store, new_ice_slot_id, game.season_id)
            return _BaseSetupService.move_game.__wrapped__(
                self, game_id, new_ice_slot_id, reason=reason,
                actor_id=actor_id)

    def publish_game(self, game_id: str, published: bool = True,
                     actor_id: Optional[str] = None):
        with self.store.transaction():
            game = self.store.get_game(game_id)
            if published and game is not None:
                require_game_league_id(self.store, game)
                require_slot_belongs_to_season(
                    self.store, game.ice_slot_id, game.season_id)
            return _BaseSetupService.publish_game.__wrapped__(
                self, game_id, published, actor_id)
