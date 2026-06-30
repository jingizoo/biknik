"""League + Arena setup service.

Builds the scheduling universe before games exist: league → season →
division, club → team, venue → rink → ice slot, and manual game creation.
Pure logic over the store with an injected clock; every create is audited.
"""

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
    Player,
    Position,
    Rink,
    Season,
    SetupAuditLog,
    Team,
    Venue,
)
from ..domain.errors import (
    DivisionMismatchError,
    NotFoundError,
    ScheduleConflictError,
    ValidationError,
)
from ..store import InMemoryStore


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    def create_league(self, name: str, country: str = "", timezone_name: str = "UTC",
                      actor_id: Optional[str] = None) -> League:
        league = League(id=self.store.next_id("league"),
                        name=self._require_name(name), country=country,
                        timezone=timezone_name or "UTC")
        self.store.add_league(league)
        self._audit("league_created", "league", league.id, actor_id)
        return league

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
    def create_club(self, name: str, country: str = "",
                    actor_id: Optional[str] = None) -> Club:
        club = Club(id=self.store.next_id("club"), name=self._require_name(name),
                    country=country)
        self.store.add_club(club)
        self._audit("club_created", "club", club.id, actor_id)
        return club

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
    def create_venue(self, name: str, address: str = "", timezone_name: str = "UTC",
                     actor_id: Optional[str] = None) -> Venue:
        venue = Venue(id=self.store.next_id("venue"), name=self._require_name(name),
                      address=address, timezone=timezone_name or "UTC")
        self.store.add_venue(venue)
        self._audit("venue_created", "venue", venue.id, actor_id)
        return venue

    def create_rink(self, venue_id: str, name: str,
                    actor_id: Optional[str] = None) -> Rink:
        if self.store.get_venue(venue_id) is None:
            raise NotFoundError(f"Venue {venue_id} not found.")
        rink = Rink(id=self.store.next_id("rink"), venue_id=venue_id,
                    name=self._require_name(name))
        self.store.add_rink(rink)
        self._audit("rink_created", "rink", rink.id, actor_id, {"venue_id": venue_id})
        return rink

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
        for ex in self.store.ice_slots.values():
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
        for ex in self.store.games.values():
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
        self._audit("game_created", "game", game.id, actor_id, {
            "season_id": season_id, "division_id": division_id,
            "home_team_id": home_team_id, "away_team_id": away_team_id,
            "ice_slot_id": ice_slot_id, "override": allow_division_override,
        })
        return game

    # -- convenience: add a player to a team ------------------------------
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

    # -- listings ----------------------------------------------------------
    def list_leagues(self) -> List[League]:
        return list(self.store.leagues.values())

    def list_seasons(self, league_id: str) -> List[Season]:
        return self.store.seasons_for_league(league_id)

    def list_divisions(self, season_id: str) -> List[Division]:
        return self.store.divisions_for_season(season_id)
