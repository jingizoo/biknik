"""In-memory persistence for the first slice.

This is deliberately small and synchronous. The service layer depends only on
the public methods here, so a SQL-backed implementation can be substituted
later without touching domain logic.
"""

from itertools import count
from typing import Dict, List, Optional

from ..domain import (
    AuditLog,
    Game,
    GameAvailability,
    GameRosterEntry,
    NotificationEvent,
    Player,
    SubstituteEnrollment,
    Team,
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
