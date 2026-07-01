"""League + Arena setup service.

Builds the scheduling universe before games exist: league → season →
division, club → team, venue → rink → ice slot, and manual game creation.
Pure logic over the store with an injected clock; every create is audited.
"""

import functools
from datetime import datetime, timezone
from typing import Callable, List, Optional

from ..domain import (
    Club,
    Division,
    Game,
    IceSlot,
    IceSlotStatus,
    IceSlotType,
    League,
    GameResult,
    Official,
    OfficialAssignment,
    OfficialAssignmentStatus,
    OfficialRole,
    Player,
    ResultStatus,
    Position,
    Rink,
    Season,
    SetupAuditLog,
    Team,
    Venue,
)
from ..domain.errors import (
    DivisionMismatchError,
    NotEligibleError,
    NotFoundError,
    ScheduleConflictError,
    ValidationError,
)
from ..store import InMemoryStore


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _transactional(fn):
    """Wrap a mutating service method in a single store transaction."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self.store.transaction():
            return fn(self, *args, **kwargs)
    return wrapper


class SetupService:
    def __init__(self, store: InMemoryStore, clock: Callable[[], datetime] = _utcnow):
        self.store = store
        self.clock = clock

    # -- helpers -----------------------------------------------------------
    def _require_name(self, name: str, field_name: str = "name") -> str:
        if name is None or not str(name).strip():
            raise ValidationError(f"{field_name} is required.")
        return str(name).strip()

    def _require_utc(self, dt, field_name: str) -> datetime:
        if not isinstance(dt, datetime):
            raise ValidationError(f"{field_name} must be a datetime.")
        if dt.tzinfo is None or dt.utcoffset() is None:
            raise ValidationError(
                f"{field_name} must be a timezone-aware UTC datetime."
            )
        return dt.astimezone(timezone.utc)

    def _audit(self, action: str, entity_type: str, entity_id: str,
               actor_id: Optional[str] = None, detail: Optional[dict] = None
               ) -> SetupAuditLog:
        return self.store.add_setup_audit(SetupAuditLog(
            id=self.store.next_id("setupaudit"),
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            at=self.clock(),
            actor_id=actor_id,
            detail=detail or {},
        ))

    # -- league / season / division ---------------------------------------
    @_transactional
    def create_league(self, name: str, country: str = "", timezone_name: str = "UTC",
                      actor_id: Optional[str] = None) -> League:
        league = League(id=self.store.next_id("league"),
                        name=self._require_name(name), country=country,
                        timezone=timezone_name or "UTC")
        self.store.add_league(league)
        self._audit("league_created", "league", league.id, actor_id)
        return league

    @_transactional
    def create_season(self, league_id: str, name: str,
                      start_date: Optional[datetime] = None,
                      end_date: Optional[datetime] = None,
                      actor_id: Optional[str] = None) -> Season:
        if self.store.get_league(league_id) is None:
            raise NotFoundError(f"League {league_id} not found.")
        start = self._require_utc(start_date, "start_date") if start_date else None
        end = self._require_utc(end_date, "end_date") if end_date else None
        if start and end and end < start:
            raise ValidationError("end_date cannot be before start_date.")
        season = Season(id=self.store.next_id("season"), league_id=league_id,
                        name=self._require_name(name), start_date=start, end_date=end)
        self.store.add_season(season)
        self._audit("season_created", "season", season.id, actor_id,
                    {"league_id": league_id})
        return season

    @_transactional
    def create_division(self, season_id: str, name: str, age_group: str = "",
                        actor_id: Optional[str] = None) -> Division:
        if self.store.get_season(season_id) is None:
            raise NotFoundError(f"Season {season_id} not found.")
        division = Division(id=self.store.next_id("division"), season_id=season_id,
                            name=self._require_name(name), age_group=age_group)
        self.store.add_division(division)
        self._audit("division_created", "division", division.id, actor_id,
                    {"season_id": season_id})
        return division

    # -- club / team -------------------------------------------------------
    @_transactional
    def create_club(self, name: str, country: str = "",
                    actor_id: Optional[str] = None) -> Club:
        club = Club(id=self.store.next_id("club"), name=self._require_name(name),
                    country=country)
        self.store.add_club(club)
        self._audit("club_created", "club", club.id, actor_id)
        return club

    @_transactional
    def create_team(self, club_id: str, division_id: str, name: str,
                    actor_id: Optional[str] = None) -> Team:
        if self.store.get_club(club_id) is None:
            raise NotFoundError(f"Club {club_id} not found.")
        division = self.store.get_division(division_id)
        if division is None:
            raise NotFoundError(f"Division {division_id} not found.")
        team = Team(id=self.store.next_id("team"), name=self._require_name(name),
                    division=division.name, club_id=club_id, division_id=division_id)
        self.store.add_team(team)
        self._audit("team_created", "team", team.id, actor_id,
                    {"club_id": club_id, "division_id": division_id})
        return team

    # -- venue / rink / ice slot ------------------------------------------
    @_transactional
    def create_venue(self, name: str, address: str = "", timezone_name: str = "UTC",
                     actor_id: Optional[str] = None) -> Venue:
        venue = Venue(id=self.store.next_id("venue"), name=self._require_name(name),
                      address=address, timezone=timezone_name or "UTC")
        self.store.add_venue(venue)
        self._audit("venue_created", "venue", venue.id, actor_id)
        return venue

    @_transactional
    def create_rink(self, venue_id: str, name: str,
                    actor_id: Optional[str] = None) -> Rink:
        if self.store.get_venue(venue_id) is None:
            raise NotFoundError(f"Venue {venue_id} not found.")
        rink = Rink(id=self.store.next_id("rink"), venue_id=venue_id,
                    name=self._require_name(name))
        self.store.add_rink(rink)
        self._audit("rink_created", "rink", rink.id, actor_id, {"venue_id": venue_id})
        return rink

    @_transactional
    def create_ice_slot(self, rink_id: str, start_time: datetime, end_time: datetime,
                        slot_type: IceSlotType = IceSlotType.GAME,
                        actor_id: Optional[str] = None) -> IceSlot:
        if self.store.get_rink(rink_id) is None:
            raise NotFoundError(f"Rink {rink_id} not found.")
        start = self._require_utc(start_time, "start_time")
        end = self._require_utc(end_time, "end_time")
        if end <= start:
            raise ValidationError("end_time must be after start_time.")
        # No two slots may overlap on the same rink (adjacent is fine).
        for ex in self.store.all_ice_slots():
            if ex.rink_id != rink_id:
                continue
            if start < ex.end_time and end > ex.start_time:
                raise ScheduleConflictError(
                    f"Ice slot overlaps existing slot {ex.id} on this rink."
                )
        # Only GAME ice is bookable for league games; other types are blocked
        # so they cannot be scheduled into.
        status = (IceSlotStatus.AVAILABLE if slot_type == IceSlotType.GAME
                  else IceSlotStatus.BLOCKED)
        slot = IceSlot(id=self.store.next_id("slot"), rink_id=rink_id,
                       start_time=start, end_time=end, slot_type=slot_type,
                       status=status)
        self.store.add_ice_slot(slot)
        self._audit("ice_slot_created", "ice_slot", slot.id, actor_id,
                    {"rink_id": rink_id, "slot_type": slot_type.value})
        return slot

    # -- manual game creation ---------------------------------------------
    @_transactional
    def create_game(self, season_id: str, division_id: str, home_team_id: str,
                    away_team_id: str, ice_slot_id: str,
                    target_goalies: int = 1, target_skaters: int = 15,
                    max_skaters: int = 18, allow_division_override: bool = False,
                    actor_id: Optional[str] = None) -> Game:
        season = self.store.get_season(season_id)
        if season is None:
            raise NotFoundError(f"Season {season_id} not found.")
        division = self.store.get_division(division_id)
        if division is None:
            raise NotFoundError(f"Division {division_id} not found.")
        if division.season_id != season_id:
            raise ValidationError(
                "Division does not belong to the given season."
            )
        if home_team_id == away_team_id:
            raise ValidationError("A team cannot play itself.")

        home = self.store.get_team(home_team_id)
        away = self.store.get_team(away_team_id)
        if home is None:
            raise NotFoundError(f"Team {home_team_id} not found.")
        if away is None:
            raise NotFoundError(f"Team {away_team_id} not found.")

        if not allow_division_override:
            for team in (home, away):
                if team.division_id != division_id:
                    raise DivisionMismatchError(
                        f"{team.name} is not in division {division.name}. "
                        f"Use the override flag to force this game."
                    )

        slot = self.store.get_ice_slot(ice_slot_id)
        if slot is None:
            raise NotFoundError(f"Ice slot {ice_slot_id} not found.")
        if slot.slot_type != IceSlotType.GAME:
            raise ValidationError(
                "Only game ice slots can host a game (not maintenance / "
                "public skate / practice / tournament)."
            )
        if slot.status != IceSlotStatus.AVAILABLE:
            raise ScheduleConflictError(
                f"Ice slot {ice_slot_id} is not available."
            )
        clash = self.store.game_using_ice_slot(ice_slot_id)
        if clash is not None:
            raise ScheduleConflictError(
                f"Ice slot {ice_slot_id} is already used by game {clash.id}."
            )
        # Neither team may already have an overlapping (non-cancelled) game.
        for ex in self.store.all_games():
            if ex.cancelled or ex.ice_slot_id is None:
                continue
            ex_slot = self.store.get_ice_slot(ex.ice_slot_id)
            if ex_slot is None:
                continue
            overlaps = (slot.start_time < ex_slot.end_time
                        and slot.end_time > ex_slot.start_time)
            same_team = (ex.home_team_id in (home_team_id, away_team_id)
                         or ex.away_team_id in (home_team_id, away_team_id))
            if overlaps and same_team:
                raise ScheduleConflictError(
                    f"A team already has an overlapping game {ex.id}."
                )

        rink = self.store.get_rink(slot.rink_id)
        game = Game(
            id=self.store.next_id("game"),
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            start_time=slot.start_time,
            end_time=slot.end_time,
            rink=rink.name if rink else None,
            target_goalies=target_goalies,
            target_skaters=target_skaters,
            max_skaters=max_skaters,
            season_id=season_id,
            division_id=division_id,
            ice_slot_id=ice_slot_id,
        )
        self.store.add_game(game)
        # Mark the slot allocated so it reads as taken across the arena.
        slot.status = IceSlotStatus.ALLOCATED
        self.store.save_ice_slot(slot)
        self._audit("game_created", "game", game.id, actor_id, {
            "season_id": season_id, "division_id": division_id,
            "home_team_id": home_team_id, "away_team_id": away_team_id,
            "ice_slot_id": ice_slot_id, "override": allow_division_override,
        })
        return game

    @_transactional
    def publish_game(self, game_id: str, published: bool = True,
                     actor_id: Optional[str] = None) -> Game:
        game = self.store.get_game(game_id)
        if game is None:
            raise NotFoundError(f"Game {game_id} not found.")
        game.published = published
        self.store.save_game(game)
        self._audit("game_published" if published else "game_unpublished",
                    "game", game_id, actor_id)
        return game

    @_transactional
    def move_game(self, game_id: str, new_ice_slot_id: str, reason: str = "",
                  actor_id: Optional[str] = None) -> Game:
        """Move a game to another available game ice slot (drag/drop)."""
        game = self.store.get_game(game_id)
        if game is None:
            raise NotFoundError(f"Game {game_id} not found.",
                                details={"reason": "game_missing"})
        if game.cancelled:
            raise ValidationError("Cannot move a cancelled game.",
                                  details={"reason": "game_cancelled"})

        new_slot = self.store.get_ice_slot(new_ice_slot_id)
        if new_slot is None:
            raise NotFoundError(f"Ice slot {new_ice_slot_id} not found.",
                                details={"reason": "slot_missing"})
        if new_slot.id == game.ice_slot_id:
            raise ValidationError("Game is already in that ice slot.",
                                  details={"reason": "same_slot"})
        if new_slot.slot_type != IceSlotType.GAME:
            raise ValidationError(
                "Only game ice slots can host a game (not maintenance / "
                "public skate / practice / tournament).",
                details={"reason": "not_game_slot",
                         "slot_type": new_slot.slot_type.value},
            )
        if new_slot.status != IceSlotStatus.AVAILABLE:
            raise ScheduleConflictError(
                f"Ice slot {new_ice_slot_id} is not available.",
                details={"reason": "slot_unavailable",
                         "slot_status": new_slot.status.value},
            )
        # Neither team may already have an overlapping game (excluding this one).
        for ex in self.store.all_games():
            if ex.id == game_id or ex.cancelled or ex.ice_slot_id is None:
                continue
            ex_slot = self.store.get_ice_slot(ex.ice_slot_id)
            if ex_slot is None:
                continue
            overlaps = (new_slot.start_time < ex_slot.end_time
                        and new_slot.end_time > ex_slot.start_time)
            same_team = (ex.home_team_id in (game.home_team_id, game.away_team_id)
                         or ex.away_team_id in (game.home_team_id, game.away_team_id))
            if overlaps and same_team:
                raise ScheduleConflictError(
                    f"A team already has an overlapping game {ex.id}.",
                    details={"reason": "team_overlap", "conflict_game_id": ex.id},
                )

        old_slot_id = game.ice_slot_id
        if old_slot_id:
            old_slot = self.store.get_ice_slot(old_slot_id)
            if old_slot is not None:
                old_slot.status = IceSlotStatus.AVAILABLE
                self.store.save_ice_slot(old_slot)
        new_slot.status = IceSlotStatus.ALLOCATED
        self.store.save_ice_slot(new_slot)

        rink = self.store.get_rink(new_slot.rink_id)
        was_published = game.published
        was_locked = game.locked
        game.ice_slot_id = new_slot.id
        game.start_time = new_slot.start_time
        game.end_time = new_slot.end_time
        game.rink = rink.name if rink else None
        if was_published:
            # The fixture changed — it must be re-published before going public.
            game.published = False
        if was_locked:
            # Time/rink changed — players must reconfirm, so unlock the roster.
            game.locked = False
        self.store.save_game(game)
        self._audit("game_moved", "game", game_id, actor_id, {
            "old_slot_id": old_slot_id, "new_slot_id": new_slot.id,
            "reason": reason, "unpublished": was_published,
            "roster_unlocked": was_locked,
        })
        return game

    # -- convenience: add a player to a team ------------------------------
    @_transactional
    def add_player(self, team_id: str, name: str, position: Position,
                   jersey_number: Optional[int] = None,
                   actor_id: Optional[str] = None) -> Player:
        if self.store.get_team(team_id) is None:
            raise NotFoundError(f"Team {team_id} not found.")
        player = Player(id=self.store.next_id("player"), team_id=team_id,
                        name=self._require_name(name), position=position,
                        jersey_number=jersey_number)
        self.store.add_player(player)
        self._audit("player_added", "player", player.id, actor_id,
                    {"team_id": team_id})
        return player

    # -- officials (#30) ---------------------------------------------------
    @_transactional
    def create_official(self, name: str, home_club_id: Optional[str] = None,
                        actor_id: Optional[str] = None) -> Official:
        if home_club_id and self.store.get_club(home_club_id) is None:
            raise NotFoundError(f"Club {home_club_id} not found.")
        official = Official(id=self.store.next_id("official"),
                            name=self._require_name(name),
                            home_club_id=home_club_id or None)
        self.store.add_official(official)
        self._audit("official_created", "official", official.id, actor_id)
        return official

    def _game_time(self, game: Game):
        slot = self.store.get_ice_slot(game.ice_slot_id) if game.ice_slot_id else None
        start = game.start_time or (slot.start_time if slot else None)
        end = game.end_time or (slot.end_time if slot else None)
        return start, end

    @_transactional
    def assign_official(self, game_id: str, official_id: str, role: OfficialRole,
                        actor_id: Optional[str] = None) -> OfficialAssignment:
        """Propose an official for a role on a game, with conflict checks."""
        game = self.store.get_game(game_id)
        if game is None:
            raise NotFoundError(f"Game {game_id} not found.")
        if game.cancelled:
            raise ValidationError("Cannot assign officials to a cancelled game.")
        official = self.store.get_official(official_id)
        if official is None:
            raise NotFoundError(f"Official {official_id} not found.")
        if not official.is_active:
            raise NotEligibleError(f"{official.name} is not an active official.")

        # Conflict of interest: an official from a club playing in this game.
        if official.home_club_id:
            for tid in (game.home_team_id, game.away_team_id):
                team = self.store.get_team(tid) if tid else None
                if team is not None and team.club_id == official.home_club_id:
                    raise NotEligibleError(
                        f"{official.name} has a club conflict with this game.")

        # Already actively assigned to this game (any role)?
        for a in self.store.assignments_for_game(game_id):
            if a.official_id == official_id and a.status.is_active:
                raise ScheduleConflictError(
                    f"{official.name} is already assigned to this game.")

        # Time overlap with another active assignment on a different game.
        start, end = self._game_time(game)
        if start is not None and end is not None:
            for a in self.store.assignments_for_official(official_id):
                if a.game_id == game_id or not a.status.is_active:
                    continue
                other = self.store.get_game(a.game_id)
                if other is None or other.cancelled:
                    continue
                o_start, o_end = self._game_time(other)
                if o_start is not None and o_end is not None \
                        and start < o_end and end > o_start:
                    raise ScheduleConflictError(
                        f"{official.name} is already officiating an overlapping game.")

        now = self.clock()
        assignment = OfficialAssignment(
            id=self.store.next_id("assign"),
            game_id=game_id, official_id=official_id, role=role,
            status=OfficialAssignmentStatus.PROPOSED,
            assigned_at=now, assigned_by=actor_id,
        )
        self.store.add_official_assignment(assignment)
        self._audit("official_assigned", "official_assignment", assignment.id,
                    actor_id, {"game_id": game_id, "official_id": official_id,
                               "role": role.value})
        return assignment

    @_transactional
    def respond_assignment(self, assignment_id: str, accept: bool,
                           actor_id: Optional[str] = None) -> OfficialAssignment:
        """An official accepts or declines a proposed assignment."""
        a = self.store.get_official_assignment(assignment_id)
        if a is None:
            raise NotFoundError(f"Assignment {assignment_id} not found.")
        if a.status != OfficialAssignmentStatus.PROPOSED:
            raise ValidationError("Only a proposed assignment can be responded to.")
        a.status = (OfficialAssignmentStatus.ACCEPTED if accept
                    else OfficialAssignmentStatus.DECLINED)
        a.responded_at = self.clock()
        self.store.save_official_assignment(a)
        self._audit("assignment_accepted" if accept else "assignment_declined",
                    "official_assignment", a.id, actor_id)
        return a

    @_transactional
    def unassign_official(self, assignment_id: str,
                          actor_id: Optional[str] = None) -> OfficialAssignment:
        """Remove an official assignment from a game entirely."""
        a = self.store.get_official_assignment(assignment_id)
        if a is None:
            raise NotFoundError(f"Assignment {assignment_id} not found.")
        self.store.remove_official_assignment(assignment_id)
        self._audit("official_unassigned", "official_assignment", assignment_id,
                    actor_id, {"game_id": a.game_id, "official_id": a.official_id})
        return a

    # -- results (#31) -----------------------------------------------------
    @_transactional
    def record_result(self, game_id: str, home_score: int, away_score: int,
                      actor_id: Optional[str] = None) -> GameResult:
        """Enter (or re-enter) a score as a DRAFT result. Approval finalizes it."""
        game = self.store.get_game(game_id)
        if game is None:
            raise NotFoundError(f"Game {game_id} not found.")
        if game.cancelled:
            raise ValidationError("Cannot record a result for a cancelled game.")
        hs, as_ = self._require_score(home_score), self._require_score(away_score)
        result = self.store.result_for_game(game_id)
        if result is not None and result.status == ResultStatus.FINAL:
            raise ValidationError(
                "Result is already final. It can't be edited without reopening.")
        now = self.clock()
        if result is None:
            result = GameResult(id=self.store.next_id("result"), game_id=game_id,
                                home_score=hs, away_score=as_,
                                status=ResultStatus.DRAFT,
                                recorded_by=actor_id, recorded_at=now)
            self.store.add_game_result(result)
        else:
            result.home_score, result.away_score = hs, as_
            result.recorded_by, result.recorded_at = actor_id, now
            self.store.save_game_result(result)
        self._audit("result_recorded", "game_result", result.id, actor_id,
                    {"game_id": game_id, "home_score": hs, "away_score": as_})
        return result

    @_transactional
    def approve_result(self, game_id: str,
                       actor_id: Optional[str] = None) -> GameResult:
        """Approve a draft result → FINAL. Only a FINAL result affects standings."""
        result = self.store.result_for_game(game_id)
        if result is None:
            raise NotFoundError("No result recorded for this game yet.")
        if result.status == ResultStatus.FINAL:
            raise ValidationError("Result is already final.")
        result.status = ResultStatus.FINAL
        result.approved_by = actor_id
        result.approved_at = self.clock()
        self.store.save_game_result(result)
        self._audit("result_approved", "game_result", result.id, actor_id,
                    {"game_id": game_id})
        return result

    def _require_score(self, value) -> int:
        try:
            score = int(value)
        except (TypeError, ValueError):
            raise ValidationError("Score must be a whole number.")
        if score < 0:
            raise ValidationError("Score can't be negative.")
        return score

    # -- listings ----------------------------------------------------------
    def list_leagues(self) -> List[League]:
        return list(self.store.all_leagues())

    def list_seasons(self, league_id: str) -> List[Season]:
        return self.store.seasons_for_league(league_id)

    def list_divisions(self, season_id: str) -> List[Division]:
        return self.store.divisions_for_season(season_id)
