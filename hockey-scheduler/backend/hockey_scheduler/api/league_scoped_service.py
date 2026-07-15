"""League-scoped API facade extension (#173 PR C).

Draft generation is scoped in ``services.league_scoped_scheduler``. This facade
persists the resolved season context and revalidates draft commit/publish so a
stale or legacy row cannot bypass the same league-ice invariant.
"""

from datetime import datetime

from ..domain import Game
from ..domain.errors import DomainError
from ..services.league_scope import (
    require_game_league_id,
    require_slot_belongs_to_season,
)
from .service import ApiService as _BaseApiService
from .service import catch


class ApiService(_BaseApiService):
    """API facade with league-isolated draft persistence and publishing."""

    def _draft_game_dto(self, game) -> dict:
        row = super()._draft_game_dto(game)
        row["season_id"] = game.season_id
        try:
            row["league_id"] = require_game_league_id(self.store, game)
        except DomainError:
            row["league_id"] = None
        return row

    def _draft_review_row(self, game, slot_games: dict,
                          double_booked: bool) -> dict:
        row = super()._draft_review_row(game, slot_games, double_booked)
        try:
            require_game_league_id(self.store, game)
            require_slot_belongs_to_season(
                self.store, game.ice_slot_id, game.season_id)
        except DomainError as exc:
            reason = getattr(exc, "details", {}).get("reason")
            issue = (
                "wrong_league_ice"
                if reason == "venue_access_missing"
                else "league_scope_missing"
            )
            if issue not in row["issues"]:
                row["issues"].append(issue)
        return row

    @catch
    def commit_draft_schedule(self, division_id: str, slot_ids=None,
                              constraints=None, actor_id=None) -> dict:
        proposal = self.draft_season_schedule(
            division_id, slot_ids=slot_ids, constraints=constraints)
        if isinstance(proposal, dict) and proposal.get("error"):
            return proposal

        # Revalidate every proposed slot before the first write. This closes the
        # commit boundary even if inventory changed after an earlier preview.
        season_id = proposal["season_id"]
        for row in proposal["draft_games"]:
            require_slot_belongs_to_season(
                self.store, row["ice_slot_id"], season_id)

        created = []
        with self.store.transaction():
            for row in proposal["draft_games"]:
                game = Game(
                    id=self.store.next_id("game"),
                    home_team_id=row["home_team_id"],
                    away_team_id=row["away_team_id"],
                    start_time=datetime.fromisoformat(row["start_time"]),
                    end_time=(datetime.fromisoformat(row["end_time"])
                              if row.get("end_time") else None),
                    rink=row.get("rink_name"),
                    season_id=proposal["season_id"],
                    division_id=division_id,
                    ice_slot_id=row.get("ice_slot_id"),
                    published=False,
                    is_draft=True,
                )
                self.store.add_game(game)
                created.append(self._draft_game_dto(game))
            self.setup._audit(
                "draft_schedule_committed", "division", division_id, actor_id,
                {
                    "created_count": len(created),
                    "game_ids": [row["game_id"] for row in created],
                    "unscheduled_count": len(proposal["unscheduled"]),
                    "season_id": proposal["season_id"],
                    "league_id": proposal["league_id"],
                },
            )
        return {
            "division_id": division_id,
            "season_id": proposal["season_id"],
            "league_id": proposal["league_id"],
            "created": created,
            "unscheduled": proposal["unscheduled"],
        }

    @catch
    def publish_draft_games(self, game_ids=None, all_drafts=False,
                            actor_id=None) -> dict:
        targets = self._draft_targets(game_ids, all_drafts)
        # Validate the whole batch before the first slot allocation or publish;
        # one bad legacy draft must not partially publish the selection.
        for game in targets:
            require_game_league_id(self.store, game)
            require_slot_belongs_to_season(
                self.store, game.ice_slot_id, game.season_id)
        return super().publish_draft_games(
            game_ids=game_ids, all_drafts=all_drafts, actor_id=actor_id)
