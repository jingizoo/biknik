"""League-scoped API facade extension (#173 PR C).

Draft generation is scoped in ``services.league_scoped_scheduler``. This facade
persists the resolved season context and revalidates draft commit/publish so a
stale or legacy row cannot bypass the same league-ice invariant.
"""

from datetime import datetime

from ..domain import Game, IceSlotStatus
from ..domain.errors import DomainError, ValidationError
from ..services.league_scope import (
    require_game_league_id,
    require_slot_belongs_to_season,
    require_slots_belong_to_locked_season,
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
    def commit_draft_schedule(self, division_id: str = None,
                              season_id: str = None, league_id: str = None,
                              slot_ids=None, constraints=None,
                              actor_id=None) -> dict:
        """Accepts the same Division-only or Season+League(+optional
        Division) scope as ``draft_season_schedule`` (#233 Slice G). Every
        created Game's ``season_id``/``league_id`` (the canonical grouping
        League, ``store.get_league``) are stamped from the regenerated
        proposal, and ``division_id`` is read per-row — a League-wide draft's
        rows can span several Divisions (or none). Previously ``league_id``
        was never set on a committed draft game at all, silently defeating
        the stranding guards in ``assign_season_team_league``/
        ``move_division_to_league`` (#233 Slice G review) for every
        scheduler-created game.
        """
        proposal = self.draft_season_schedule(
            division_id=division_id, season_id=season_id, league_id=league_id,
            slot_ids=slot_ids, constraints=constraints)
        if isinstance(proposal, dict) and proposal.get("error"):
            return proposal
        resolved_season_id = proposal["season_id"]

        # The Division-only proposal's own "league_id" is this tenancy
        # layer's frozen Program-scoped vocabulary (league_scope.py's
        # league_id_for_division -> season.program_id, see scope_bridge.py's
        # docstring) — NOT the canonical grouping League Game.league_id
        # actually stores. Resolve the CANONICAL league straight from the
        # Division for that path; the League-wide path already received the
        # canonical league_id as an explicit param, so its own proposal
        # value is correct as-is.
        if season_id and league_id:
            canonical_league_id = proposal["league_id"]
        else:
            division = self.store.get_division(division_id) if division_id else None
            # #283: Division.league_id dropped; resolve its owning League via
            # the LeagueSeason it hangs off.
            division_ls = (self.store.get_league_season(division.league_season_id)
                           if division and division.league_season_id else None)
            canonical_league_id = division_ls.league_id if division_ls else None

        created = []
        # #159 — lock the target Season read-only and do EVERY Game/audit write
        # in one transaction, so a concurrent archive cannot slip between the
        # guard and the writes (autocommit would drop the FOR UPDATE lock at the
        # end of the check) and the batch stays all-or-nothing.
        with self.store.transaction():
            # #277/#313 — lock every team the batch places, then every rink it
            # places onto (Team→Rink→Season order, before the Season guard) so each
            # per-row check + ALLOCATE is atomic against a concurrent placement
            # sharing a team AND against the ice-availability builder on those rinks
            # (Rink before Season matches the builder and avoids deadlocking it).
            # One globally-sorted pre-pass fixes the lock order for the whole batch;
            # see SetupService._lock_teams / _lock_rinks for the ordering contract.
            self.setup._lock_teams(
                t for row in proposal["draft_games"]
                for t in (row["home_team_id"], row["away_team_id"]))
            _batch_rinks = set()
            for row in proposal["draft_games"]:
                _s = self.store.get_ice_slot(row["ice_slot_id"])
                if _s is not None:
                    _batch_rinks.add(_s.rink_id)
            self.setup._lock_rinks(_batch_rinks)
            self._guard_active_seasons([resolved_season_id])
            # Resolve the exact LeagueSeason after the Season lock. A regular
            # draft must carry this identity so publish/move can enforce the
            # same competition scope, including league-wide rows with no
            # Division.
            draft_ls = self.store.league_season_for(
                canonical_league_id, resolved_season_id
            ) if canonical_league_id else None
            if draft_ls is None:
                raise ValidationError(
                    "The draft's League is not linked to this Season.",
                    {"reason": "draft_league_season_missing",
                     "league_id": canonical_league_id,
                     "season_id": resolved_season_id})
            draft_ls_id = draft_ls.id
            # #314 review — re-validate every proposed slot's Season eligibility
            # HERE, under the Rink+Season locks just acquired, not before this
            # transaction opened (as a stale prevalidation could leave a window
            # for a concurrent SeasonVenueAccess revoke or Rink→Venue
            # reassignment to commit and then be missed). Before ANY Game,
            # slot-status, or audit write for the whole batch — a bad row rolls
            # back everything, matching this commit's existing all-or-nothing
            # contract; shared with move_game via the same locked helper.
            require_slots_belong_to_locked_season(
                self.store, [row["ice_slot_id"] for row in proposal["draft_games"]],
                resolved_season_id)
            # #314 review — also re-validate every proposed row's competition
            # participation HERE, under the same locks: a concurrent
            # unregister_team_from_season or a team-to-league transfer can
            # commit in the SAME gap a stale pre-lock proposal would miss, after
            # which the write would persist a Game for a team no longer a valid
            # participant. Reuses the identical check create_game enforces.
            self.setup._require_batch_team_participation(
                resolved_season_id, draft_ls_id, proposal["draft_games"])
            for row in proposal["draft_games"]:
                # #277: run the SAME final conflict check as create_game /
                # move_game before persisting — slot free (exists, GAME,
                # AVAILABLE, not already held) AND neither team on an overlapping
                # fixture — so a regenerated proposal that would double-book a
                # slot OR a team fails atomically (the whole batch rolls back)
                # instead of silently persisting a bad fixture. Per #277's
                # acceptance, schedule commits enforce the identical check as
                # manual moves; there is no draft-only exception. Returns the
                # resolved slot to allocate below. (The base facade's own
                # commit_draft_schedule uses this same check; this override
                # reimplements the commit body for league-scope validation, so it
                # must enforce the identical invariant.)
                slot = self.setup._assert_slot_free_for_game(
                    row["ice_slot_id"], row["home_team_id"], row["away_team_id"],
                    season_id=resolved_season_id)
                game = Game(
                    id=self.store.next_id("game"),
                    home_team_id=row["home_team_id"],
                    away_team_id=row["away_team_id"],
                    start_time=datetime.fromisoformat(row["start_time"]),
                    end_time=(datetime.fromisoformat(row["end_time"])
                              if row.get("end_time") else None),
                    rink=row.get("rink_name"),
                    season_id=resolved_season_id,
                    league_id=canonical_league_id,
                    division_id=row.get("division_id"),
                    ice_slot_id=row.get("ice_slot_id"),
                    league_season_id=draft_ls_id,
                    published=False,
                    is_draft=True,
                )
                self.store.add_game(game)
                # A committed draft occupies its ice: flip the backing slot to
                # ALLOCATED (exactly as create_game / move_game do) so later
                # scheduling and the shared checker read it as taken instead of
                # offering the same occupied ice again. The check above already
                # rejected any slot a game holds, so this only flips AVAILABLE.
                slot.status = IceSlotStatus.ALLOCATED
                self.store.save_ice_slot(slot)
                created.append(self._draft_game_dto(game))
            if season_id and league_id:
                scope_type, scope_id = "league", league_id
            else:
                scope_type, scope_id = "division", division_id
            self.setup._audit(
                "draft_schedule_committed", scope_type, scope_id, actor_id,
                {
                    "created_count": len(created),
                    "game_ids": [row["game_id"] for row in created],
                    "unscheduled_count": len(proposal["unscheduled"]),
                    "season_id": resolved_season_id,
                    "league_id": proposal["league_id"],
                },
            )
        return {
            "division_id": division_id,
            "season_id": resolved_season_id,
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
