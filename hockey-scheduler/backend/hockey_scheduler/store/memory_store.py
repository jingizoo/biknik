"""In-memory persistence for the first slice.

This is deliberately small and synchronous. The service layer depends only on
the public methods here, so a SQL-backed implementation can be substituted
later without touching domain logic.
"""

from itertools import count
from typing import Dict, List, Optional

from ..domain import (
    AuditLog,
    Club,
    Division,
    Game,
    GameAvailability,
    GameRosterEntry,
    IceSlot,
    League,
    NotificationEvent,
    Player,
    Rink,
    Season,
    SetupAuditLog,
    SubstituteEnrollment,
    Team,
    Venue,
)


class InMemoryStore:
    def __init__(self) -> None:
        self.teams: Dict[str, Team] = {}
        self.players: Dict[str, Player] = {}
        self.games: Dict[str, Game] = {}
        self.roster_entries: Dict[str, GameRosterEntry] = {}
        self.availability: Dict[str, GameAvailability] = {}
        self.substitutes: Dict[str, SubstituteEnrollment] = {}
        self.audit: List[AuditLog] = []
        self.notifications: List[NotificationEvent] = []
        # Organization & arena setup collections.
        self.leagues: Dict[str, League] = {}
        self.seasons: Dict[str, Season] = {}
        self.divisions: Dict[str, Division] = {}
        self.clubs: Dict[str, Club] = {}
        self.venues: Dict[str, Venue] = {}
        self.rinks: Dict[str, Rink] = {}
        self.ice_slots: Dict[str, IceSlot] = {}
        self.setup_audit: List[SetupAuditLog] = []
        self._counters: Dict[str, count] = {}

    # -- id generation -----------------------------------------------------
    def next_id(self, prefix: str) -> str:
        if prefix not in self._counters:
            self._counters[prefix] = count(1)
        return f"{prefix}_{next(self._counters[prefix])}"

    # -- teams / players ---------------------------------------------------
    def add_team(self, team: Team) -> Team:
        self.teams[team.id] = team
        return team

    def add_player(self, player: Player) -> Player:
        self.players[player.id] = player
        return player

    def get_player(self, player_id: str) -> Optional[Player]:
        return self.players.get(player_id)

    def players_for_team(self, team_id: str) -> List[Player]:
        return [p for p in self.players.values() if p.team_id == team_id]

    # -- games -------------------------------------------------------------
    def add_game(self, game: Game) -> Game:
        self.games[game.id] = game
        return game

    def get_game(self, game_id: str) -> Optional[Game]:
        return self.games.get(game_id)

    # -- roster entries ----------------------------------------------------
    def add_roster_entry(self, entry: GameRosterEntry) -> GameRosterEntry:
        self.roster_entries[entry.id] = entry
        return entry

    def roster_for_game(self, game_id: str) -> List[GameRosterEntry]:
        return [e for e in self.roster_entries.values() if e.game_id == game_id]

    def roster_entry_for_player(
        self, game_id: str, player_id: str
    ) -> Optional[GameRosterEntry]:
        for e in self.roster_entries.values():
            if e.game_id == game_id and e.player_id == player_id:
                return e
        return None

    # -- availability ------------------------------------------------------
    def upsert_availability(self, av: GameAvailability) -> GameAvailability:
        self.availability[av.id] = av
        return av

    def availability_for_game(self, game_id: str) -> List[GameAvailability]:
        return [a for a in self.availability.values() if a.game_id == game_id]

    def availability_for_player(
        self, game_id: str, player_id: str
    ) -> Optional[GameAvailability]:
        for a in self.availability.values():
            if a.game_id == game_id and a.player_id == player_id:
                return a
        return None

    # -- substitutes -------------------------------------------------------
    def add_substitute(self, sub: SubstituteEnrollment) -> SubstituteEnrollment:
        self.substitutes[sub.id] = sub
        return sub

    def substitutes_for_game(self, game_id: str) -> List[SubstituteEnrollment]:
        return [s for s in self.substitutes.values() if s.game_id == game_id]

    def substitute_for_player(
        self, game_id: str, player_id: str
    ) -> Optional[SubstituteEnrollment]:
        for s in self.substitutes.values():
            if s.game_id == game_id and s.player_id == player_id:
                return s
        return None

    # -- audit / notifications --------------------------------------------
    def add_audit(self, entry: AuditLog) -> AuditLog:
        self.audit.append(entry)
        return entry

    def audit_for_game(self, game_id: str) -> List[AuditLog]:
        return [a for a in self.audit if a.game_id == game_id]

    def add_notification(self, event: NotificationEvent) -> NotificationEvent:
        self.notifications.append(event)
        return event

    def notifications_for_game(self, game_id: str) -> List[NotificationEvent]:
        return [n for n in self.notifications if n.game_id == game_id]

    # -- organization & arena setup ---------------------------------------
    def add_league(self, league: League) -> League:
        self.leagues[league.id] = league
        return league

    def get_league(self, league_id: str) -> Optional[League]:
        return self.leagues.get(league_id)

    def add_season(self, season: Season) -> Season:
        self.seasons[season.id] = season
        return season

    def get_season(self, season_id: str) -> Optional[Season]:
        return self.seasons.get(season_id)

    def seasons_for_league(self, league_id: str) -> List[Season]:
        return [s for s in self.seasons.values() if s.league_id == league_id]

    def add_division(self, division: Division) -> Division:
        self.divisions[division.id] = division
        return division

    def get_division(self, division_id: str) -> Optional[Division]:
        return self.divisions.get(division_id)

    def divisions_for_season(self, season_id: str) -> List[Division]:
        return [d for d in self.divisions.values() if d.season_id == season_id]

    def add_club(self, club: Club) -> Club:
        self.clubs[club.id] = club
        return club

    def get_club(self, club_id: str) -> Optional[Club]:
        return self.clubs.get(club_id)

    def get_team(self, team_id: str) -> Optional[Team]:
        return self.teams.get(team_id)

    def add_venue(self, venue: Venue) -> Venue:
        self.venues[venue.id] = venue
        return venue

    def get_venue(self, venue_id: str) -> Optional[Venue]:
        return self.venues.get(venue_id)

    def add_rink(self, rink: Rink) -> Rink:
        self.rinks[rink.id] = rink
        return rink

    def get_rink(self, rink_id: str) -> Optional[Rink]:
        return self.rinks.get(rink_id)

    def add_ice_slot(self, slot: IceSlot) -> IceSlot:
        self.ice_slots[slot.id] = slot
        return slot

    def get_ice_slot(self, slot_id: str) -> Optional[IceSlot]:
        return self.ice_slots.get(slot_id)

    def game_using_ice_slot(self, slot_id: str) -> Optional[Game]:
        for g in self.games.values():
            if g.ice_slot_id == slot_id and not g.cancelled:
                return g
        return None

    def add_setup_audit(self, entry: SetupAuditLog) -> SetupAuditLog:
        self.setup_audit.append(entry)
        return entry
