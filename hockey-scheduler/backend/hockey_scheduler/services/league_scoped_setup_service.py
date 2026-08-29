"""League-scoped SetupService extension (#173 PR C).

The base setup service remains responsible for game/slot state transitions. This
extension adds the one cross-cutting precondition: game ice must belong to the
same league as the game's season/division.
"""

from typing import Optional

from .league_scope import (
    require_game_league_id,
    require_slot_belongs_to_season,
    require_slots_belong_to_locked_season,
)
from .setup_service import SetupService as _BaseSetupService


class SetupService(_BaseSetupService):
    """Setup service with league-isolated game-ice enforcement."""

    def create_game(self, season_id: str, division_id: str, home_team_id: str,
                    away_team_id: str, ice_slot_id: str,
                    target_goalies: int = 1, target_skaters: int = 15,
                    max_skaters: int = 18, allow_division_override: bool = False,
                    actor_id: Optional[str] = None,
                    league_id: Optional[str] = None,
                    game_type: str = "regular"):
        # The scope check and all writes share one transaction. Call the base
        # method's undecorated body to avoid opening a nested SqlStore
        # transaction (SQLite rejects BEGIN inside BEGIN).
        with self.store.transaction():
            # #313/#318 — take the FULL placement lock set in the canonical
            # global order (Program -> Team -> Rink -> Season, matching the
            # ice-availability builder's Program -> Rink -> Season) BEFORE the
            # archived-Season guard. This override is the true entry point, so
            # the order is pinned HERE: the scope plan (candidate + same-rink
            # neighbor Seasons and their Programs — every policy scope the
            # placement gate reads) is computed by a plain pre-lock locator
            # and handed down to the base body, which then only re-locks
            # already-held rows (no-ops) and re-verifies the plan under the
            # locks — it never takes a NEW Program/Season lock after the Rink,
            # which is exactly the inversion that deadlocked the builder.
            _slot = self.store.get_ice_slot(ice_slot_id)  # scope locator only
            _plan = self._policy_scope_lock_plan(
                (_slot.rink_id,) if _slot is not None else (), (season_id,))
            self._lock_programs(_plan["programs"])
            self._lock_teams((home_team_id, away_team_id))
            _slot = self.store.get_ice_slot(ice_slot_id)
            if _slot is not None:
                self._lock_rinks((_slot.rink_id,))
            # Record exactly which Rink rows this entry point locked: the
            # base body must never take a FRESH Rink lock after the Season
            # locks below (Rink-before-Season would invert against the
            # builder), so it refuses placement_raced on any rink outside
            # this set instead of locking late.
            _plan["locked_rinks"] = (
                frozenset((_slot.rink_id,)) if _slot is not None
                else frozenset())
            self._lock_seasons(_plan["seasons"])
            # #159 — an archived (read-only) Season blocks game creation before
            # any slot/scope resolution, matching the base body's own guard.
            self._require_active_season(season_id)
            # Preserve the base service's established validation precedence.
            # Scope validation runs only after season/division/team structure is
            # valid; otherwise the base body reports its original error.
            season = self.store.get_season(season_id) if season_id else None
            # #283 Slice D: an EXHIBITION game carries no Division (the base body
            # ignores any supplied division and never derives a league from it),
            # so the ice-eligibility precheck runs season-only — matching the
            # base method's own relaxed exhibition path.
            is_exhibition = (game_type or "regular") == "exhibition"
            division = (self.store.get_division(division_id)
                        if division_id and not is_exhibition else None)
            home = self.store.get_team(home_team_id) if home_team_id else None
            away = self.store.get_team(away_team_id) if away_team_id else None
            # Participation is resolved through SeasonTeamRegistration (#180),
            # not Team.division_id (#200 review): a league-first team created
            # with division_id=None but correctly registered must still count as
            # a valid matchup so the cross-league ice guard runs. When v2 omits a
            # division (#233 Slice C2) — or the game is an exhibition (#283
            # Slice D) — participation relaxes to season-only.
            require_division = division_id is not None and not is_exhibition
            teams_match = (
                season is not None
                and self._team_participates(season, home_team_id, division_id,
                                            require_division)
                and self._team_participates(season, away_team_id, division_id,
                                            require_division)
            )
            # Division is optional in v2 (a supplied league_id scopes the game);
            # when a division IS given it must belong to the season, as in v1.
            # #283: Division.season_id dropped; resolve its Season via LeagueSeason.
            division_ls = (self.store.get_league_season(division.league_season_id)
                           if division is not None else None)
            division_ok = (division is not None and division_ls is not None
                           and division_ls.season_id == season_id) \
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
                actor_id=actor_id, league_id=league_id, game_type=game_type,
                _scope_plan=_plan,
            )

    def move_game(self, game_id: str, new_ice_slot_id: str, reason: str = "",
                  actor_id: Optional[str] = None):
        def _scope_check(game, new_slot):
            # #314 review — called from INSIDE _move_game_locked, after the
            # Team/Rink/Season locks are held and the same-slot no-op check
            # has already passed (a no-op move never needs re-scoping): this
            # is the game's fresh, post-lock state and the resolved target
            # slot, so a concurrent SeasonVenueAccess revoke or Rink→Venue
            # reassignment can never land in the gap between this check and
            # the write below. Previously this ran before ANY lock was taken.
            # #283 Slice D/E: an exhibition has no owning League, so only a
            # regular game needs the league-ice-isolation check; both kinds
            # still need their new slot's venue to serve the game's Season.
            if game.game_type != "exhibition":
                require_game_league_id(self.store, game)
            require_slots_belong_to_locked_season(
                self.store, [new_slot.id], game.season_id)
        def _attempt():
            # The base's raw locked body (not its public move_game, which would
            # install its OWN retry loop/transaction) so the scope hook and
            # the base's Team/Rink/Season locking run as ONE attempt sharing
            # ONE transaction (#314 review) — _retry_on_move_race below owns
            # the transaction boundary and the retry-on-race loop for both.
            return _BaseSetupService._move_game_locked(
                self, game_id, new_ice_slot_id, reason=reason,
                actor_id=actor_id, scope_check=_scope_check)
        return self._retry_on_move_race(_attempt, game_id=game_id)

    def publish_game(self, game_id: str, published: bool = True,
                     actor_id: Optional[str] = None):
        with self.store.transaction():
            game = self.store.get_game(game_id)
            if published and game is not None:
                # PR #427: the Season AUTHORITY is established FIRST, before
                # any scope check reads `game.season_id`. This override runs
                # ahead of the base's own guard, so without this line a Game
                # whose denormalized Season is NULL or drifted reached
                # `require_slot_belongs_to_season` with that value and was
                # refused `season_missing`/`venue_access_missing` — a
                # misleading "grant this Season venue access" remediation for
                # a row whose real problem is that it disagrees with its own
                # competition, and, worse, an answer produced with no Season
                # lock held at all. The guard refuses the drift with the right
                # reason and, when it passes, has PROVEN `game.season_id ==
                # league_season.season_id`, so the value handed to the scope
                # check below is now the canonical one by construction. It
                # also returns the row re-fetched under that lock; the base's
                # own guard re-locks the same row idempotently.
                game = self._guard_game_season(game)
                # #283 Slice D/E: only a regular game needs league-ice isolation;
                # both kinds need their slot's venue to serve the game's Season.
                if game.game_type != "exhibition":
                    require_game_league_id(self.store, game)
                require_slot_belongs_to_season(
                    self.store, game.ice_slot_id, game.season_id)
            return _BaseSetupService.publish_game.__wrapped__(
                self, game_id, published, actor_id)
