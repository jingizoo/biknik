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
    ContactDestination,
    Division,
    Game,
    IceSlot,
    IceSlotStatus,
    IceSlotType,
    League,
    Level,
    GameResult,
    NotificationAudience,
    NotificationChannel,
    NotificationKind,
    Official,
    OfficialAssignment,
    OfficialAssignmentStatus,
    OfficialAvailability,
    OfficialAvailabilityStatus,
    OfficialRole,
    Player,
    ResultStatus,
    Position,
    RescheduleRequest,
    RescheduleStatus,
    Organization,
    Rink,
    Season,
    SeasonTeamRegistration,
    SetupAuditLog,
    Team,
    Venue,
    intervals_overlap,
)
from ..domain.errors import (
    DivisionMismatchError,
    InvalidTransitionError,
    NotEligibleError,
    NotFoundError,
    ScheduleConflictError,
    ValidationError,
)
from ..store import InMemoryStore
from .import_validator import validate_import, validate_official_availability
from .notifier import push as _push_notification


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _blank(value) -> bool:
    return value is None or not str(value).strip()


def _clean(value) -> str:
    return str(value).strip()


def _parse_iso_utc(value) -> Optional[datetime]:
    """Parse a timezone-aware ISO-8601 timestamp, else None.

    Duplicated locally rather than imported from ``import_validator`` (same
    precedent as ``_blank``/``_clean`` above) — by the time this is called
    from ``commit_officials_availability_import`` the value has already
    passed ``validate_official_availability``, so ``None`` here would
    indicate a bug elsewhere, not a real user-facing validation failure.
    """
    if _blank(value):
        return None
    try:
        parsed = datetime.fromisoformat(_clean(value))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


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

    def _matchup(self, game) -> str:
        def team_name(tid):
            t = self.store.get_team(tid) if tid else None
            return t.name if t else "TBD"
        return f"{team_name(game.home_team_id)} vs {team_name(game.away_team_id)}"

    def _notify(self, kind, audience, title, message, **links):
        return _push_notification(self.store, self.clock, kind, audience,
                                  title, message, **links)

    def _game_label(self, game) -> str:
        def name(tid):
            t = self.store.get_team(tid) if tid else None
            return t.name if t else "TBD"
        return f"{name(game.home_team_id)} vs {name(game.away_team_id)}"

    def _notify_game_change(self, game, kind, title, message,
                            include_public=False):
        """Fan a schedule-change notification out to the affected parties (#87):
        both teams (coach audience → team recipient), each actively-assigned
        official, and optionally the public feed. Delivery honors each
        recipient's channel preferences (#81)."""
        for tid in (game.home_team_id, game.away_team_id):
            if tid:
                self._notify(kind, NotificationAudience.COACH, title, message,
                             audience_ref=tid, game_id=game.id)
        for a in self.store.assignments_for_game(game.id):
            if a.status.is_active:
                self._notify(kind, NotificationAudience.OFFICIAL, title, message,
                             audience_ref=a.official_id, game_id=game.id)
        if include_public:
            self._notify(kind, NotificationAudience.PUBLIC, title, message,
                         game_id=game.id)

    # -- league / season / division ---------------------------------------
    @_transactional
    def create_league(self, name: str, country: str = "", timezone_name: str = "UTC",
                      organization_id: Optional[str] = None,
                      actor_id: Optional[str] = None) -> League:
        # Optional owning organization (#173) — validated when given so a league
        # never dangles off a non-existent owner. Nullable for migration/legacy;
        # null-owner leagues surface in the missing-assignment queue.
        if organization_id and self.store.get_organization(organization_id) is None:
            raise NotFoundError(f"Organization {organization_id} not found.")
        league = League(id=self.store.next_id("league"),
                        name=self._require_name(name), country=country,
                        timezone=timezone_name or "UTC",
                        organization_id=organization_id or None)
        self.store.add_league(league)
        self._audit("league_created", "league", league.id, actor_id,
                    {"organization_id": organization_id} if organization_id else None)
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
    def create_level(self, season_id: str, name: str, sort_order: int = 0,
                     actor_id: Optional[str] = None) -> Level:
        if self.store.get_season(season_id) is None:
            raise NotFoundError(f"Season {season_id} not found.")
        level = Level(id=self.store.next_id("level"), season_id=season_id,
                      name=self._require_name(name), sort_order=sort_order or 0)
        self.store.add_level(level)
        self._audit("level_created", "level", level.id, actor_id,
                    {"season_id": season_id})
        return level

    @_transactional
    def create_division(self, season_id: str, name: str, age_group: str = "",
                        level_id: Optional[str] = None,
                        actor_id: Optional[str] = None) -> Division:
        if self.store.get_season(season_id) is None:
            raise NotFoundError(f"Season {season_id} not found.")
        # An optional owning level/tier (#166) — validated when given: it must
        # exist AND belong to this division's season, so a division can never
        # sit under a level from a different season. Null is fine (unassigned).
        if level_id:
            level = self.store.get_level(level_id)
            if level is None:
                raise NotFoundError(f"Level {level_id} not found.")
            if level.season_id != season_id:
                raise ValidationError(
                    "Level belongs to a different season than the division.")
        division = Division(id=self.store.next_id("division"), season_id=season_id,
                            name=self._require_name(name), age_group=age_group,
                            level_id=level_id or None)
        self.store.add_division(division)
        self._audit("division_created", "division", division.id, actor_id,
                    {"season_id": season_id,
                     **({"level_id": level_id} if level_id else {})})
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
    def create_team(self, club_id: str, division_id: Optional[str] = None,
                    name: str = "", actor_id: Optional[str] = None,
                    league_id: Optional[str] = None) -> Team:
        """Create a permanent league team (#180).

        A team belongs permanently to a *league*, not a division — its
        season-specific division participation lives in SeasonTeamRegistration.
        Two ways to say which league:

        - Pass ``league_id`` directly (the #180-correct path: create the team
          under the league, register it into a season/division separately).
        - Pass a ``division_id`` (legacy/back-compat, still used by the CSV
          import and older callers): the league is derived from the division's
          season, and division_id is retained on the Team for now.

        Exactly one is required. When both are given, the division's league
        wins (and must not contradict a supplied league_id).
        """
        if self.store.get_club(club_id) is None:
            raise NotFoundError(f"Club {club_id} not found.")
        division = None
        if division_id:
            division = self.store.get_division(division_id)
            if division is None:
                raise NotFoundError(f"Division {division_id} not found.")
            season = self.store.get_season(division.season_id)
            derived_league = season.league_id if season else None
            if league_id and derived_league and league_id != derived_league:
                raise ValidationError(
                    "The chosen division belongs to a different league.")
            league_id = derived_league
        if not league_id:
            raise ValidationError(
                "A team needs a league (choose a league, or a division to "
                "derive it from).")
        if self.store.get_league(league_id) is None:
            raise NotFoundError(f"League {league_id} not found.")
        team = Team(id=self.store.next_id("team"), name=self._require_name(name),
                    division=division.name if division else "", club_id=club_id,
                    division_id=division_id or None, league_id=league_id)
        self.store.add_team(team)
        self._audit("team_created", "team", team.id, actor_id,
                    {"club_id": club_id, "division_id": division_id or None,
                     "league_id": league_id})
        return team

    # -- permanent teams + season registrations (#180) ---------------------
    # A team belongs permanently to its league; each season it plays in is a
    # SeasonTeamRegistration carrying that season's division. These overlay the
    # legacy Team.division_id additively — scheduling still reads division_id
    # until a later slice moves it onto registrations, so nothing here changes
    # existing game validation yet.
    @_transactional
    def register_team_for_season(self, season_id: str, team_id: str,
                                 division_id: Optional[str] = None,
                                 actor_id: Optional[str] = None
                                 ) -> SeasonTeamRegistration:
        season = self.store.get_season(season_id)
        if season is None:
            raise NotFoundError(f"Season {season_id} not found.")
        team = self.store.get_team(team_id)
        if team is None:
            raise NotFoundError(f"Team {team_id} not found.")
        # Rule 4 — league consistency: the team's permanent league must match
        # the season's league. Cross-league registration is rejected.
        if team.league_id and team.league_id != season.league_id:
            raise ValidationError(
                "Team belongs to a different league than this season.")
        if division_id:
            division = self.store.get_division(division_id)
            if division is None:
                raise NotFoundError(f"Division {division_id} not found.")
            # Rule 4 — a division from another season can't be used here.
            if division.season_id != season_id:
                raise ValidationError(
                    "Division belongs to a different season.")
        # Rule 5 — one registration per team per season. A prior *inactive*
        # registration (a team removed then re-added) is reactivated in place
        # rather than duplicated, honoring the (season_id, team_id) uniqueness.
        existing = self.store.registration_for_team_in_season(season_id, team_id)
        if existing is not None:
            if existing.active:
                raise ValidationError(
                    f"Team {team_id} is already registered for this season.")
            existing.active = True
            existing.division_id = division_id or None
            self.store.save_season_team_registration(existing)
            self._audit("season_team_registered", "season_team_registration",
                        existing.id, actor_id,
                        {"season_id": season_id, "team_id": team_id,
                         "division_id": existing.division_id, "reactivated": True})
            return existing
        reg = SeasonTeamRegistration(
            id=self.store.next_id("streg"), season_id=season_id, team_id=team_id,
            division_id=division_id or None, active=True)
        self.store.add_season_team_registration(reg)
        self._audit("season_team_registered", "season_team_registration",
                    reg.id, actor_id,
                    {"season_id": season_id, "team_id": team_id,
                     "division_id": reg.division_id})
        return reg

    def _games_scheduled_for_team_in_season(self, season_id, team_id):
        """Ids of committed (non-cancelled, non-draft) games in ``season_id``
        that ``team_id`` plays in — the games a removal or division change would
        strand. Draft proposals aren't real games yet, so they don't block."""
        return [g.id for g in self.store.all_games()
                if g.season_id == season_id and not g.cancelled
                and not g.is_draft
                and team_id in (g.home_team_id, g.away_team_id)]

    @_transactional
    def assign_season_team_division(self, registration_id: str,
                                    division_id: Optional[str] = None,
                                    actor_id: Optional[str] = None
                                    ) -> SeasonTeamRegistration:
        reg = self.store.get_season_team_registration(registration_id)
        if reg is None:
            raise NotFoundError(f"Registration {registration_id} not found.")
        if division_id:
            division = self.store.get_division(division_id)
            if division is None:
                raise NotFoundError(f"Division {division_id} not found.")
            # Rule 3/4 — a registration's division must be in its own season.
            if division.season_id != reg.season_id:
                raise ValidationError(
                    "Division belongs to a different season.")
        old = reg.division_id
        # Safety — a division change would leave already-scheduled games in the
        # old division mismatched against the team's participation. Refuse and
        # report the affected games so the operator can resolve them first,
        # rather than silently invalidating a published schedule.
        if (division_id or None) != (old or None):
            stranded = self._games_scheduled_for_team_in_season(
                reg.season_id, reg.team_id)
            if stranded:
                raise ValidationError(
                    "Cannot change this team's division while it has scheduled "
                    "games this season; resolve those games first.",
                    {"reason": "team_has_scheduled_games",
                     "affected_game_ids": stranded, "count": len(stranded)})
        reg.division_id = division_id or None
        self.store.save_season_team_registration(reg)
        self._audit("season_team_division_assigned", "season_team_registration",
                    reg.id, actor_id, {"from": old, "to": reg.division_id})
        return reg

    @_transactional
    def unregister_team_from_season(self, registration_id: str,
                                    actor_id: Optional[str] = None
                                    ) -> SeasonTeamRegistration:
        # Rule 6 — removing a team from a season deactivates only this season's
        # registration; the permanent Team and prior-season registrations are
        # untouched.
        reg = self.store.get_season_team_registration(registration_id)
        if reg is None:
            raise NotFoundError(f"Registration {registration_id} not found.")
        # Safety — refuse to strand a team that still has committed games this
        # season, returning the affected game ids so they can be resolved first.
        stranded = self._games_scheduled_for_team_in_season(
            reg.season_id, reg.team_id)
        if stranded:
            raise ValidationError(
                "Cannot remove this team from the season while it has scheduled "
                "games; resolve those games first.",
                {"reason": "team_has_scheduled_games",
                 "affected_game_ids": stranded, "count": len(stranded)})
        reg.active = False
        self.store.save_season_team_registration(reg)
        self._audit("season_team_unregistered", "season_team_registration",
                    reg.id, actor_id,
                    {"season_id": reg.season_id, "team_id": reg.team_id})
        return reg

    @_transactional
    def roll_forward_registrations(self, from_season_id: str, to_season_id: str,
                                   selections: Optional[list] = None,
                                   actor_id: Optional[str] = None) -> dict:
        """Copy participation from one season into another (#180 rollover).

        Creates a registration in ``to_season_id`` for each carried-forward team
        that plays in ``from_season_id``, reusing the permanent Team — Team
        records are never copied. ``selections`` is an optional list of
        ``{"team_id", "division_id"}`` (division_id in the *target* season); when
        omitted, every team active in the source season is carried with no
        division so the operator assigns them afterward. A team already active in
        the target season is skipped, not duplicated. Not ``@_transactional``-
        nested — it opens its own single transaction (via the decorator) and
        inlines the per-team writes rather than calling the transactional
        register method, since the store's transaction isn't reentrant.

        Every carried team is resolved and league-checked in a pre-write gate
        (#197): a source season's registration rows may be legacy, orphaned, or
        cross-league, and the store transaction is a lock rather than a
        rollback, so a missing or foreign team aborts the entire batch before
        any registration or audit is written.
        """
        src = self.store.get_season(from_season_id)
        if src is None:
            raise NotFoundError(f"Season {from_season_id} not found.")
        dst = self.store.get_season(to_season_id)
        if dst is None:
            raise NotFoundError(f"Season {to_season_id} not found.")
        if from_season_id == to_season_id:
            raise ValidationError("Source and target seasons must differ.")
        # Rule 4 — a rollover stays within one league.
        if (src.league_id or None) != (dst.league_id or None):
            raise ValidationError(
                "Cannot roll participation between seasons of different leagues.")
        source_active = {r.team_id
                         for r in self.store.registrations_for_season(from_season_id)
                         if r.active}
        if selections is not None:
            # Malformed HTTP input must surface as a structured validation
            # error, not an AttributeError 500 (#197): ``selections`` is a list
            # of ``{team_id, division_id}`` objects, each with a non-empty
            # team_id. ``catch`` only translates DomainError, so an attribute
            # access on a non-dict selection would otherwise escape as a 500.
            if not isinstance(selections, list):
                raise ValidationError(
                    "selections must be a list of {team_id, division_id} objects.")
            wanted = {}
            for sel in selections:
                if not isinstance(sel, dict):
                    raise ValidationError(
                        "Each selection must be an object with a team_id.")
                tid = sel.get("team_id")
                if _blank(tid):
                    raise ValidationError(
                        "Each selection needs a non-empty team_id.")
                if tid not in source_active:
                    raise ValidationError(
                        f"Team {tid} is not registered in the source season.")
                wanted[tid] = sel.get("division_id") or None
        else:
            wanted = {tid: None for tid in source_active}
        # Pre-write gate. The store's transaction is a lock, not a rollback
        # (see ``memory_store.transaction``), so every check that can fail must
        # run *before* the first write; any failure below aborts the whole
        # batch with zero registrations and zero audits.
        #
        # (a) Every carried team must be a real permanent Team whose permanent
        # league matches this rollover (#197). A source season's registration
        # rows can be legacy, orphaned, or cross-league, so ``source_active``
        # alone is not trustworthy — resolve each Team and verify its permanent
        # ``league_id`` before writing. This is what stops a rollover from
        # materializing a target registration for a missing or foreign team.
        # Deliberately stricter than ``register_team_for_season`` (which
        # tolerates a null team league): manual registration is an operator
        # vouching for one team, whereas rollover blindly trusts a batch of
        # source rows, so a null/other league_id here is treated as bad data.
        league_id = src.league_id
        for tid in wanted:
            team = self.store.get_team(tid)
            if team is None:
                raise ValidationError(
                    f"Team {tid} in the source season no longer exists; "
                    "it cannot be rolled forward.")
            if (team.league_id or None) != league_id:
                raise ValidationError(
                    f"Team {tid} belongs to a different league than this "
                    "rollover; it cannot be carried into this season.")
        # (b) Every target division must belong to the target season.
        for div_id in set(wanted.values()):
            if div_id is not None:
                division = self.store.get_division(div_id)
                if division is None:
                    raise NotFoundError(f"Division {div_id} not found.")
                if division.season_id != to_season_id:
                    raise ValidationError(
                        "A target division belongs to a different season.")

        rolled, skipped, created = 0, 0, []
        for tid, div_id in wanted.items():
            existing = self.store.registration_for_team_in_season(to_season_id, tid)
            if existing is not None and existing.active:
                skipped += 1
                continue
            if existing is not None:  # reactivate a prior inactive registration
                existing.active = True
                existing.division_id = div_id
                self.store.save_season_team_registration(existing)
                reg = existing
            else:
                reg = SeasonTeamRegistration(
                    id=self.store.next_id("streg"), season_id=to_season_id,
                    team_id=tid, division_id=div_id, active=True)
                self.store.add_season_team_registration(reg)
            self._audit("season_team_registered", "season_team_registration",
                        reg.id, actor_id,
                        {"season_id": to_season_id, "team_id": tid,
                         "division_id": div_id,
                         "rolled_forward_from": from_season_id})
            rolled += 1
            created.append(reg)
        self._audit("season_registrations_rolled_forward", "season",
                    to_season_id, actor_id,
                    {"from_season_id": from_season_id,
                     "rolled_forward": rolled, "skipped": skipped})
        return {"rolled_forward": rolled, "skipped": skipped,
                "registrations": created}

    # -- reassignment: move a record under a new parent (#166 PR D) --------
    # Each records the old→new parent id in the audit detail so a move is
    # traceable. Nullable links (venue→org, division→level, team→club) accept
    # None to unassign; required links (rink→venue, team→division,
    # player→team) demand a valid target so a move never orphans a record.
    @_transactional
    def assign_venue_organization(self, venue_id: str,
                                  organization_id: Optional[str] = None,
                                  actor_id: Optional[str] = None) -> Venue:
        venue = self.store.get_venue(venue_id)
        if venue is None:
            raise NotFoundError(f"Venue {venue_id} not found.")
        if organization_id and self.store.get_organization(organization_id) is None:
            raise NotFoundError(f"Organization {organization_id} not found.")
        old = venue.organization_id
        venue.organization_id = organization_id or None
        self.store.save_venue(venue)
        self._audit("venue_organization_assigned", "venue", venue.id, actor_id,
                    {"from": old, "to": venue.organization_id})
        return venue

    @_transactional
    def assign_rink_venue(self, rink_id: str, venue_id: str,
                          actor_id: Optional[str] = None) -> Rink:
        rink = self.store.get_rink(rink_id)
        if rink is None:
            raise NotFoundError(f"Rink {rink_id} not found.")
        if not venue_id or self.store.get_venue(venue_id) is None:
            raise NotFoundError(f"Venue {venue_id} not found.")
        old = rink.venue_id
        rink.venue_id = venue_id
        self.store.save_rink(rink)
        self._audit("rink_venue_assigned", "rink", rink.id, actor_id,
                    {"from": old, "to": venue_id})
        return rink

    @_transactional
    def assign_division_level(self, division_id: str,
                              level_id: Optional[str] = None,
                              actor_id: Optional[str] = None) -> Division:
        division = self.store.get_division(division_id)
        if division is None:
            raise NotFoundError(f"Division {division_id} not found.")
        if level_id:
            level = self.store.get_level(level_id)
            if level is None:
                raise NotFoundError(f"Level {level_id} not found.")
            if level.season_id != division.season_id:
                raise ValidationError(
                    "Level belongs to a different season than the division.")
        old = division.level_id
        division.level_id = level_id or None
        self.store.save_division(division)
        self._audit("division_level_assigned", "division", division.id, actor_id,
                    {"from": old, "to": division.level_id})
        return division

    @_transactional
    def assign_team_club(self, team_id: str, club_id: Optional[str] = None,
                         actor_id: Optional[str] = None) -> Team:
        team = self.store.get_team(team_id)
        if team is None:
            raise NotFoundError(f"Team {team_id} not found.")
        if club_id and self.store.get_club(club_id) is None:
            raise NotFoundError(f"Club {club_id} not found.")
        old = team.club_id
        team.club_id = club_id or None
        self.store.save_team(team)
        self._audit("team_club_assigned", "team", team.id, actor_id,
                    {"from": old, "to": team.club_id})
        return team

    @_transactional
    def assign_team_division(self, team_id: str, division_id: str,
                             actor_id: Optional[str] = None) -> Team:
        team = self.store.get_team(team_id)
        if team is None:
            raise NotFoundError(f"Team {team_id} not found.")
        division = self.store.get_division(division_id) if division_id else None
        if division is None:
            raise NotFoundError(f"Division {division_id} not found.")
        old = team.division_id
        team.division_id = division_id
        team.division = division.name  # keep the denormalized label in sync
        self.store.save_team(team)
        self._audit("team_division_assigned", "team", team.id, actor_id,
                    {"from": old, "to": division_id})
        return team

    @_transactional
    def assign_player_team(self, player_id: str, team_id: str,
                           actor_id: Optional[str] = None) -> Player:
        player = self.store.get_player(player_id)
        if player is None:
            raise NotFoundError(f"Player {player_id} not found.")
        if not team_id or self.store.get_team(team_id) is None:
            raise NotFoundError(f"Team {team_id} not found.")
        old = player.team_id
        player.team_id = team_id
        self.store.save_player(player)
        self._audit("player_team_assigned", "player", player.id, actor_id,
                    {"from": old, "to": team_id})
        return player

    # -- cross-domain reassignment: league owner + venue league (#173) -----
    @_transactional
    def assign_league_organization(self, league_id: str,
                                   organization_id: Optional[str] = None,
                                   actor_id: Optional[str] = None) -> League:
        league = self.store.get_league(league_id)
        if league is None:
            raise NotFoundError(f"League {league_id} not found.")
        if organization_id and self.store.get_organization(organization_id) is None:
            raise NotFoundError(f"Organization {organization_id} not found.")
        old = league.organization_id
        # Invariant #4: changing the owner must not silently cascade to the
        # league's venues (whose owner is derived from it). Refuse while venues
        # are still attached — the operator unassigns/reassigns them first, so
        # the ownership change is deliberate and each venue is re-audited.
        if (organization_id or None) != (old or None):
            attached = [v for v in self.store.all_venues() if v.league_id == league_id]
            if attached:
                raise ValidationError(
                    f"Cannot change this league's owner while {len(attached)} "
                    f"venue(s) are attached. Unassign them first.")
        league.organization_id = organization_id or None
        self.store.save_league(league)
        self._audit("league_organization_assigned", "league", league.id, actor_id,
                    {"from": old, "to": league.organization_id})
        return league

    @_transactional
    def assign_venue_league(self, venue_id: str, league_id: Optional[str] = None,
                            actor_id: Optional[str] = None) -> Venue:
        venue = self.store.get_venue(venue_id)
        if venue is None:
            raise NotFoundError(f"Venue {venue_id} not found.")
        old_league = venue.league_id
        old_org = venue.organization_id
        new_org = venue.organization_id
        if league_id:
            league = self.store.get_league(league_id)
            if league is None:
                raise NotFoundError(f"League {league_id} not found.")
            league_owner = league.organization_id
            # Owner agreement (#173 invariant 3): the venue and league must
            # share an owner. Derive the owner from the league when the venue
            # has none; reject a conflicting existing owner rather than silently
            # transferring facility ownership.
            if venue.organization_id and league_owner \
                    and venue.organization_id != league_owner:
                raise ValidationError(
                    "Venue owner must match the league's owner. Reassign the "
                    "venue's owner first, or pick a league with the same owner.")
            new_org = league_owner or venue.organization_id
        # Reassignment safety (#173): refuse if the move would strand non-
        # cancelled scheduled games on ice that no longer belongs to their
        # league — surface the offending game ids so the operator resolves them.
        stranded = self._games_stranded_by_venue_league(venue_id, league_id)
        if stranded:
            raise ValidationError(
                f"{len(stranded)} scheduled game(s) use this venue's ice and "
                f"belong to a different league. Move or cancel them first: "
                f"{', '.join(stranded[:10])}.")
        venue.league_id = league_id or None
        venue.organization_id = new_org or None
        self.store.save_venue(venue)
        detail = {"from": old_league, "to": venue.league_id}
        if (new_org or None) != (old_org or None):
            detail["organization_from"] = old_org
            detail["organization_to"] = venue.organization_id
        self._audit("venue_league_assigned", "venue", venue.id, actor_id, detail)
        return venue

    def _games_stranded_by_venue_league(self, venue_id, new_league_id):
        """Ids of non-cancelled games whose ice is on ``venue_id`` but whose
        own league (via Season) would not match the venue's new league — i.e.
        games the reassignment would leave on ice outside their league (#173).
        """
        rink_ids = {r.id for r in self.store.all_rinks() if r.venue_id == venue_id}
        if not rink_ids:
            return []
        stranded = []
        for g in self.store.all_games():
            if g.cancelled or not g.ice_slot_id:
                continue
            slot = self.store.get_ice_slot(g.ice_slot_id)
            if slot is None or slot.rink_id not in rink_ids:
                continue
            season = self.store.get_season(g.season_id) if g.season_id else None
            game_league = season.league_id if season else None
            if (game_league or None) != (new_league_id or None):
                stranded.append(g.id)
        return stranded

    # -- organization / venue / rink / ice slot ---------------------------
    @_transactional
    def create_organization(self, name: str, short_name: str = "",
                            actor_id: Optional[str] = None) -> Organization:
        org = Organization(id=self.store.next_id("org"),
                           name=self._require_name(name), short_name=short_name or "")
        self.store.add_organization(org)
        self._audit("organization_created", "organization", org.id, actor_id)
        return org

    @_transactional
    def create_venue(self, name: str, address: str = "", timezone_name: str = "UTC",
                     organization_id: Optional[str] = None,
                     league_id: Optional[str] = None,
                     actor_id: Optional[str] = None) -> Venue:
        # An optional owning organization (#166) — validated when given so a
        # venue never dangles off a non-existent org; null is fine (unassigned).
        if organization_id and self.store.get_organization(organization_id) is None:
            raise NotFoundError(f"Organization {organization_id} not found.")
        # An optional owning league (#173). A league carries its own owner, so
        # the venue's owner must agree with it: prefer deriving the owner from
        # the league, and reject an explicitly-supplied conflicting owner rather
        # than silently transferring facility ownership.
        if league_id:
            league = self.store.get_league(league_id)
            if league is None:
                raise NotFoundError(f"League {league_id} not found.")
            league_owner = league.organization_id
            if organization_id and league_owner and organization_id != league_owner:
                raise ValidationError(
                    "Venue owner must match the league's owner.")
            organization_id = league_owner or organization_id
        venue = Venue(id=self.store.next_id("venue"), name=self._require_name(name),
                      address=address, timezone=timezone_name or "UTC",
                      organization_id=organization_id or None, league_id=league_id or None)
        self.store.add_venue(venue)
        detail = {k: v for k, v in
                  {"organization_id": organization_id, "league_id": league_id}.items() if v}
        self._audit("venue_created", "venue", venue.id, actor_id, detail or None)
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
            overlaps = intervals_overlap(slot.start_time, slot.end_time,
                                         ex_slot.start_time, ex_slot.end_time)
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
        was_published = game.published
        game.published = published
        self.store.save_game(game)
        self._audit("game_published" if published else "game_unpublished",
                    "game", game_id, actor_id)
        # Notify affected parties + public only on the false→true transition,
        # so re-publishing an already-public game is a no-op (#87 idempotency).
        if published and not was_published:
            label = self._game_label(game)
            self._notify_game_change(
                game, NotificationKind.GAME_PUBLISHED, "Game published",
                f"{label} has been published.", include_public=True)
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
            overlaps = intervals_overlap(new_slot.start_time, new_slot.end_time,
                                         ex_slot.start_time, ex_slot.end_time)
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
        when = game.start_time.isoformat() if game.start_time else "a new time"
        self._notify_game_change(
            game, NotificationKind.GAME_MOVED, "Game moved",
            f"{self._game_label(game)} moved to {game.rink or 'a new rink'} "
            f"({when}).{(' ' + reason) if reason else ''}")
        return game

    # -- reschedule request / approval workflow (#29) -----------------------
    # A controlled, coach-initiated way to move a PUBLISHED game — distinct
    # from move_game above (an operator-only drag/drop). The opponent team
    # must accept before it ever reaches league review; an approval reuses
    # move_game/publish_game verbatim for the actual slot swap, so it
    # inherits the same one-game-per-slot / team-overlap guarantees rather
    # than re-implementing them.
    _OPEN_RESCHEDULE_STATUSES = {
        RescheduleStatus.PENDING_OPPONENT, RescheduleStatus.PENDING_LEAGUE_APPROVAL}

    @_transactional
    def request_reschedule(self, game_id: str, team_id: str, reason: str,
                           actor_id: Optional[str] = None) -> RescheduleRequest:
        game = self.store.get_game(game_id)
        if game is None:
            raise NotFoundError(f"Game {game_id} not found.")
        if game.cancelled:
            raise ValidationError("Cannot request a reschedule for a cancelled game.")
        if not game.published:
            raise ValidationError(
                "Only a published game can have a reschedule requested.")
        if team_id not in (game.home_team_id, game.away_team_id):
            raise ValidationError(
                "Only the game's own two teams may request a reschedule.")
        reason = self._require_name(reason, "reason")
        if any(r.status in self._OPEN_RESCHEDULE_STATUSES
              for r in self.store.reschedule_requests_for_game(game_id)):
            raise ScheduleConflictError(
                "This game already has an open reschedule request.",
                details={"reason": "reschedule_already_open"})
        req = RescheduleRequest(
            id=self.store.next_id("reschedule"), game_id=game_id,
            requested_by_team_id=team_id, reason=reason,
            status=RescheduleStatus.PENDING_OPPONENT, created_at=self.clock())
        self.store.add_reschedule_request(req)
        self._audit("reschedule_requested", "game", game_id, actor_id, {
            "request_id": req.id, "requested_by_team_id": team_id, "reason": reason})
        opponent_id = (game.away_team_id if team_id == game.home_team_id
                      else game.home_team_id)
        if opponent_id:
            self._notify(
                NotificationKind.RESCHEDULE_REQUESTED, NotificationAudience.COACH,
                "Reschedule requested",
                f"{self._matchup(game)}: a reschedule was requested ({reason}). "
                f"Please respond.", audience_ref=opponent_id, game_id=game_id)
        return req

    @_transactional
    def respond_to_reschedule(self, request_id: str, accept: bool,
                              actor_id: Optional[str] = None) -> RescheduleRequest:
        req = self.store.get_reschedule_request(request_id)
        if req is None:
            raise NotFoundError(f"Reschedule request {request_id} not found.")
        if req.status != RescheduleStatus.PENDING_OPPONENT:
            raise InvalidTransitionError(
                "This reschedule request is not awaiting an opponent response.",
                details={"status": req.status.value})
        game = self.store.get_game(req.game_id)
        req.status = (RescheduleStatus.PENDING_LEAGUE_APPROVAL if accept
                     else RescheduleStatus.OPPONENT_REJECTED)
        req.opponent_responded_at = self.clock()
        self.store.save_reschedule_request(req)
        self._audit("reschedule_accepted" if accept else "reschedule_rejected",
                   "game", req.game_id, actor_id, {"request_id": req.id})
        if accept:
            self._notify(
                NotificationKind.RESCHEDULE_ACCEPTED, NotificationAudience.SCHEDULER,
                "Reschedule awaiting approval",
                f"{self._matchup(game)}: the opponent accepted a reschedule "
                f"request ({req.reason}). League approval needed.",
                game_id=req.game_id)
        else:
            self._notify(
                NotificationKind.RESCHEDULE_REJECTED, NotificationAudience.COACH,
                "Reschedule rejected",
                f"{self._matchup(game)}: the opponent rejected the reschedule "
                f"request ({req.reason}).",
                audience_ref=req.requested_by_team_id, game_id=req.game_id)
        return req

    def decide_reschedule(self, request_id: str, approve: bool,
                          new_ice_slot_id: Optional[str] = None,
                          note: Optional[str] = None,
                          actor_id: Optional[str] = None) -> RescheduleRequest:
        """NOT @_transactional: the approve path calls move_game/publish_game,
        which already are — nested transaction() calls are not reentrant on
        SqlStore (matches copy_previous_roster's call to select_roster)."""
        req = self.store.get_reschedule_request(request_id)
        if req is None:
            raise NotFoundError(f"Reschedule request {request_id} not found.")
        if req.status != RescheduleStatus.PENDING_LEAGUE_APPROVAL:
            raise InvalidTransitionError(
                "This reschedule request is not awaiting league approval.",
                details={"status": req.status.value})
        if not approve:
            req.status = RescheduleStatus.DENIED
            req.decision_note = note
            req.league_decided_at = self.clock()
            self.store.save_reschedule_request(req)
            self._audit("reschedule_denied", "game", req.game_id, actor_id,
                       {"request_id": req.id, "note": note})
            game = self.store.get_game(req.game_id)
            self._notify_game_change(
                game, NotificationKind.RESCHEDULE_DENIED, "Reschedule denied",
                f"{self._matchup(game)}: the reschedule request was denied."
                f"{(' ' + note) if note else ''}")
            return req
        if not new_ice_slot_id:
            raise ValidationError(
                "A replacement ice slot is required to approve a reschedule.")
        # This workflow's whole point is that by this step both the opponent
        # and league have signed off, so — unlike a raw move_game drag/drop,
        # which conservatively unpublishes pending human review — republish
        # immediately rather than leaving it in limbo.
        #
        # move_game/publish_game are each their own committed transaction (not
        # one atomic unit with the RescheduleRequest save below), so a crash
        # between them could otherwise leave the game moved-but-unpublished
        # with this request stuck PENDING_LEAGUE_APPROVAL forever — retrying
        # with the same slot would hit move_game's own "already in that slot"
        # guard before ever reaching the request update. Skip a step that's
        # already done instead of re-running it, so re-approving (the natural
        # recovery action) always completes the request rather than erroring.
        game = self.store.get_game(req.game_id)
        if game is None or game.ice_slot_id != new_ice_slot_id:
            self.move_game(req.game_id, new_ice_slot_id,
                           reason=f"Reschedule approved: {req.reason}",
                           actor_id=actor_id)
            game = self.store.get_game(req.game_id)
        if not game.published:
            self.publish_game(req.game_id, actor_id=actor_id)
        req.status = RescheduleStatus.REPUBLISHED
        req.new_ice_slot_id = new_ice_slot_id
        req.decision_note = note
        req.league_decided_at = self.clock()
        self.store.save_reschedule_request(req)
        self._audit("reschedule_republished", "game", req.game_id, actor_id,
                   {"request_id": req.id, "new_ice_slot_id": new_ice_slot_id})
        return req

    def list_reschedule_requests(self, game_id: Optional[str] = None
                                 ) -> List[RescheduleRequest]:
        """Pure read helper — must NOT be @_transactional."""
        rows = (self.store.reschedule_requests_for_game(game_id) if game_id
               else self.store.all_reschedule_requests())
        return sorted(rows, key=lambda r: r.created_at, reverse=True)

    # -- convenience: add a player to a team ------------------------------
    @_transactional
    def add_player(self, team_id: str, name: str, position: Position,
                   jersey_number: Optional[int] = None,
                   email: Optional[str] = None,
                   is_active: bool = True,
                   actor_id: Optional[str] = None) -> Player:
        """Manually create one Player (#114) — the same model/store the CSV
        import path writes, so a league admin isn't forced through Import for
        a single new arrival. Validation mirrors import_validator's row
        checks (jersey_number > 0, an ``@`` with a ``.`` after it in email)
        so a manual create can't slip in data the bulk path would reject."""
        if self.store.get_team(team_id) is None:
            raise NotFoundError(f"Team {team_id} not found.")
        if jersey_number is not None and (
                not isinstance(jersey_number, int) or jersey_number <= 0):
            raise ValidationError("jersey_number must be a positive number.")
        if email:
            at = email.find("@")
            if at <= 0 or "." not in email[at + 1:]:
                raise ValidationError(f"Invalid email {email}.")
        player = Player(id=self.store.next_id("player"), team_id=team_id,
                        name=self._require_name(name), position=position,
                        jersey_number=jersey_number, is_active=is_active)
        self.store.add_player(player)
        self._audit("player_added", "player", player.id, actor_id,
                    {"team_id": team_id})
        if email:
            self.store.add_contact_destination(ContactDestination(
                id=self.store.next_id("contact"),
                recipient_ref=f"player:{player.id}",
                channel=NotificationChannel.EMAIL,
                destination=email))
        return player

    # -- CSV import commit (#93) --------------------------------------------
    def commit_teams_players_import(self, season_id: str, sheets: dict,
                                    actor_id: Optional[str] = None) -> dict:
        """Commit step 2 of the pilot onboarding import wizard: teams+players.

        ``sheets`` is ALREADY-PARSED row dicts (``{"teams": [...],
        "players": [...]}``) — CSV-text parsing stays at the API facade layer
        (see ``ApiService.commit_teams_players_import``), matching #92's
        layering. ``validate_import`` (the SAME pure gate #92 added) is reused
        unchanged as the pre-commit check: if it reports any errors, nothing
        is written at all, not even otherwise-valid rows in the other sheet.

        Teams and players are matched across repeat uploads by ``external_ref``
        (the sheet's ``team_code``/``player_code``), so re-importing the same
        payload updates existing rows in place instead of duplicating them.
        This match is deliberately GLOBAL, not scoped to ``season_id``: a
        ``team_code`` reused in a different season's import will move the
        existing team into the new season rather than create a second one.
        Acceptable for v1 if codes are treated as globally stable identifiers;
        revisit if/when multi-season imports become a real workflow (#93
        review).

        Clubs and divisions have no external code in this slice — they are
        found-or-created by an exact (``.strip()``ped) name match, division
        scoped to ``season_id`` since the same division name can recur across
        seasons. This is a deliberately simple v1: no fuzzy matching, no
        dedup across near-identical names beyond whitespace-trimming.

        Not ``@_transactional``: this method must call no other
        ``@_transactional`` service method (``create_club``/``create_team``/
        ``add_player`` all open their own transaction), or a nested
        ``with store.transaction():`` raises ``sqlite3.OperationalError:
        cannot start a transaction within a transaction`` on the SQL backend —
        exactly the decorator/transaction-boundary bug that broke #87 and
        #88. Instead this opens exactly one transaction itself below and
        duplicates the small amount of create-logic it needs via raw store
        calls + its own ``self._audit(...)`` calls.
        """
        if self.store.get_season(season_id) is None:
            raise NotFoundError(f"Season {season_id} not found.")

        result = validate_import(sheets)
        if not result["ok"]:
            return {"committed": False, "summary": result["summary"],
                    "errors": result["errors"], "warnings": result["warnings"]}

        team_rows = list(sheets.get("teams") or [])
        player_rows = list(sheets.get("players") or [])

        # #92's validator never checks that `position` is a recognized value
        # (it doesn't require the column at all). Validate every row's
        # position BEFORE any writes, so a bad value on a later row can't
        # leave earlier rows (or the teams sheet) partially committed —
        # the same all-or-nothing guarantee validate_import already gives us.
        for row in player_rows:
            value = row.get("position")
            if _blank(value):
                continue
            try:
                Position(_clean(value))
            except ValueError:
                raise ValidationError(
                    f"Unknown position '{value}' for player_code "
                    f"{row.get('player_code')}.")

        counts = {"teams_created": 0, "teams_updated": 0,
                  "players_created": 0, "players_updated": 0,
                  "clubs_created": 0, "divisions_created": 0}
        # Generated up front (not after the row loops) so every row-level
        # audit entry below can be tagged with it, letting the Activity feed
        # group a batch's individual creates/updates under its summary (#102).
        batch_id = self.store.next_id("importbatch")

        with self.store.transaction():
            team_code_to_id = {}
            for row in team_rows:
                team_code = _clean(row.get("team_code"))
                team_name = _clean(row.get("team_name"))

                club_id = None
                club_name_raw = row.get("club_name")
                if not _blank(club_name_raw):
                    club_name = _clean(club_name_raw)
                    club = next((c for c in self.store.all_clubs()
                                if c.name == club_name), None)
                    if club is None:
                        club = Club(id=self.store.next_id("club"), name=club_name)
                        self.store.add_club(club)
                        self._audit("club_created", "club", club.id, actor_id,
                                    {"import_batch_id": batch_id})
                        counts["clubs_created"] += 1
                    club_id = club.id

                division = None
                division_name_raw = row.get("division_name")
                if not _blank(division_name_raw):
                    division_name = _clean(division_name_raw)
                    division = next(
                        (d for d in self.store.all_divisions()
                         if d.season_id == season_id and d.name == division_name),
                        None)
                    if division is None:
                        division = Division(id=self.store.next_id("division"),
                                            season_id=season_id, name=division_name)
                        self.store.add_division(division)
                        self._audit("division_created", "division", division.id,
                                    actor_id, {"season_id": season_id,
                                              "import_batch_id": batch_id})
                        counts["divisions_created"] += 1

                division_id = division.id if division else None
                division_name_str = division.name if division else ""

                team = next((t for t in self.store.all_teams()
                            if t.external_ref == team_code), None)
                if team is not None:
                    team.name = team_name
                    team.club_id = club_id
                    team.division_id = division_id
                    team.division = division_name_str
                    self.store.save_team(team)
                    self._audit("team_updated", "team", team.id, actor_id,
                                {"club_id": club_id, "division_id": division_id,
                                 "import_batch_id": batch_id})
                    counts["teams_updated"] += 1
                else:
                    team = Team(id=self.store.next_id("team"), name=team_name,
                               division=division_name_str, club_id=club_id,
                               division_id=division_id, external_ref=team_code)
                    self.store.add_team(team)
                    self._audit("team_created", "team", team.id, actor_id,
                                {"club_id": club_id, "division_id": division_id,
                                 "import_batch_id": batch_id})
                    counts["teams_created"] += 1
                team_code_to_id[team_code] = team.id

            for row in player_rows:
                player_code = _clean(row.get("player_code"))
                full_name = (f"{_clean(row.get('first_name'))} "
                            f"{_clean(row.get('last_name'))}").strip()
                # validate_import already guarantees this team_code matches a
                # row in THIS SAME upload's teams sheet; .get() is just a
                # defensive belt-and-suspenders check against a bug elsewhere.
                team_id = team_code_to_id.get(_clean(row.get("team_code")))
                if team_id is None:
                    raise ValidationError(
                        f"Unknown team_code for player_code {player_code}.")

                jersey_raw = row.get("jersey_number")
                jersey_number = (int(_clean(jersey_raw)) if not _blank(jersey_raw)
                                else None)
                position_raw = row.get("position")
                position = (Position(_clean(position_raw))
                           if not _blank(position_raw) else None)
                email_raw = row.get("email")
                email = _clean(email_raw) if not _blank(email_raw) else None

                player = next((p for p in self.store.all_players()
                              if p.external_ref == player_code), None)
                if player is not None:
                    # Partial-field-overwrite: only fields the sheet actually
                    # supplies this time are updated.
                    player.name = full_name
                    player.team_id = team_id
                    if jersey_number is not None:
                        player.jersey_number = jersey_number
                    if position is not None:
                        player.position = position
                    self.store.save_player(player)
                    self._audit("player_updated", "player", player.id, actor_id,
                                {"team_id": team_id, "import_batch_id": batch_id})
                    counts["players_updated"] += 1
                else:
                    # The domain model requires a Position with no default,
                    # but #92 doesn't require the CSV to supply one — default
                    # a brand-new player to FORWARD as an explicit judgment
                    # call; may want revisiting.
                    player = Player(id=self.store.next_id("player"), team_id=team_id,
                                    name=full_name,
                                    position=position or Position.FORWARD,
                                    jersey_number=jersey_number,
                                    external_ref=player_code)
                    self.store.add_player(player)
                    self._audit("player_added", "player", player.id, actor_id,
                                {"team_id": team_id, "import_batch_id": batch_id})
                    counts["players_created"] += 1

                if email is not None:
                    recipient_ref = f"player:{player.id}"
                    existing = self.store.get_contact_destination(
                        recipient_ref, NotificationChannel.EMAIL)
                    if existing is not None:
                        existing.destination = email
                        self.store.save_contact_destination(existing)
                    else:
                        self.store.add_contact_destination(ContactDestination(
                            id=self.store.next_id("contact"),
                            recipient_ref=recipient_ref,
                            channel=NotificationChannel.EMAIL,
                            destination=email))

            # skipped/errors are always 0 here by construction: the
            # all-or-nothing gate above means the only way to reach this line
            # is a fully clean validate_import result — any error blocks the
            # transaction before an audit row is ever written. Present anyway
            # for a stable import_committed detail shape across #93-#95.
            self._audit("import_committed", "import_batch", batch_id, actor_id,
                        {"import_type": "teams_players", "season_id": season_id,
                         "skipped": 0, "errors": 0, **counts})

        return {
            "committed": True,
            "summary": {
                "teams": {"created": counts["teams_created"],
                         "updated": counts["teams_updated"]},
                "players": {"created": counts["players_created"],
                           "updated": counts["players_updated"]},
                "clubs_created": counts["clubs_created"],
                "divisions_created": counts["divisions_created"],
            },
            "warnings": result["warnings"],
        }

    # -- CSV import commit: officials + availability (#94) ------------------
    def commit_officials_availability_import(self, sheets: dict,
                                             actor_id: Optional[str] = None
                                             ) -> dict:
        """Commit step 3 of the pilot onboarding import wizard: officials +
        their availability windows.

        ``sheets`` is ALREADY-PARSED row dicts (``{"officials": [...],
        "official_availability": [...]}``) — CSV-text parsing stays at the
        API facade layer, matching #93's layering. No ``season_id`` param:
        officials aren't season-scoped.

        Validation happens FIRST, before any writes, all-or-nothing across
        BOTH sheets: a bad ``official_availability`` row blocks an otherwise-
        clean ``officials`` sheet, exactly like #93's cross-sheet guarantee.
        The officials sheet reuses #92's existing (unmodified) checks via
        ``validate_import({"officials": ...})`` — every other key is treated
        as empty by that function. The availability sheet is checked by the
        NEW sibling function ``validate_official_availability``, which
        (deliberately, see its own docstring) resolves ``official_code``
        against either this upload's officials sheet OR an official already
        persisted from a prior commit.

        Not ``@_transactional``, for the exact reason documented at length on
        ``commit_teams_players_import`` above: this method must call no other
        ``@_transactional`` service method (``create_official`` opens its own
        transaction), or a nested ``with store.transaction():`` raises
        ``sqlite3.OperationalError: cannot start a transaction within a
        transaction`` on the SQL backend — the #87/#88 bug class. It opens
        exactly one transaction itself below, duplicating the small amount of
        create/update logic it needs via raw store calls plus its own
        ``self._audit(...)`` calls. ``self.set_official_availability`` is NOT
        ``@_transactional`` (see its docstring), so it is safe to call
        directly from inside this method's single transaction for the
        create-a-new-availability-window path.
        """
        officials_rows = list(sheets.get("officials") or [])
        availability_rows = list(sheets.get("official_availability") or [])

        officials_result = validate_import({"officials": officials_rows})
        if officials_result["errors"]:
            return {"committed": False, "summary": officials_result["summary"],
                    "errors": officials_result["errors"],
                    "warnings": officials_result["warnings"]}

        official_codes_in_sheet = {
            _clean(row.get("official_code")) for row in officials_rows
            if not _blank(row.get("official_code"))
        }
        existing_external_refs = {
            o.external_ref for o in self.store.all_officials() if o.external_ref
        }
        avail_result = validate_official_availability(
            availability_rows, official_codes_in_sheet, existing_external_refs)
        if avail_result["errors"]:
            return {
                "committed": False,
                "summary": {"officials": len(officials_rows),
                           "official_availability": len(availability_rows)},
                "errors": officials_result["errors"] + avail_result["errors"],
                "warnings": officials_result["warnings"] + avail_result["warnings"],
            }

        counts = {"officials_created": 0, "officials_updated": 0,
                  "availability_created": 0, "availability_updated": 0,
                  "clubs_created": 0}
        # See commit_teams_players_import's identical note: generated up
        # front so every row-level audit entry can be tagged with it (#102).
        batch_id = self.store.next_id("importbatch")

        with self.store.transaction():
            official_code_to_id = {}
            for row in officials_rows:
                official_code = _clean(row.get("official_code"))
                name = _clean(row.get("name"))
                email_raw = row.get("email")
                email = _clean(email_raw) if not _blank(email_raw) else None

                club_id = None
                club_name_raw = row.get("home_club_name")
                if not _blank(club_name_raw):
                    club_name = _clean(club_name_raw)
                    club = next((c for c in self.store.all_clubs()
                                if c.name == club_name), None)
                    if club is None:
                        club = Club(id=self.store.next_id("club"), name=club_name)
                        self.store.add_club(club)
                        self._audit("club_created", "club", club.id, actor_id,
                                    {"import_batch_id": batch_id})
                        counts["clubs_created"] += 1
                    club_id = club.id

                official = next((o for o in self.store.all_officials()
                                 if o.external_ref == official_code), None)
                if official is not None:
                    official.name = name
                    official.home_club_id = club_id
                    self.store.save_official(official)
                    self._audit("official_updated", "official", official.id,
                                actor_id, {"home_club_id": club_id,
                                          "import_batch_id": batch_id})
                    counts["officials_updated"] += 1
                else:
                    official = Official(id=self.store.next_id("official"),
                                        name=name, home_club_id=club_id,
                                        external_ref=official_code)
                    self.store.add_official(official)
                    self._audit("official_created", "official", official.id,
                                actor_id, {"import_batch_id": batch_id})
                    counts["officials_created"] += 1
                official_code_to_id[official_code] = official.id

                if email is not None:
                    recipient_ref = f"official:{official.id}"
                    existing = self.store.get_contact_destination(
                        recipient_ref, NotificationChannel.EMAIL)
                    if existing is not None:
                        existing.destination = email
                        self.store.save_contact_destination(existing)
                    else:
                        self.store.add_contact_destination(ContactDestination(
                            id=self.store.next_id("contact"),
                            recipient_ref=recipient_ref,
                            channel=NotificationChannel.EMAIL,
                            destination=email))

            for row in availability_rows:
                official_code = _clean(row.get("official_code"))
                official_id = official_code_to_id.get(official_code)
                if official_id is None:
                    # Not in this upload's officials sheet — validation only
                    # let this through because official_code matched an
                    # existing_external_ref, i.e. an official created by a
                    # PRIOR commit (#94's key new capability over #93).
                    existing = next(
                        (o for o in self.store.all_officials()
                         if o.external_ref == official_code), None)
                    official_id = existing.id if existing else None
                if official_id is None:
                    raise ValidationError(
                        f"Unknown official_code {official_code} for "
                        f"official_availability row.")

                start = _parse_iso_utc(row.get("start_time"))
                end = _parse_iso_utc(row.get("end_time"))
                status_raw = _clean(row.get("status"))
                note_raw = row.get("note")
                note = _clean(note_raw) if not _blank(note_raw) else None

                existing_window = next(
                    (a for a in self.store.availability_for_official(official_id)
                     if a.start_time == start and a.end_time == end), None)
                if existing_window is not None:
                    existing_window.status = OfficialAvailabilityStatus(status_raw)
                    existing_window.note = note
                    self.store.save_official_availability(existing_window)
                    self._audit("official_availability_updated",
                                "official_availability", existing_window.id,
                                actor_id, {"official_id": official_id,
                                          "status": status_raw,
                                          "import_batch_id": batch_id})
                    counts["availability_updated"] += 1
                else:
                    self.set_official_availability(
                        official_id, start, end, status_raw, note=note,
                        actor_id=actor_id,
                        extra_detail={"import_batch_id": batch_id})
                    counts["availability_created"] += 1

            # skipped/errors are always 0 here by construction — see the
            # identical note on commit_teams_players_import's import_committed
            # audit row above.
            self._audit("import_committed", "import_batch", batch_id, actor_id,
                        {"import_type": "officials_availability",
                         "officials_created": counts["officials_created"],
                         "officials_updated": counts["officials_updated"],
                         "availability_created": counts["availability_created"],
                         "availability_updated": counts["availability_updated"],
                         "clubs_created": counts["clubs_created"],
                         "skipped": 0, "errors": 0})

        return {
            "committed": True,
            "summary": {
                "officials": {"created": counts["officials_created"],
                             "updated": counts["officials_updated"]},
                "official_availability": {
                    "created": counts["availability_created"],
                    "updated": counts["availability_updated"]},
                "clubs_created": counts["clubs_created"],
            },
            "warnings": officials_result["warnings"] + avail_result["warnings"],
        }

    # -- CSV import commit: rinks + ice slots (#95) --------------------------
    def commit_rinks_ice_slots_import(self, sheets: dict,
                                      actor_id: Optional[str] = None) -> dict:
        """Commit step 4 of the pilot onboarding import wizard: rinks + their
        ice slots.

        ``sheets`` is ALREADY-PARSED row dicts (``{"rinks": [...],
        "ice_slots": [...]}``) — CSV-text parsing stays at the API facade
        layer, matching #93/#94's layering. No ``season_id`` param: rinks
        aren't season-scoped (same as #94's officials).

        Unlike #94, no new sibling validator is needed here: ``rinks`` and
        ``ice_slots`` are already first-class ``IMPORT_SHEET_NAMES`` members
        that #92's ``validate_import`` fully validates (required fields,
        ``rink_code`` uniqueness, the ``ice_slots`` sheet's ``rink_code``
        cross-reference, ISO-8601 UTC parsing, the ``slot_type`` enum, and
        same-rink overlap warnings) — reused completely unchanged via a
        single ``validate_import(sheets)`` call. That cross-reference is
        deliberately SHEET-INTERNAL-ONLY, like #93's ``team_code`` (NOT
        widened to persisted ``external_ref``s the way #94's
        ``official_code`` was): a real pilot workflow sends ``rinks.csv`` and
        ``ice_slots.csv`` together in one upload, not rinks now and
        slots-for-those-rinks in a separate later commit. ``slot_type`` is
        already validated against :class:`IceSlotType` by ``validate_import``
        itself, so no extra gate is needed for its FORMAT — but see the
        allocated-slot pre-write gate below, which is about its INTERACTION
        with an already-booked game, not its format.

        Venues are found-or-created by an exact (``.strip()``ped) name
        match, the same simple v1 as #93/#94's club matching — never
        updated in place even if a later row's ``address`` differs for the
        same name. Rinks are matched across repeat uploads by
        ``external_ref`` (the sheet's ``rink_code``), and ``rink_name`` is a
        PARTIAL-FIELD-OVERWRITE on that update path (mirroring
        ``commit_teams_players_import``'s player name/jersey_number/position
        handling): since ``validate_import`` doesn't require ``rink_name``
        (only ``venue_name``/``rink_code`` are required for the ``rinks``
        sheet), a repeat row that omits it must leave the existing rink's
        name alone rather than clobbering it back to the row's
        ``rink_code`` — only a brand-new rink defaults its name to the code.
        Ice slots have no code of their own and are matched by the natural
        ``(rink_id, start_time, end_time)`` tuple, mirroring #94's
        availability-window matching.

        Not ``@_transactional``, for the same reason documented at length on
        ``commit_teams_players_import``/``commit_officials_availability_import``
        above — but note the DIFFERENT consequence here: ``create_rink`` AND
        ``create_ice_slot`` are BOTH ``@_transactional`` (unlike
        ``set_official_availability``, which #94 could call directly), so
        this method must call neither — it duplicates their create logic via
        raw store calls plus its own ``self._audit(...)`` calls, all inside
        the single transaction it opens itself.

        Correctness note: an ice slot's ``status`` also transitions to
        ``ALLOCATED`` outside this import, when a game is scheduled onto it
        (``create_game``/``move_game``). A repeat import that always
        re-derived ``status`` from ``slot_type`` — the way ``create_ice_slot``
        does on first creation — would silently downgrade an already-booked
        slot back to ``AVAILABLE``/``BLOCKED`` while the game record still
        points at it. The update-in-place path below therefore leaves
        ``status`` untouched whenever ``store.game_using_ice_slot`` reports a
        game already claims the slot.

        That guard alone isn't enough, though: ``create_game`` also requires
        a booked slot's ``slot_type`` to stay ``GAME`` (it refuses to host a
        game on ``practice``/``maintenance``/etc ice), so silently changing
        ``slot_type`` out from under an allocated slot would leave the game
        record pointing at ice that's no longer game-bookable, even with
        ``status`` correctly preserved. A pre-write gate below therefore
        rejects the ENTIRE commit — before any writes, same all-or-nothing
        guarantee as everything else here — if an incoming row would change
        the ``slot_type`` of a slot a game already uses.

        Also unlike ``create_ice_slot``'s single-entity route, this does NOT
        hard-block on overlapping ice times via ``ScheduleConflictError`` —
        ``validate_import``'s overlap check is a WARNING only (mirroring
        #94's own availability-overlap warning), so newly-created slots here
        are allowed to overlap; the warning surfaces in the response instead
        of aborting the whole commit.
        """
        result = validate_import(sheets)
        if not result["ok"]:
            return {"committed": False, "summary": result["summary"],
                    "errors": result["errors"], "warnings": result["warnings"]}

        rink_rows = list(sheets.get("rinks") or [])
        slot_rows = list(sheets.get("ice_slots") or [])

        # Pre-write gate (mirrors #93's Position check): create_game requires
        # a booked slot's slot_type to stay GAME (it refuses to schedule onto
        # practice/maintenance/etc ice). Silently changing slot_type on a
        # slot a game already uses would leave that game pointing at ice
        # that's no longer game-bookable, even though the status-preserving
        # guard below correctly leaves `status` alone (review fix). Block the
        # WHOLE commit before any writes if any row would do this — the same
        # all-or-nothing guarantee as everything else here.
        for row in slot_rows:
            rink_code = _clean(row.get("rink_code"))
            existing_rink = next((r for r in self.store.all_rinks()
                                 if r.external_ref == rink_code), None)
            if existing_rink is None:
                continue  # a brand-new rink can't yet have a booked slot
            start = _parse_iso_utc(row.get("start_time"))
            end = _parse_iso_utc(row.get("end_time"))
            new_slot_type = IceSlotType(_clean(row.get("slot_type")))
            existing_slot = next(
                (s for s in self.store.all_ice_slots()
                 if s.rink_id == existing_rink.id and s.start_time == start
                 and s.end_time == end), None)
            if existing_slot is None:
                continue
            if (existing_slot.slot_type != new_slot_type
                    and self.store.game_using_ice_slot(existing_slot.id) is not None):
                raise ValidationError(
                    f"Ice slot {existing_slot.id} on rink_code {rink_code} "
                    f"has a game scheduled on it; slot_type cannot change "
                    f"from {existing_slot.slot_type.value} to "
                    f"{new_slot_type.value}.")

        counts = {"rinks_created": 0, "rinks_updated": 0,
                  "ice_slots_created": 0, "ice_slots_updated": 0,
                  "venues_created": 0}
        # See commit_teams_players_import's identical note: generated up
        # front so every row-level audit entry can be tagged with it (#102).
        batch_id = self.store.next_id("importbatch")

        with self.store.transaction():
            rink_code_to_id = {}
            for row in rink_rows:
                rink_code = _clean(row.get("rink_code"))
                venue_name = _clean(row.get("venue_name"))
                rink_name_raw = row.get("rink_name")
                # rink_name isn't in validate_import's required fields for
                # "rinks" (only venue_name/rink_code are). A brand-new rink
                # defaults its name to the code so it's never blank (an
                # explicit judgment call mirroring #93's position-default,
                # may want revisiting); an EXISTING rink's name is a
                # partial-field-overwrite — a repeat row that omits
                # rink_name must leave the current name alone rather than
                # clobbering it back to the code (review fix).
                rink_name_supplied = not _blank(rink_name_raw)
                rink_name = _clean(rink_name_raw) if rink_name_supplied else None
                address_raw = row.get("address")
                address = _clean(address_raw) if not _blank(address_raw) else ""

                venue = next((v for v in self.store.all_venues()
                             if v.name == venue_name), None)
                if venue is None:
                    venue = Venue(id=self.store.next_id("venue"), name=venue_name,
                                  address=address)
                    self.store.add_venue(venue)
                    self._audit("venue_created", "venue", venue.id, actor_id,
                                {"import_batch_id": batch_id})
                    counts["venues_created"] += 1

                rink = next((r for r in self.store.all_rinks()
                            if r.external_ref == rink_code), None)
                if rink is not None:
                    if rink_name_supplied:
                        rink.name = rink_name
                    rink.venue_id = venue.id
                    self.store.save_rink(rink)
                    self._audit("rink_updated", "rink", rink.id, actor_id,
                                {"venue_id": venue.id, "import_batch_id": batch_id})
                    counts["rinks_updated"] += 1
                else:
                    rink = Rink(id=self.store.next_id("rink"), venue_id=venue.id,
                               name=rink_name if rink_name_supplied else rink_code,
                               external_ref=rink_code)
                    self.store.add_rink(rink)
                    self._audit("rink_created", "rink", rink.id, actor_id,
                                {"venue_id": venue.id, "import_batch_id": batch_id})
                    counts["rinks_created"] += 1
                rink_code_to_id[rink_code] = rink.id

            for row in slot_rows:
                rink_code = _clean(row.get("rink_code"))
                # validate_import already guarantees this rink_code matches a
                # row in THIS SAME upload's rinks sheet; .get() is just a
                # defensive belt-and-suspenders check against a bug elsewhere.
                rink_id = rink_code_to_id.get(rink_code)
                if rink_id is None:
                    raise ValidationError(
                        f"Unknown rink_code {rink_code} for ice_slots row.")

                start = _parse_iso_utc(row.get("start_time"))
                end = _parse_iso_utc(row.get("end_time"))
                slot_type = IceSlotType(_clean(row.get("slot_type")))

                existing_slot = next(
                    (s for s in self.store.all_ice_slots()
                     if s.rink_id == rink_id and s.start_time == start
                     and s.end_time == end), None)
                if existing_slot is not None:
                    existing_slot.slot_type = slot_type
                    if self.store.game_using_ice_slot(existing_slot.id) is None:
                        existing_slot.status = (
                            IceSlotStatus.AVAILABLE
                            if slot_type == IceSlotType.GAME
                            else IceSlotStatus.BLOCKED)
                    self.store.save_ice_slot(existing_slot)
                    self._audit("ice_slot_updated", "ice_slot", existing_slot.id,
                                actor_id, {"rink_id": rink_id,
                                          "slot_type": slot_type.value,
                                          "import_batch_id": batch_id})
                    counts["ice_slots_updated"] += 1
                else:
                    status = (IceSlotStatus.AVAILABLE
                             if slot_type == IceSlotType.GAME
                             else IceSlotStatus.BLOCKED)
                    slot = IceSlot(id=self.store.next_id("slot"), rink_id=rink_id,
                                  start_time=start, end_time=end,
                                  slot_type=slot_type, status=status)
                    self.store.add_ice_slot(slot)
                    self._audit("ice_slot_created", "ice_slot", slot.id, actor_id,
                                {"rink_id": rink_id, "slot_type": slot_type.value,
                                 "import_batch_id": batch_id})
                    counts["ice_slots_created"] += 1

            # skipped/errors are always 0 here by construction — see the
            # identical note on commit_teams_players_import's import_committed
            # audit row above.
            self._audit("import_committed", "import_batch", batch_id, actor_id,
                        {"import_type": "rinks_ice_slots", "skipped": 0,
                         "errors": 0, **counts})

        return {
            "committed": True,
            "summary": {
                "rinks": {"created": counts["rinks_created"],
                         "updated": counts["rinks_updated"]},
                "ice_slots": {"created": counts["ice_slots_created"],
                             "updated": counts["ice_slots_updated"]},
                "venues_created": counts["venues_created"],
            },
            "warnings": result["warnings"],
        }

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

    # -- official availability (#88) ---------------------------------------
    def set_official_availability(self, official_id, start_time, end_time,
                                  status, note=None, actor_id=None,
                                  extra_detail=None):
        """Declare an available/unavailable window for an official (#88).

        ``extra_detail`` merges additional keys into the audit entry's
        ``detail`` dict — used by ``commit_officials_availability_import``
        (#102) to tag the entry with its ``import_batch_id`` without this
        single-entity method needing to know anything about imports.

        Not ``@_transactional``: the decorator here was an orphan that had
        drifted off ``assign_official`` (its rightful owner) onto this method
        via the section comment. Restored to ``assign_official`` below; this is
        a single add + audit like ``delete_official_availability``."""
        if self.store.get_official(official_id) is None:
            raise NotFoundError(f"Official {official_id} not found.")
        try:
            st = OfficialAvailabilityStatus(status) if not isinstance(
                status, OfficialAvailabilityStatus) else status
        except ValueError:
            raise ValidationError(f"Unknown availability status '{status}'.")
        if end_time <= start_time:
            raise ValidationError("The end time must be after the start time.")
        a = OfficialAvailability(
            id=self.store.next_id("oavail"), official_id=official_id,
            start_time=start_time, end_time=end_time, status=st, note=note)
        self.store.add_official_availability(a)
        detail = {"official_id": official_id, "status": st.value}
        if extra_detail:
            detail.update(extra_detail)
        self._audit("official_availability_set", "official_availability", a.id,
                    actor_id, detail)
        return a

    def official_availabilities(self, official_id):
        return sorted(self.store.availability_for_official(official_id),
                      key=lambda a: a.start_time)

    def delete_official_availability(self, avail_id, actor_id=None):
        a = self.store.get_official_availability(avail_id)
        if a is None:
            raise NotFoundError("Availability window not found.")
        self.store.delete_official_availability(avail_id)
        self._audit("official_availability_deleted", "official_availability",
                    avail_id, actor_id, {"official_id": a.official_id})
        return a

    def unavailable_window(self, official_id, start, end):
        """The official's UNAVAILABLE window overlapping [start, end), else None."""
        if start is None or end is None:
            return None
        for a in self.store.availability_for_official(official_id):
            if a.status != OfficialAvailabilityStatus.UNAVAILABLE:
                continue
            if start < a.end_time and end > a.start_time:
                return a
        return None

    @_transactional
    def assign_official(self, game_id: str, official_id: str, role: OfficialRole,
                        actor_id: Optional[str] = None,
                        override_unavailable: bool = False) -> OfficialAssignment:
        """Propose an official for a role on a game, with conflict checks.

        If the official has declared an overlapping UNAVAILABLE window (#88),
        the assignment is blocked unless ``override_unavailable`` is set — an
        explicit operator override, which is audited.
        """
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

        # Declared-unavailable window (#88): block unless the operator overrides.
        unavail = self.unavailable_window(official_id, start, end)
        if unavail is not None and not override_unavailable:
            raise ScheduleConflictError(
                f"{official.name} is marked unavailable at this time.",
                details={"reason": "official_unavailable",
                         "availability_id": unavail.id,
                         "note": unavail.note})

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
        self._notify(
            NotificationKind.ASSIGNMENT_OFFERED, NotificationAudience.OFFICIAL,
            "New game assignment",
            f"You've been assigned as {role.value} for {self._matchup(game)}.",
            audience_ref=official_id, game_id=game_id, assignment_id=assignment.id)
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
        official = self.store.get_official(a.official_id)
        name = official.name if official else "An official"
        game = self.store.get_game(a.game_id)
        matchup = self._matchup(game) if game else "a game"
        self._notify(
            NotificationKind.ASSIGNMENT_ACCEPTED if accept
            else NotificationKind.ASSIGNMENT_DECLINED,
            NotificationAudience.SCHEDULER,
            f"Assignment {'accepted' if accept else 'declined'}",
            f"{name} {'accepted' if accept else 'declined'} the {a.role.value} "
            f"assignment for {matchup}.",
            game_id=a.game_id, assignment_id=a.id)
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
        # Tell the official their assignment was removed (#87).
        game = self.store.get_game(a.game_id)
        label = self._game_label(game) if game else "a game"
        self._notify(NotificationKind.ASSIGNMENT_UNASSIGNED,
                     NotificationAudience.OFFICIAL, "Assignment removed",
                     f"You are no longer assigned to {label}.",
                     audience_ref=a.official_id, game_id=a.game_id)
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
        game = self.store.get_game(game_id)
        if game is None:
            raise NotFoundError(f"Game {game_id} not found.")
        if game.cancelled:
            raise ValidationError("Cannot approve a result for a cancelled game.")
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
        self._notify(
            NotificationKind.RESULT_APPROVED, NotificationAudience.PUBLIC,
            "Final result",
            f"Final: {self._matchup(game)} — {result.home_score}–{result.away_score}.",
            game_id=game_id)
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
