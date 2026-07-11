"""In-memory persistence for the first slice.

This is deliberately small and synchronous. The service layer depends only on
the public methods here, so a SQL-backed implementation can be substituted
later without touching domain logic.
"""

from contextlib import contextmanager
from datetime import datetime
import threading
from itertools import count
from typing import Dict, List, Optional

from ..domain import (
    AuditLog,
    CalendarFeedToken,
    Club,
    OfficialAvailability,
    Division,
    Game,
    GameAvailability,
    GameRosterEntry,
    IceSlot,
    GameResult,
    ContactDestination,
    DeliveryStatus,
    DeviceToken,
    League,
    Level,
    Notification,
    NotificationDelivery,
    GuardianLink,
    InstallationState,
    NotificationEvent,
    NotificationPreference,
    NotificationRecipient,
    Official,
    OfficialAssignment,
    Player,
    RescheduleRequest,
    Rink,
    Season,
    SeasonTeamRegistration,
    Session,
    SetupAuditLog,
    SubstituteEnrollment,
    Team,
    UserAccount,
    Organization,
    Venue,
)


class InMemoryStore:
    backend = "memory"  # store kind, for the runtime status endpoint (#72)

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
        self.organizations: Dict[str, Organization] = {}
        self.leagues: Dict[str, League] = {}
        self.seasons: Dict[str, Season] = {}
        self.levels: Dict[str, Level] = {}
        self.divisions: Dict[str, Division] = {}
        self.season_team_registrations: Dict[str, SeasonTeamRegistration] = {}
        self.clubs: Dict[str, Club] = {}
        self.venues: Dict[str, Venue] = {}
        self.rinks: Dict[str, Rink] = {}
        self.ice_slots: Dict[str, IceSlot] = {}
        self.officials: Dict[str, Official] = {}
        self.official_assignments: Dict[str, OfficialAssignment] = {}
        self.game_results: Dict[str, GameResult] = {}
        self.feed_notifications: Dict[str, Notification] = {}
        self.notif_recipients: Dict[str, NotificationRecipient] = {}
        self.notif_deliveries: Dict[str, NotificationDelivery] = {}
        self.contact_destinations: Dict[str, ContactDestination] = {}
        self.device_tokens: Dict[str, DeviceToken] = {}
        self.notification_preferences: Dict[str, NotificationPreference] = {}
        self.calendar_feed_tokens: Dict[str, CalendarFeedToken] = {}
        self.official_availability: Dict[str, OfficialAvailability] = {}
        self.installation_state: Dict[str, InstallationState] = {}
        self.user_accounts: Dict[str, UserAccount] = {}
        self.sessions: Dict[str, Session] = {}
        self.guardian_links: Dict[str, GuardianLink] = {}
        self.reschedule_requests: Dict[str, RescheduleRequest] = {}
        self.setup_audit: List[SetupAuditLog] = []
        self._counters: Dict[str, count] = {}
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self):
        """Serialize atomic service operations in the threaded test/demo store."""
        with self._lock:
            yield

    def close(self) -> None:
        pass

    # -- operational health (#90) ------------------------------------------
    def db_reachable(self) -> bool:
        return True  # the in-memory store is always reachable

    def migration_status(self) -> dict:
        # No SQL migrations for the in-memory store; trivially current.
        return {"backend": self.backend, "applied": [], "expected": [],
                "current": True}

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

    def all_players(self) -> List[Player]:
        return list(self.players.values())

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

    def add_level(self, level: Level) -> Level:
        self.levels[level.id] = level
        return level

    def get_level(self, level_id: str) -> Optional[Level]:
        return self.levels.get(level_id)

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

    def teams_for_league(self, league_id: str) -> List[Team]:
        return [t for t in self.teams.values() if t.league_id == league_id]

    # -- season team registrations (#180) ----------------------------------
    def add_season_team_registration(
            self, reg: SeasonTeamRegistration) -> SeasonTeamRegistration:
        self.season_team_registrations[reg.id] = reg
        return reg

    def get_season_team_registration(
            self, reg_id: str) -> Optional[SeasonTeamRegistration]:
        return self.season_team_registrations.get(reg_id)

    def save_season_team_registration(
            self, reg: SeasonTeamRegistration) -> SeasonTeamRegistration:
        self.season_team_registrations[reg.id] = reg
        return reg

    def all_season_team_registrations(self) -> List[SeasonTeamRegistration]:
        return list(self.season_team_registrations.values())

    def registrations_for_season(
            self, season_id: str) -> List[SeasonTeamRegistration]:
        return [r for r in self.season_team_registrations.values()
                if r.season_id == season_id]

    def registration_for_team_in_season(
            self, season_id: str, team_id: str) -> Optional[SeasonTeamRegistration]:
        return next((r for r in self.season_team_registrations.values()
                     if r.season_id == season_id and r.team_id == team_id), None)

    def add_organization(self, org: Organization) -> Organization:
        self.organizations[org.id] = org
        return org

    def get_organization(self, org_id: str) -> Optional[Organization]:
        return self.organizations.get(org_id)

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

    # -- officials (#30) ---------------------------------------------------
    def add_official(self, official: Official) -> Official:
        self.officials[official.id] = official
        return official

    def get_official(self, official_id: str) -> Optional[Official]:
        return self.officials.get(official_id)

    def all_officials(self) -> List[Official]:
        return list(self.officials.values())

    def save_official(self, official: Official) -> Official:
        self.officials[official.id] = official
        return official

    def add_official_assignment(self, a: OfficialAssignment) -> OfficialAssignment:
        self.official_assignments[a.id] = a
        return a

    def save_official_assignment(self, a: OfficialAssignment) -> OfficialAssignment:
        self.official_assignments[a.id] = a
        return a

    def get_official_assignment(self, assignment_id: str) -> Optional[OfficialAssignment]:
        return self.official_assignments.get(assignment_id)

    def all_official_assignments(self) -> List[OfficialAssignment]:
        return list(self.official_assignments.values())

    def assignments_for_game(self, game_id: str) -> List[OfficialAssignment]:
        return [a for a in self.official_assignments.values() if a.game_id == game_id]

    def assignments_for_official(self, official_id: str) -> List[OfficialAssignment]:
        return [a for a in self.official_assignments.values()
                if a.official_id == official_id]

    def remove_official_assignment(self, assignment_id: str) -> None:
        self.official_assignments.pop(assignment_id, None)

    # -- game results (#31) ------------------------------------------------
    def add_game_result(self, result: GameResult) -> GameResult:
        self.game_results[result.id] = result
        return result

    def save_game_result(self, result: GameResult) -> GameResult:
        self.game_results[result.id] = result
        return result

    def result_for_game(self, game_id: str) -> Optional[GameResult]:
        for r in self.game_results.values():
            if r.game_id == game_id:
                return r
        return None

    def all_game_results(self) -> List[GameResult]:
        return list(self.game_results.values())

    # -- feed notifications (#32) ------------------------------------------
    def add_notification_feed(self, n: Notification) -> Notification:
        self.feed_notifications[n.id] = n
        return n

    def save_notification_feed(self, n: Notification) -> Notification:
        self.feed_notifications[n.id] = n
        return n

    def get_notification_feed(self, notification_id: str) -> Optional[Notification]:
        return self.feed_notifications.get(notification_id)

    def all_notifications_feed(self) -> List[Notification]:
        return list(self.feed_notifications.values())

    # -- per-recipient read state (#57) ------------------------------------
    def get_notification_recipient(
            self, recipient_id: str) -> Optional[NotificationRecipient]:
        return self.notif_recipients.get(recipient_id)

    def save_notification_recipient(
            self, r: NotificationRecipient) -> NotificationRecipient:
        self.notif_recipients[r.id] = r
        return r

    def recipients_for_actor(
            self, actor_key: str) -> List[NotificationRecipient]:
        return [r for r in self.notif_recipients.values()
                if r.actor_key == actor_key]

    # -- notification delivery queue (#58) ---------------------------------
    def add_notification_delivery(
            self, d: NotificationDelivery) -> NotificationDelivery:
        self.notif_deliveries[d.id] = d
        return d

    def save_notification_delivery(
            self, d: NotificationDelivery) -> NotificationDelivery:
        self.notif_deliveries[d.id] = d
        return d

    def get_notification_delivery(
            self, delivery_id: str) -> Optional[NotificationDelivery]:
        return self.notif_deliveries.get(delivery_id)

    def deliveries_for_notification(
            self, notification_id: str) -> List[NotificationDelivery]:
        return [d for d in self.notif_deliveries.values()
                if d.notification_id == notification_id]

    def all_notification_deliveries(self) -> List[NotificationDelivery]:
        return list(self.notif_deliveries.values())

    def pending_deliveries(
            self, max_attempts: int) -> List[NotificationDelivery]:
        """Deliverable rows: still pending, or failed with attempts to spare."""
        return [d for d in self.notif_deliveries.values()
                if d.status == DeliveryStatus.PENDING
                or (d.status == DeliveryStatus.FAILED
                    and d.attempts < max_attempts)]

    # -- contact registry (#60) --------------------------------------------
    def add_contact_destination(
            self, c: ContactDestination) -> ContactDestination:
        self.contact_destinations[c.id] = c
        return c

    def save_contact_destination(
            self, c: ContactDestination) -> ContactDestination:
        self.contact_destinations[c.id] = c
        return c

    def get_contact_destination(
            self, recipient_ref: str, channel) -> Optional[ContactDestination]:
        for c in self.contact_destinations.values():
            if c.recipient_ref == recipient_ref and c.channel == channel:
                return c
        return None

    def all_contact_destinations(self) -> List[ContactDestination]:
        return list(self.contact_destinations.values())

    # -- device token registry (#65) ---------------------------------------
    def add_device_token(self, t: DeviceToken) -> DeviceToken:
        self.device_tokens[t.id] = t
        return t

    def save_device_token(self, t: DeviceToken) -> DeviceToken:
        self.device_tokens[t.id] = t
        return t

    def get_device_token(self, token_id: str) -> Optional[DeviceToken]:
        return self.device_tokens.get(token_id)

    def get_device_token_by_value(
            self, recipient_ref: str, token: str) -> Optional[DeviceToken]:
        for t in self.device_tokens.values():
            if t.recipient_ref == recipient_ref and t.token == token:
                return t
        return None

    def device_tokens_for(self, recipient_ref: str) -> List[DeviceToken]:
        return [t for t in self.device_tokens.values()
                if t.recipient_ref == recipient_ref]

    def active_device_token_for(
            self, recipient_ref: str) -> Optional[DeviceToken]:
        for t in self.device_tokens.values():
            if t.recipient_ref == recipient_ref and t.active:
                return t
        return None

    def all_device_tokens(self) -> List[DeviceToken]:
        return list(self.device_tokens.values())

    # -- official availability (#88) ---------------------------------------
    def add_official_availability(self, a: OfficialAvailability) -> OfficialAvailability:
        self.official_availability[a.id] = a
        return a

    def get_official_availability(self, avail_id: str) -> Optional[OfficialAvailability]:
        return self.official_availability.get(avail_id)

    def delete_official_availability(self, avail_id: str) -> None:
        self.official_availability.pop(avail_id, None)

    def availability_for_official(self, official_id: str) -> List[OfficialAvailability]:
        return [a for a in self.official_availability.values()
                if a.official_id == official_id]

    def save_official_availability(
            self, a: OfficialAvailability) -> OfficialAvailability:
        self.official_availability[a.id] = a
        return a

    # -- calendar feed tokens (#82) ----------------------------------------
    def add_calendar_feed_token(self, t: CalendarFeedToken) -> CalendarFeedToken:
        self.calendar_feed_tokens[t.id] = t
        return t

    def save_calendar_feed_token(self, t: CalendarFeedToken) -> CalendarFeedToken:
        self.calendar_feed_tokens[t.id] = t
        return t

    def get_calendar_feed_token(self, token_id: str) -> Optional[CalendarFeedToken]:
        return self.calendar_feed_tokens.get(token_id)

    def get_calendar_feed_token_by_hash(
            self, token_hash: str) -> Optional[CalendarFeedToken]:
        for t in self.calendar_feed_tokens.values():
            if t.token_hash == token_hash:
                return t
        return None

    def calendar_feed_tokens_for(
            self, actor_type: str, actor_ref: str) -> List[CalendarFeedToken]:
        return [t for t in self.calendar_feed_tokens.values()
                if t.actor_type == actor_type and t.actor_ref == actor_ref]

    # -- notification preferences (#81) ------------------------------------
    def save_notification_preference(
            self, p: NotificationPreference) -> NotificationPreference:
        self.notification_preferences[p.id] = p
        return p

    def get_notification_preference(
            self, recipient_ref: str, channel) -> Optional[NotificationPreference]:
        for p in self.notification_preferences.values():
            if p.recipient_ref == recipient_ref and p.channel == channel:
                return p
        return None

    def preferences_for_recipient(
            self, recipient_ref: str) -> List[NotificationPreference]:
        return [p for p in self.notification_preferences.values()
                if p.recipient_ref == recipient_ref]

    # -- installation claim state (#174) -----------------------------------
    def add_installation_state(
            self, state: InstallationState) -> InstallationState:
        self.installation_state[state.id] = state
        return state

    def get_installation_state(
            self, state_id: str) -> Optional[InstallationState]:
        return self.installation_state.get(state_id)

    # -- user accounts (#67) -------------------------------------------------
    def add_user_account(self, a: UserAccount) -> UserAccount:
        self.user_accounts[a.id] = a
        return a

    def save_user_account(self, a: UserAccount) -> UserAccount:
        self.user_accounts[a.id] = a
        return a

    def get_user_account(self, account_id: str) -> Optional[UserAccount]:
        return self.user_accounts.get(account_id)

    def get_user_account_by_username(self, username: str) -> Optional[UserAccount]:
        for a in self.user_accounts.values():
            if a.username == username:
                return a
        return None

    def all_user_accounts(self) -> List[UserAccount]:
        return list(self.user_accounts.values())

    # -- guardian ↔ junior links (#26) -------------------------------------
    def add_guardian_link(self, link: GuardianLink) -> GuardianLink:
        self.guardian_links[link.id] = link
        return link

    def save_guardian_link(self, link: GuardianLink) -> GuardianLink:
        self.guardian_links[link.id] = link
        return link

    def get_guardian_link(self, link_id: str) -> Optional[GuardianLink]:
        return self.guardian_links.get(link_id)

    def guardian_links_for(self, guardian_user_id: str) -> List[GuardianLink]:
        return [g for g in self.guardian_links.values()
                if g.guardian_user_id == guardian_user_id]

    def guardian_link_for(self, guardian_user_id: str,
                          player_id: str) -> Optional[GuardianLink]:
        for g in self.guardian_links.values():
            if g.guardian_user_id == guardian_user_id and g.player_id == player_id:
                return g
        return None

    def guardian_links_for_player(self, player_id: str) -> List[GuardianLink]:
        return [g for g in self.guardian_links.values() if g.player_id == player_id]

    def all_guardian_links(self) -> List[GuardianLink]:
        return list(self.guardian_links.values())

    # -- reschedule requests (#29) ------------------------------------------
    def add_reschedule_request(self, r: RescheduleRequest) -> RescheduleRequest:
        self.reschedule_requests[r.id] = r
        return r

    def save_reschedule_request(self, r: RescheduleRequest) -> RescheduleRequest:
        self.reschedule_requests[r.id] = r
        return r

    def get_reschedule_request(self, request_id: str) -> Optional[RescheduleRequest]:
        return self.reschedule_requests.get(request_id)

    def reschedule_requests_for_game(self, game_id: str) -> List[RescheduleRequest]:
        return [r for r in self.reschedule_requests.values() if r.game_id == game_id]

    def all_reschedule_requests(self) -> List[RescheduleRequest]:
        return list(self.reschedule_requests.values())

    # -- sessions (#74) ----------------------------------------------------
    def add_session(self, s: Session) -> Session:
        self.sessions[s.id] = s
        return s

    def save_session(self, s: Session) -> Session:
        self.sessions[s.id] = s
        return s

    def get_session(self, session_id: str) -> Optional[Session]:
        return self.sessions.get(session_id)

    def get_session_by_hash(self, token_hash: str) -> Optional[Session]:
        for s in self.sessions.values():
            if s.token_hash == token_hash:
                return s
        return None

    def sessions_for_user(self, user_id: str) -> List[Session]:
        return [s for s in self.sessions.values() if s.user_id == user_id]

    def delete_sessions_before(self, cutoff: datetime) -> int:
        """Delete finished sessions whose terminal time is before ``cutoff`` —
        revoked sessions by ``revoked_at``, otherwise expired ones by
        ``expires_at``. Active and recently-finished sessions are kept. Returns
        the number removed (#77)."""
        doomed = [sid for sid, s in self.sessions.items()
                  if (s.revoked_at is not None and s.revoked_at < cutoff)
                  or (s.revoked_at is None and s.expires_at < cutoff)]
        for sid in doomed:
            del self.sessions[sid]
        return len(doomed)

    def add_setup_audit(self, entry: SetupAuditLog) -> SetupAuditLog:
        self.setup_audit.append(entry)
        return entry

    # -- listings (interface shared with the SQL store) -------------------
    def all_leagues(self) -> List[League]:
        return list(self.leagues.values())

    def all_seasons(self) -> List[Season]:
        return list(self.seasons.values())

    def all_levels(self) -> List[Level]:
        return list(self.levels.values())

    def all_divisions(self) -> List[Division]:
        return list(self.divisions.values())

    def all_clubs(self) -> List[Club]:
        return list(self.clubs.values())

    def all_teams(self) -> List[Team]:
        return list(self.teams.values())

    def all_organizations(self) -> List[Organization]:
        return list(self.organizations.values())

    def all_venues(self) -> List[Venue]:
        return list(self.venues.values())

    def all_rinks(self) -> List[Rink]:
        return list(self.rinks.values())

    def all_ice_slots(self) -> List[IceSlot]:
        return list(self.ice_slots.values())

    def all_games(self) -> List[Game]:
        return list(self.games.values())

    def delete_game(self, game_id: str) -> None:
        self.games.pop(game_id, None)

    def all_setup_audit(self) -> List[SetupAuditLog]:
        return list(self.setup_audit)

    # -- saves (persist in-place mutations) -------------------------------
    # For the in-memory store these are effectively no-ops (the object is
    # already held by reference); they exist so the service layer can be
    # written backend-agnostically and the SQL store can persist updates.
    def save_game(self, game: Game) -> Game:
        self.games[game.id] = game
        return game

    def save_league(self, league: League) -> League:
        self.leagues[league.id] = league
        return league

    def save_venue(self, venue: Venue) -> Venue:
        self.venues[venue.id] = venue
        return venue

    def save_division(self, division: Division) -> Division:
        self.divisions[division.id] = division
        return division

    def save_organization(self, org: Organization) -> Organization:
        self.organizations[org.id] = org
        return org

    def save_season(self, season: Season) -> Season:
        self.seasons[season.id] = season
        return season

    def save_level(self, level: Level) -> Level:
        self.levels[level.id] = level
        return level

    def save_team(self, team: Team) -> Team:
        self.teams[team.id] = team
        return team

    def save_player(self, player: Player) -> Player:
        self.players[player.id] = player
        return player

    def save_ice_slot(self, slot: IceSlot) -> IceSlot:
        self.ice_slots[slot.id] = slot
        return slot

    def save_rink(self, rink: Rink) -> Rink:
        self.rinks[rink.id] = rink
        return rink

    def save_substitute(self, sub: SubstituteEnrollment) -> SubstituteEnrollment:
        self.substitutes[sub.id] = sub
        return sub

    def save_roster_entry(self, entry: GameRosterEntry) -> GameRosterEntry:
        self.roster_entries[entry.id] = entry
        return entry

    def save_availability(self, av: GameAvailability) -> GameAvailability:
        self.availability[av.id] = av
        return av
