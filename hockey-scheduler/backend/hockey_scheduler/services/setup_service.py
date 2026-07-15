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
    Program,
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
    SeasonVenueAccess,
    SetupAuditLog,
    Team,
    Venue,
    intervals_overlap,
)
from ..domain.errors import (
    DivisionMismatchError,
    HasDependenciesError,
    InvalidTransitionError,
    NotEligibleError,
    NotFoundError,
    ScheduleConflictError,
    ValidationError,
)
from ..store import InMemoryStore
from .import_validator import validate_import, validate_official_availability
from .league_scope import (
    registered_team_ids_in_division as _registered_team_ids,
    team_registration_valid,
)
from .notifier import push as _push_notification


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _blank(value) -> bool:
    return value is None or not str(value).strip()


def _clean(value) -> str:
    return str(value).strip()


def _no_club(value) -> bool:
    """Blank, null, and the literal spreadsheet placeholder "NA" (any case)
    all mean "no Club" on import (#233 Slice D) — never a Club literally
    named "NA"."""
    return _blank(value) or _clean(value).upper() == "NA"


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

    # -- program / season / league / division -----------------------------
    @_transactional
    def create_program(self, name: str, country: str = "", timezone_name: str = "UTC",
                       operator_organization_id: Optional[str] = None,
                       actor_id: Optional[str] = None) -> Program:
        # Optional operating organization (#173) — validated when given so a
        # program never dangles off a non-existent operator. Nullable for
        # migration/legacy; null-operator programs surface in the queue.
        if operator_organization_id and \
                self.store.get_organization(operator_organization_id) is None:
            raise NotFoundError(
                f"Organization {operator_organization_id} not found.")
        program = Program(id=self.store.next_id("league"),
                          name=self._require_name(name), country=country,
                          timezone=timezone_name or "UTC",
                          operator_organization_id=operator_organization_id or None)
        self.store.add_program(program)
        self._audit("league_created", "league", program.id, actor_id,
                    {"organization_id": operator_organization_id}
                    if operator_organization_id else None)
        return program

    @_transactional
    def create_season(self, program_id: str, name: str,
                      start_date: Optional[datetime] = None,
                      end_date: Optional[datetime] = None,
                      actor_id: Optional[str] = None) -> Season:
        if self.store.get_program(program_id) is None:
            raise NotFoundError(f"Program {program_id} not found.")
        start = self._require_utc(start_date, "start_date") if start_date else None
        end = self._require_utc(end_date, "end_date") if end_date else None
        if start and end and end < start:
            raise ValidationError("end_date cannot be before start_date.")
        season = Season(id=self.store.next_id("season"), program_id=program_id,
                        name=self._require_name(name), start_date=start, end_date=end)
        self.store.add_season(season)
        self._audit("season_created", "season", season.id, actor_id,
                    {"league_id": program_id})
        return season

    @_transactional
    def create_league(self, season_id: str, name: str, sort_order: int = 0,
                      actor_id: Optional[str] = None) -> League:
        if self.store.get_season(season_id) is None:
            raise NotFoundError(f"Season {season_id} not found.")
        league = League(id=self.store.next_id("level"), season_id=season_id,
                        name=self._require_name(name), sort_order=sort_order or 0)
        self.store.add_league(league)
        self._audit("level_created", "level", league.id, actor_id,
                    {"season_id": season_id})
        return league

    @_transactional
    def create_division(self, season_id: str, name: str, age_group: str = "",
                        league_id: Optional[str] = None,
                        actor_id: Optional[str] = None) -> Division:
        if self.store.get_season(season_id) is None:
            raise NotFoundError(f"Season {season_id} not found.")
        # An optional owning league/grouping (#166/#233) — validated when given:
        # it must exist AND belong to this division's season, so a division can
        # never sit under a league from a different season. Null is fine.
        if league_id:
            league = self.store.get_league(league_id)
            if league is None:
                raise NotFoundError(f"League {league_id} not found.")
            if league.season_id != season_id:
                raise ValidationError(
                    "League belongs to a different season than the division.")
        division = Division(id=self.store.next_id("division"), season_id=season_id,
                            name=self._require_name(name), age_group=age_group,
                            league_id=league_id or None)
        self.store.add_division(division)
        self._audit("division_created", "division", division.id, actor_id,
                    {"season_id": season_id,
                     **({"level_id": league_id} if league_id else {})})
        return division

    def create_division_under_league(self, league_id: str, name: str,
                                     age_group: str = "",
                                     actor_id: Optional[str] = None) -> Division:
        """Create a Division parented by a grouping League (#233 Slice C2, v2).

        The v2 canonical path: the League is REQUIRED and its Season is *derived*
        from the league — a caller never supplies season_id. Validates the league
        exists, then delegates to ``create_division`` (which re-checks league↔
        season consistency and audits). Not ``@_transactional`` itself — the
        ``get_league`` read is outside any transaction and ``create_division``
        opens its own (the store transaction is not reentrant). v1
        ``create_division`` (season_id + optional level→league) is untouched."""
        if not league_id:
            raise ValidationError("A league_id is required.")
        league = self.store.get_league(league_id)
        if league is None:
            raise NotFoundError(f"League {league_id} not found.")
        return self.create_division(league.season_id, name, age_group,
                                    league_id, actor_id)

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
    def create_team(self, club_id: Optional[str] = None,
                    division_id: Optional[str] = None,
                    name: str = "", actor_id: Optional[str] = None,
                    program_id: Optional[str] = None) -> Team:
        """Create a permanent league team (#180).

        A team belongs permanently to a *league*, not a division — its
        season-specific division participation lives in SeasonTeamRegistration.
        The legacy ``Team.division_id`` is NEVER written here (#180): the created
        team carries only its permanent ``league_id``. Two ways to say which
        league:

        - Pass ``league_id`` directly (the #180-correct path: create the team
          under the league, then register it into a season/division separately).
        - Pass a ``division_id`` (back-compat convenience for seeds/import): the
          league is *derived* from the division's season — a read only; the
          division is not stored on the Team.

        Exactly one is required. When both are given, the division's league
        wins (and must not contradict a supplied league_id).

        ``club_id`` is optional (#233 Slice D): a team's Club is just an
        affiliation, not a structural requirement, and many programs use no
        club at all. Only validate it when a non-null id is actually supplied
        — never invent or require a placeholder Club.
        """
        if club_id and self.store.get_club(club_id) is None:
            raise NotFoundError(f"Club {club_id} not found.")
        if division_id:
            division = self.store.get_division(division_id)
            if division is None:
                raise NotFoundError(f"Division {division_id} not found.")
            season = self.store.get_season(division.season_id)
            derived_program = season.program_id if season else None
            if program_id and derived_program and program_id != derived_program:
                raise ValidationError(
                    "The chosen division belongs to a different program.")
            program_id = derived_program
        if not program_id:
            raise ValidationError(
                "A team needs a program (choose a program, or a division to "
                "derive it from).")
        if self.store.get_program(program_id) is None:
            raise NotFoundError(f"Program {program_id} not found.")
        team = Team(id=self.store.next_id("team"), name=self._require_name(name),
                    club_id=club_id or None, program_id=program_id)
        self.store.add_team(team)
        self._audit("team_created", "team", team.id, actor_id,
                    {"club_id": team.club_id, "league_id": program_id})
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
                                 actor_id: Optional[str] = None,
                                 league_id: Optional[str] = None
                                 ) -> SeasonTeamRegistration:
        season = self.store.get_season(season_id)
        if season is None:
            raise NotFoundError(f"Season {season_id} not found.")
        team = self.store.get_team(team_id)
        if team is None:
            raise NotFoundError(f"Team {team_id} not found.")
        # Rule 4 — program consistency: the team's permanent program must match
        # the season's program. Cross-program registration is rejected.
        if team.program_id and team.program_id != season.program_id:
            raise ValidationError(
                "Team belongs to a different program than this season.")
        if division_id:
            division = self.store.get_division(division_id)
            if division is None:
                raise NotFoundError(f"Division {division_id} not found.")
            # Rule 4 — a division from another season can't be used here.
            if division.season_id != season_id:
                raise ValidationError(
                    "Division belongs to a different season.")
        # v2 (#233 Slice C2): an explicit ``league_id`` is REQUIRED-and-validated
        # — it must resolve to this Season and, when a division is given, own that
        # division. It then becomes the registration's league verbatim (not the
        # v1 C1b derivation). When omitted (v1), keep the derivation below.
        explicit_league_id = league_id or None
        if explicit_league_id:
            # v2 (#233 Slice C2 review): a canonical registration must resolve to
            # the same Program as the Team — require an EXACT, non-null Team→
            # Program match. The permissive Rule-4 check above only rejects a
            # non-null *different* program, so a legacy Team with no program would
            # otherwise slip into the canonical tree. v1 (no explicit league)
            # keeps that permissive behavior.
            if not team.program_id or team.program_id != season.program_id:
                raise ValidationError(
                    "Team must belong to this season's program.",
                    {"reason": "team_program_mismatch",
                     "team_id": team.id,
                     "team_program_id": team.program_id,
                     "season_program_id": season.program_id})
            league = self.store.get_league(explicit_league_id)
            if league is None:
                raise NotFoundError(f"League {explicit_league_id} not found.")
            if league.season_id != season_id:
                raise ValidationError(
                    "League belongs to a different season than this registration.")
            if division_id and division.league_id != explicit_league_id:
                raise ValidationError(
                    "Division belongs to a different league than the registration.")
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
            existing.league_id = explicit_league_id or \
                self._derive_registration_league(season_id, existing.division_id)
            self.store.save_season_team_registration(existing)
            self._audit("season_team_registered", "season_team_registration",
                        existing.id, actor_id,
                        {"season_id": season_id, "team_id": team_id,
                         "division_id": existing.division_id, "reactivated": True})
            return existing
        reg = SeasonTeamRegistration(
            id=self.store.next_id("streg"), season_id=season_id, team_id=team_id,
            division_id=division_id or None,
            league_id=explicit_league_id or self._derive_registration_league(
                season_id, division_id or None),
            active=True)
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

    def _registration_league(self, reg):
        """The program a registration resolves to (via its season), or None."""
        season = self.store.get_season(reg.season_id)
        return season.program_id if season else None

    def _derive_registration_league(self, season_id, division_id):
        """The competition League (#233) a registration belongs to: its
        division's league when the division carries one, else the season's sole
        league. None when neither is determinable (mirrors the migration
        backfill; the C1a preflight guarantees a single league at upgrade)."""
        if division_id:
            division = self.store.get_division(division_id)
            if division is not None and division.league_id:
                return division.league_id
        leagues = [lv for lv in self.store.all_leagues()
                   if lv.season_id == season_id]
        return leagues[0].id if len(leagues) == 1 else None

    @_transactional
    def assign_season_team_division(self, registration_id: str,
                                    division_id: Optional[str] = None,
                                    actor_id: Optional[str] = None,
                                    v2: bool = False
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
            # v2 (#233 Slice C2): the canonical registration League is required
            # and load-bearing — a division set on it must belong to that exact
            # League. Reject a division whose League disagrees with the
            # registration's League rather than silently re-deriving.
            if v2 and division.league_id != reg.league_id:
                raise ValidationError(
                    "Division belongs to a different league than the "
                    "registration.",
                    {"reason": "division_league_mismatch",
                     "registration_id": reg.id,
                     "registration_league_id": reg.league_id,
                     "division_league_id": division.league_id})
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
        if v2:
            # Preserve the registration's required League (#233 Slice C2):
            # clearing the Division makes the team division-less UNDER the same
            # League — the required league_id must never be nulled. When a
            # Division is set, it was validated above to match the League, so the
            # League is likewise unchanged.
            pass
        else:
            reg.league_id = self._derive_registration_league(
                reg.season_id, reg.division_id)
        self.store.save_season_team_registration(reg)
        self._audit("season_team_division_assigned", "season_team_registration",
                    reg.id, actor_id, {"from": old, "to": reg.division_id})
        return reg

    @_transactional
    def assign_season_team_league(self, registration_id: str,
                                  league_id: Optional[str] = None,
                                  actor_id: Optional[str] = None
                                  ) -> SeasonTeamRegistration:
        """Reassign a registration's competition League (#233 Slice C2, v2).

        The League is REQUIRED and validated: it must resolve to the
        registration's own Season and, when the registration carries a division,
        own that division (cross-consistency). Sets the registration's
        ``league_id`` verbatim rather than re-deriving it."""
        reg = self.store.get_season_team_registration(registration_id)
        if reg is None:
            raise NotFoundError(f"Registration {registration_id} not found.")
        if not league_id:
            raise ValidationError("A league_id is required.")
        league = self.store.get_league(league_id)
        if league is None:
            raise NotFoundError(f"League {league_id} not found.")
        if league.season_id != reg.season_id:
            raise ValidationError(
                "League belongs to a different season than this registration.")
        if reg.division_id:
            division = self.store.get_division(reg.division_id)
            if division is not None and division.league_id != league_id:
                raise ValidationError(
                    "Registration's division belongs to a different league.")
        old = reg.league_id
        # Integrity (#233 Slice C2 review): a registration's League may only
        # change while no committed Game already relies on the team's OLD League
        # this season. A published/scheduled game carries its own ``league_id``;
        # moving the registration out from under it would strand that game in a
        # League the team no longer participates in. Reject (safe default) and
        # mutate ZERO records/audit rather than silently invalidating a fixture.
        if (league_id or None) != (old or None):
            stranded = [
                g.id for g in self.store.all_games()
                if not g.cancelled and not g.is_draft
                and g.season_id == reg.season_id
                and g.league_id == old
                and reg.team_id in (g.home_team_id, g.away_team_id)]
            if stranded:
                raise ValidationError(
                    "Cannot change this registration's league while committed "
                    "games reference its current league for this team; resolve "
                    "those games first.",
                    {"reason": "registration_league_change_strands_games",
                     "registration_id": reg.id,
                     "affected_game_ids": stranded, "count": len(stranded)})
        reg.league_id = league_id
        self.store.save_season_team_registration(reg)
        self._audit("season_team_league_assigned", "season_team_registration",
                    reg.id, actor_id, {"from": old, "to": reg.league_id})
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
    def delete_season_team_registration(self, registration_id: str,
                                        actor_id: Optional[str] = None
                                        ) -> dict:
        """Permanently remove an INACTIVE, game-free registration (#251).

        unregister_team_from_season() deliberately only deactivates a row —
        it retains the registration's Season/Team/League/Division identity
        for history, which is correct, but leaves no way to resolve the
        hidden blocker it creates for delete_league/delete_season/
        delete_team (each of which still — correctly — blocks on ANY
        registration row, active or not, since League/Season/Team are
        required identifiers on a registration and can't simply be cleared
        the way delete_division clears an optional division_id). This is
        the explicit, safe cleanup operation for that hidden row: never an
        active registration, never one any Game still references (draft,
        scheduled, cancelled, or historical all count as history).
        """
        reg = self.store.get_season_team_registration(registration_id)
        if reg is None:
            raise NotFoundError(f"Registration {registration_id} not found.")
        if reg.active:
            raise ValidationError(
                "Cannot permanently delete an active registration; remove "
                "it from the season first.",
                {"reason": "registration_active", "registration_id": reg.id})
        # Resolve every parent this row claims to belong to BEFORE any write —
        # an inactive row whose Season/Team/League has since vanished, or
        # whose Division no longer resolves, is not safe to purge blindly,
        # and the caller needs real labels (not bare ids) to confirm what it
        # just removed. Division alone is genuinely optional on the model.
        season = self.store.get_season(reg.season_id)
        if season is None:
            raise ValidationError(
                "This registration's Season no longer exists.",
                {"reason": "invalid_season", "season_id": reg.season_id})
        team = self.store.get_team(reg.team_id)
        if team is None:
            raise ValidationError(
                "This registration's Team no longer exists.",
                {"reason": "invalid_team", "team_id": reg.team_id})
        league = self.store.get_league(reg.league_id) if reg.league_id else None
        if league is None:
            raise ValidationError(
                "This registration's League no longer resolves.",
                {"reason": "invalid_league", "league_id": reg.league_id})
        division = None
        if reg.division_id:
            division = self.store.get_division(reg.division_id)
            if division is None:
                raise ValidationError(
                    "This registration's Division no longer resolves.",
                    {"reason": "invalid_division",
                     "division_id": reg.division_id})
        games = [g for g in self.store.all_games()
                if g.season_id == reg.season_id
                and reg.team_id in (g.home_team_id, g.away_team_id)]
        self._block_if_dependents(
            "season_team_registration", registration_id, "registration", [
                self._dep_group("game", games, self._matchup)])
        detail = {"season_id": reg.season_id, "team_id": reg.team_id,
                  "league_id": reg.league_id, "division_id": reg.division_id,
                  "reason": "explicit_inactive_cleanup"}
        self.store.delete_season_team_registration(registration_id)
        self._audit("season_team_registration_deleted",
                    "season_team_registration", registration_id, actor_id,
                    detail)
        return {"id": reg.id, **detail,
                "season_name": season.name, "team_name": team.name,
                "league_name": league.name,
                "division_name": division.name if division else None}

    @_transactional
    def grant_season_venue_access(self, season_id: str, venue_id: str,
                                  actor_id: Optional[str] = None
                                  ) -> SeasonVenueAccess:
        """Give a Season access to a Venue's ice (#233 Slice E).

        Physical structure (Venue) and competition structure (Season) are
        independent — unlike the legacy Venue.league_id bridge, this never
        constrains a Venue to one Program, and a Season may hold access to any
        number of Venues regardless of operator. Rule mirrors
        register_team_for_season's Rule 5: a prior *inactive* access row (once
        revoked, now regranted) is reactivated in place rather than
        duplicated, honoring the (season_id, venue_id) one-active-row
        invariant enforced by the partial unique index (migration 029).
        """
        season = self.store.get_season(season_id)
        if season is None:
            raise NotFoundError(f"Season {season_id} not found.")
        venue = self.store.get_venue(venue_id)
        if venue is None:
            raise NotFoundError(f"Venue {venue_id} not found.")
        existing = self.store.season_venue_access_for_pair(season_id, venue_id)
        if existing is not None:
            if existing.active:
                raise ValidationError(
                    f"Season {season_id} already has access to venue "
                    f"{venue_id}.",
                    {"reason": "already_active", "access_id": existing.id})
            existing.active = True
            self.store.save_season_venue_access(existing)
            self._audit("season_venue_access_granted", "season_venue_access",
                        existing.id, actor_id,
                        {"season_id": season_id, "venue_id": venue_id,
                         "reactivated": True})
            return existing
        access = SeasonVenueAccess(
            id=self.store.next_id("sva"), season_id=season_id,
            venue_id=venue_id, active=True)
        self.store.add_season_venue_access(access)
        self._audit("season_venue_access_granted", "season_venue_access",
                    access.id, actor_id,
                    {"season_id": season_id, "venue_id": venue_id})
        return access

    @_transactional
    def revoke_season_venue_access(self, access_id: str,
                                   actor_id: Optional[str] = None
                                   ) -> SeasonVenueAccess:
        """Deactivate a Season's access to a Venue (#233 Slice E).

        Deactivates only — history is preserved (mirrors
        unregister_team_from_season) so any Game already scheduled against
        this Venue for this Season remains intact and auditable. A revoked
        row still blocks a Season/Venue delete (delete_season/delete_venue
        check every access row regardless of active status, mirroring
        delete_league/delete_season/delete_team's identical registration
        check) — delete_season_venue_access below is the explicit,
        separate cleanup action for an already-revoked row.
        """
        access = self.store.get_season_venue_access(access_id)
        if access is None:
            raise NotFoundError(f"Season-venue access {access_id} not found.")
        if not access.active:
            raise ValidationError(
                "This access is already revoked.",
                {"reason": "already_revoked", "access_id": access.id})
        access.active = False
        self.store.save_season_venue_access(access)
        self._audit("season_venue_access_revoked", "season_venue_access",
                    access.id, actor_id,
                    {"season_id": access.season_id, "venue_id": access.venue_id})
        return access

    @_transactional
    def delete_season_venue_access(self, access_id: str,
                                   actor_id: Optional[str] = None) -> dict:
        """Permanently remove an already-revoked Season-Venue access row
        (#233 Slice E, #255 review).

        revoke_season_venue_access only deactivates a row, preserving it as a
        blocker for delete_season/delete_venue (both check every row
        regardless of active status) so the grant/revoke history stays
        auditable by default. This is the explicit, separate cleanup action
        that actually removes an inactive row once an operator has confirmed
        it no longer needs to block a parent delete — mirrors
        delete_season_team_registration (#251) exactly. Never an active row.
        """
        access = self.store.get_season_venue_access(access_id)
        if access is None:
            raise NotFoundError(f"Season-venue access {access_id} not found.")
        if access.active:
            raise ValidationError(
                "Cannot permanently delete an active access; revoke it "
                "first.",
                {"reason": "access_active", "access_id": access.id})
        detail = {"season_id": access.season_id, "venue_id": access.venue_id,
                  "reason": "explicit_revoked_cleanup"}
        self.store.delete_season_venue_access(access_id)
        self._audit("season_venue_access_deleted", "season_venue_access",
                    access_id, actor_id, detail)
        return {"id": access_id, **detail}

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
        # Season ids must be strings before they reach the store: an unhashable
        # JSON value (list/dict) from a malformed body would otherwise raise a
        # TypeError inside ``store.get_season`` (dict.get / SQL bind) — a 500,
        # not the structured validation_error (#197) this route must return.
        if not isinstance(from_season_id, str) or _blank(from_season_id):
            raise ValidationError("from_season_id must be a non-empty string.")
        if not isinstance(to_season_id, str) or _blank(to_season_id):
            raise ValidationError("to_season_id must be a non-empty string.")
        src = self.store.get_season(from_season_id)
        if src is None:
            raise NotFoundError(f"Season {from_season_id} not found.")
        dst = self.store.get_season(to_season_id)
        if dst is None:
            raise NotFoundError(f"Season {to_season_id} not found.")
        if from_season_id == to_season_id:
            raise ValidationError("Source and target seasons must differ.")
        # Rule 4 — a rollover stays within one program.
        if (src.program_id or None) != (dst.program_id or None):
            raise ValidationError(
                "Cannot roll participation between seasons of different programs.")
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
                # Must be a non-empty *string*: a non-string id (incl. an
                # unhashable list/dict) would slip past ``_blank`` — which
                # stringifies its argument — and then raise a TypeError on the
                # ``in source_active`` set-membership test below.
                if not isinstance(tid, str) or _blank(tid):
                    raise ValidationError(
                        "Each selection needs a non-empty team_id.")
                if tid not in source_active:
                    raise ValidationError(
                        f"Team {tid} is not registered in the source season.")
                div = sel.get("division_id")
                # Likewise reject a non-string division_id before it reaches the
                # ``set(wanted.values())`` de-dup, which would TypeError on an
                # unhashable value.
                if div is not None and not isinstance(div, str):
                    raise ValidationError(
                        "A selection's division_id must be a string or null.")
                wanted[tid] = div or None
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
        program_id = src.program_id or None
        for tid in wanted:
            team = self.store.get_team(tid)
            if team is None:
                raise ValidationError(
                    f"Team {tid} in the source season no longer exists; "
                    "it cannot be rolled forward.")
            if (team.program_id or None) != program_id:
                raise ValidationError(
                    f"Team {tid} belongs to a different program than this "
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
                existing.league_id = self._derive_registration_league(
                    to_season_id, div_id)
                self.store.save_season_team_registration(existing)
                reg = existing
            else:
                reg = SeasonTeamRegistration(
                    id=self.store.next_id("streg"), season_id=to_season_id,
                    team_id=tid, division_id=div_id,
                    league_id=self._derive_registration_league(to_season_id, div_id),
                    active=True)
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

    @_transactional
    def roll_forward_registrations_v2(self, from_season_id: str,
                                      to_season_id: str,
                                      selections: Optional[list] = None,
                                      actor_id: Optional[str] = None) -> dict:
        """Canonical v2 rollover honoring the required grouping League (#233 C2).

        Unlike the FROZEN v1 ``roll_forward_registrations`` (which ignores the
        grouping League and derives it), every carried selection REQUIRES an
        explicit target ``league_id``: the League must belong to the TARGET
        Season, the optional Division must belong to that League and Season, and
        the written registration's ``league_id`` is that League verbatim (never
        null). Selections are therefore mandatory in v2 — a division-less
        copy-all can't know which League each team joins. Same all-or-nothing
        pre-write gate as v1: any failure aborts the batch with zero writes."""
        if not isinstance(from_season_id, str) or _blank(from_season_id):
            raise ValidationError("from_season_id must be a non-empty string.")
        if not isinstance(to_season_id, str) or _blank(to_season_id):
            raise ValidationError("to_season_id must be a non-empty string.")
        src = self.store.get_season(from_season_id)
        if src is None:
            raise NotFoundError(f"Season {from_season_id} not found.")
        dst = self.store.get_season(to_season_id)
        if dst is None:
            raise NotFoundError(f"Season {to_season_id} not found.")
        if from_season_id == to_season_id:
            raise ValidationError("Source and target seasons must differ.")
        if (src.program_id or None) != (dst.program_id or None):
            raise ValidationError(
                "Cannot roll participation between seasons of different programs.")
        source_active = {r.team_id
                         for r in self.store.registrations_for_season(from_season_id)
                         if r.active}
        if not isinstance(selections, list) or not selections:
            raise ValidationError(
                "v2 rollover requires a non-empty selections list; each "
                "selection needs a target league_id.")
        program_id = src.program_id or None
        # Pre-write gate — resolve/validate every selection before any write, so
        # a failure leaves the target season and audit log untouched.
        wanted = {}  # team_id -> (league_id, division_id)
        for sel in selections:
            if not isinstance(sel, dict):
                raise ValidationError(
                    "Each selection must be an object with a team_id and "
                    "league_id.")
            tid = sel.get("team_id")
            if not isinstance(tid, str) or _blank(tid):
                raise ValidationError("Each selection needs a non-empty team_id.")
            if tid not in source_active:
                raise ValidationError(
                    f"Team {tid} is not registered in the source season.")
            lid = sel.get("league_id")
            if not isinstance(lid, str) or _blank(lid):
                raise ValidationError(
                    f"Selection for team {tid} needs a target league_id.")
            league = self.store.get_league(lid)
            if league is None:
                raise NotFoundError(f"League {lid} not found.")
            if league.season_id != to_season_id:
                raise ValidationError(
                    "A selection's league belongs to a different season than "
                    "the target season.")
            div = sel.get("division_id")
            if div is not None:
                if not isinstance(div, str):
                    raise ValidationError(
                        "A selection's division_id must be a string or null.")
                division = self.store.get_division(div)
                if division is None:
                    raise NotFoundError(f"Division {div} not found.")
                if division.season_id != to_season_id:
                    raise ValidationError(
                        "A target division belongs to a different season.")
                if division.league_id != lid:
                    raise ValidationError(
                        "A selection's division belongs to a different league "
                        "than the selection's league.")
            team = self.store.get_team(tid)
            if team is None:
                raise ValidationError(
                    f"Team {tid} in the source season no longer exists; "
                    "it cannot be rolled forward.")
            if (team.program_id or None) != program_id:
                raise ValidationError(
                    f"Team {tid} belongs to a different program than this "
                    "rollover; it cannot be carried into this season.")
            div_id = div or None
            # An already-active target registration is an idempotent skip ONLY
            # when its League AND Division exactly match this selection. A
            # mismatch means the selection's required League/Division would be
            # silently ignored (the team left in its current League) — a
            # contract violation. Catch it in the pre-write gate so the whole
            # batch aborts with zero writes rather than reporting a false skip.
            existing = self.store.registration_for_team_in_season(
                to_season_id, tid)
            if existing is not None and existing.active and (
                    (existing.league_id or None) != lid
                    or (existing.division_id or None) != div_id):
                raise ValidationError(
                    f"Team {tid} is already registered in the target season "
                    "under a different league/division than this selection; "
                    "resolve the existing registration first.",
                    {"reason": "rollover_conflicts_active_registration",
                     "team_id": tid, "registration_id": existing.id,
                     "expected_league_id": lid, "expected_division_id": div_id,
                     "actual_league_id": existing.league_id,
                     "actual_division_id": existing.division_id})
            wanted[tid] = (lid, div_id)

        rolled, skipped, created = 0, 0, []
        for tid, (lid, div_id) in wanted.items():
            existing = self.store.registration_for_team_in_season(to_season_id, tid)
            if existing is not None and existing.active:
                # Guaranteed an exact League+Division match by the pre-write gate
                # above — a safe idempotent skip.
                skipped += 1
                continue
            if existing is not None:  # reactivate a prior inactive registration
                existing.active = True
                existing.division_id = div_id
                existing.league_id = lid
                self.store.save_season_team_registration(existing)
                reg = existing
            else:
                reg = SeasonTeamRegistration(
                    id=self.store.next_id("streg"), season_id=to_season_id,
                    team_id=tid, division_id=div_id, league_id=lid, active=True)
                self.store.add_season_team_registration(reg)
            self._audit("season_team_registered", "season_team_registration",
                        reg.id, actor_id,
                        {"season_id": to_season_id, "team_id": tid,
                         "division_id": div_id, "league_id": lid,
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
    def assign_division_league(self, division_id: str,
                               league_id: Optional[str] = None,
                               actor_id: Optional[str] = None,
                               v2: bool = False) -> Division:
        division = self.store.get_division(division_id)
        if division is None:
            raise NotFoundError(f"Division {division_id} not found.")
        # v2 (#233 Slice C2): a canonical Division is always parented by a
        # grouping League — the reparent target is REQUIRED. v1 keeps its nullable
        # unassign behavior (league_id=None clears the division's level).
        if v2 and not league_id:
            raise ValidationError("A league_id is required.")
        if league_id:
            league = self.store.get_league(league_id)
            if league is None:
                raise NotFoundError(f"League {league_id} not found.")
            # The target League must be in the SAME Season as the division (and
            # thus the division's current League) — a cross-season move is invalid.
            if league.season_id != division.season_id:
                raise ValidationError(
                    "League belongs to a different season than the division.")
        old = division.league_id
        # v2 dependent-record integrity (#233 Slice C2 review): moving a Division
        # between Leagues must not strand its registrations or committed games
        # under a League that no longer matches. Any active registration or
        # non-cancelled game bound to this division whose own ``league_id`` isn't
        # the new League would become cross-league — reject (safe default) and
        # mutate ZERO records/audit rather than silently splitting the data.
        if v2 and (league_id or None) != (old or None):
            stranded_regs = [
                r.id for r in
                self.store.registrations_for_season(division.season_id)
                if r.active and r.division_id == division.id
                and r.league_id != league_id]
            stranded_games = [
                g.id for g in self.store.all_games()
                if not g.cancelled and g.division_id == division.id
                and g.league_id != league_id]
            if stranded_regs or stranded_games:
                raise ValidationError(
                    "Cannot move this division to another league while "
                    "registrations or games under it belong to the old league; "
                    "reconcile them first.",
                    {"reason": "division_reparent_strands_dependents",
                     "division_id": division.id,
                     "affected_registration_ids": stranded_regs,
                     "affected_game_ids": stranded_games})
        division.league_id = league_id or None
        self.store.save_division(division)
        self._audit("division_level_assigned", "division", division.id, actor_id,
                    {"from": old, "to": division.league_id})
        return division

    @_transactional
    def assign_team_club(self, team_id: str, club_id: Optional[str] = None,
                         actor_id: Optional[str] = None) -> Team:
        team = self.store.get_team(team_id)
        if team is None:
            raise NotFoundError(f"Team {team_id} not found.")
        # Club is optional on a Team in both v1 and v2 (#233 Slice D): null
        # unassigns it. Only validate the id when one is actually supplied.
        if club_id and self.store.get_club(club_id) is None:
            raise NotFoundError(f"Club {club_id} not found.")
        old = team.club_id
        team.club_id = club_id or None
        self.store.save_team(team)
        self._audit("team_club_assigned", "team", team.id, actor_id,
                    {"from": old, "to": team.club_id})
        return team

    # assign_team_division was removed (#180): a Team's season/division placement
    # lives in SeasonTeamRegistration (assign_season_team_division), never on the
    # legacy Team.division_id. The legacy field is retained only for persistence/
    # migration compatibility and is no longer written by any current workflow.

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

    # -- cross-domain reassignment: league owner (#173) --------------------
    @_transactional
    def assign_program_organization(self, program_id: str,
                                    organization_id: Optional[str] = None,
                                    actor_id: Optional[str] = None) -> Program:
        program = self.store.get_program(program_id)
        if program is None:
            raise NotFoundError(f"Program {program_id} not found.")
        if organization_id and self.store.get_organization(organization_id) is None:
            raise NotFoundError(f"Organization {organization_id} not found.")
        old = program.operator_organization_id
        # #233 Slice E: physical (facility owner) and competition (Program
        # operator) structure are independent trees — a Program's operating
        # organization is free to change without regard to any Venue, since
        # Venue<->Program access is now a decoupled SeasonVenueAccess grant,
        # not a shared-owner bridge.
        program.operator_organization_id = organization_id or None
        self.store.save_program(program)
        self._audit("league_organization_assigned", "league", program.id, actor_id,
                    {"from": old, "to": program.operator_organization_id})
        return program

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
            league = self.store.get_program(league_id)
            if league is None:
                raise NotFoundError(f"Program {league_id} not found.")
            league_owner = league.operator_organization_id
            if organization_id and league_owner and organization_id != league_owner:
                raise ValidationError(
                    "Current v1 compatibility: while the temporary Venue→Program "
                    "link is active (removed in Slice E), a venue's facility owner "
                    "must match the program's operating organization.")
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

    # -- shared season-registration guard (#180) ---------------------------
    # A team's participation in a (season, division) is resolved through its
    # SeasonTeamRegistration — the single source of truth — not the legacy
    # Team.division_id. Game creation, moves, publishing, and standings all go
    # through these helpers so the same rule is enforced everywhere; a team may
    # only play in a season+division it is actively registered in.
    def _require_team_registered(self, season_id: str, team_id: str,
                                 division_id: Optional[str], team=None,
                                 *, require_division: bool = True):
        """Raise ``DivisionMismatchError`` unless ``team_id`` has an active,
        league-consistent registration in ``season_id`` — and, when
        ``require_division``, in ``division_id``. A registration is trusted only
        if its Team exists and the Team's permanent league matches the season's
        (#199: rows can be orphaned or cross-league). ``require_division=False``
        (the override path) still demands an active season registration; it
        relaxes only the division match. Returns the registration on success."""
        season = self.store.get_season(season_id) if season_id else None
        resolved = team if team is not None else self.store.get_team(team_id)
        label = resolved.name if resolved is not None else team_id
        reg = team_registration_valid(self.store, season, team_id, division_id,
                                      require_division=require_division)
        if reg is not None:
            return reg
        # No valid registration — surface the precise reason.
        raw = (self.store.registration_for_team_in_season(season_id, team_id)
               if season is not None else None)
        if raw is None or not raw.active:
            raise DivisionMismatchError(
                f"{label} is not registered in this season.",
                {"reason": "team_not_registered", "team_id": team_id,
                 "season_id": season_id})
        rteam = self.store.get_team(team_id)
        season_league = season.program_id if season is not None else None
        if (rteam is None or not rteam.program_id or not season_league
                or rteam.program_id != season_league):
            raise DivisionMismatchError(
                f"{label}'s registration does not belong to this program.",
                {"reason": "registration_cross_league", "team_id": team_id,
                 "season_id": season_id})
        raise DivisionMismatchError(
            f"{label} is not registered in this division.",
            {"reason": "team_wrong_division", "team_id": team_id,
             "season_id": season_id, "division_id": division_id,
             "registered_division_id": raw.division_id})

    def _team_participates(self, season, team_id: str,
                           division_id: Optional[str],
                           require_division: bool = True) -> bool:
        """Non-raising counterpart of ``_require_team_registered`` — used where
        participation gates a *decision* (e.g. whether to run the cross-league
        ice guard) rather than a hard failure."""
        return team_registration_valid(
            self.store, season, team_id, division_id,
            require_division=require_division) is not None

    def registered_team_ids_in_division(self, division_id: str) -> set:
        """Team ids validly registered in ``division_id`` — the division's
        membership/standings roster. Delegates to the shared resolver so it
        excludes orphaned/cross-league rows exactly as draft generation does."""
        return _registered_team_ids(self.store, division_id)

    # -- manual game creation ---------------------------------------------
    @_transactional
    def create_game(self, season_id: str, division_id: str, home_team_id: str,
                    away_team_id: str, ice_slot_id: str,
                    target_goalies: int = 1, target_skaters: int = 15,
                    max_skaters: int = 18, allow_division_override: bool = False,
                    actor_id: Optional[str] = None,
                    league_id: Optional[str] = None) -> Game:
        season = self.store.get_season(season_id)
        if season is None:
            raise NotFoundError(f"Season {season_id} not found.")

        # v2 competition scope (#233 Slice C2): when a ``league_id`` is supplied
        # it is REQUIRED-and-validated against the Season, and ``division_id``
        # becomes OPTIONAL. v1 (league_id=None) is unchanged — division_id stays
        # mandatory and the game's league is derived from the division below.
        scoped_league_id = league_id or None
        if scoped_league_id:
            league = self.store.get_league(scoped_league_id)
            if league is None:
                raise NotFoundError(f"League {scoped_league_id} not found.")
            if league.season_id != season_id:
                raise ValidationError(
                    "League belongs to a different season than the game.")

        division = None
        if division_id:
            division = self.store.get_division(division_id)
            if division is None:
                raise NotFoundError(f"Division {division_id} not found.")
            if division.season_id != season_id:
                raise ValidationError(
                    "Division does not belong to the given season."
                )
            if scoped_league_id and division.league_id != scoped_league_id:
                raise ValidationError(
                    "Division belongs to a different league than the game.")
            if not scoped_league_id:
                scoped_league_id = division.league_id
        elif not scoped_league_id:
            # v1 path: a division is mandatory — preserve the exact legacy error.
            raise NotFoundError(f"Division {division_id} not found.")

        if home_team_id == away_team_id:
            raise ValidationError("A team cannot play itself.")

        home = self.store.get_team(home_team_id)
        away = self.store.get_team(away_team_id)
        if home is None:
            raise NotFoundError(f"Team {home_team_id} not found.")
        if away is None:
            raise NotFoundError(f"Team {away_team_id} not found.")

        # Participation is resolved through SeasonTeamRegistration (#180), not
        # the legacy Team.division_id: both teams must have an active, league-
        # consistent registration in this season AND division. Cross-division
        # games are deprecated until an explicit competition mode exists (#200
        # review): allow_division_override no longer relaxes the division match,
        # so every game's teams are co-registered and it can be moved,
        # rescheduled, and published without a dead-end. The flag is retained
        # (and audited below) for API compatibility. When v2 omits a division,
        # the registration check relaxes to season-only (require_division=False).
        require_division = division_id is not None
        home_reg = self._require_team_registered(
            season_id, home_team_id, division_id, home,
            require_division=require_division)
        away_reg = self._require_team_registered(
            season_id, away_team_id, division_id, away,
            require_division=require_division)

        # v2 canonical scope (#233 Slice C2): when the game is created WITH a
        # ``league_id``, both teams' active registrations must be in that exact
        # grouping League — a game is tied to its teams' registration League, not
        # merely to a season they share. (division consistency + division→league
        # agreement were already checked above.) The v1 path (league_id=None)
        # keeps today's behavior: no registration-league constraint. This runs
        # before any slot allocation, so a mismatch mutates nothing.
        if league_id is not None:
            for team, reg in ((home, home_reg), (away, away_reg)):
                if reg.league_id != scoped_league_id:
                    label = team.name if team is not None else "Team"
                    raise ValidationError(
                        f"{label}'s registration belongs to a different league "
                        "than this game.",
                        {"reason": "registration_wrong_league",
                         "team_id": team.id if team is not None else None,
                         "season_id": season_id,
                         "expected_league_id": scoped_league_id,
                         "registered_league_id": reg.league_id})

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
            division_id=division_id or None,
            ice_slot_id=ice_slot_id,
            league_id=scoped_league_id,
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
        # A game may only be made public while both teams are still co-registered
        # in its season+division (#180 shared guard). Unpublishing is unguarded so
        # an invalid fixture can always be pulled back from public view.
        if published and game.season_id and game.division_id:
            self._require_team_registered(
                game.season_id, game.home_team_id, game.division_id)
            self._require_team_registered(
                game.season_id, game.away_team_id, game.division_id)
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
        # A move can't revive a fixture whose participation has since become
        # invalid: both teams must still be co-registered in the game's
        # season+division (#180 shared guard).
        if game.season_id and game.division_id:
            self._require_team_registered(
                game.season_id, game.home_team_id, game.division_id)
            self._require_team_registered(
                game.season_id, game.away_team_id, game.division_id)

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
        _season = self.store.get_season(season_id)
        if _season is None:
            raise NotFoundError(f"Season {season_id} not found.")
        # The permanent program every imported team belongs to (#180): the
        # program of the season being imported into.
        season_league_id = _season.program_id

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
            # Pre-write integrity gate (#180 review): before ANY write, prove the
            # import won't silently re-home a permanent Team across leagues or
            # strand committed games by moving a registration's division. A
            # violation returns a structured error with zero writes.
            if (not season_league_id
                    or self.store.get_program(season_league_id) is None):
                return {"committed": False, "summary": result["summary"],
                        "errors": [{"sheet": "teams",
                            "reason": "season_league_missing",
                            "message": ("The import target season is not linked "
                                        "to a valid league."),
                            "season_id": season_id}],
                        "warnings": result["warnings"]}
            gate_errors = []
            for row in team_rows:
                code = _clean(row.get("team_code"))
                existing = next((t for t in self.store.all_teams()
                                 if t.external_ref == code), None)
                if existing is None:
                    continue
                team_regs = [r for r in self.store.all_season_team_registrations()
                             if r.team_id == existing.id]
                # (1) The import must never move a permanent Team's program.
                if (existing.program_id or None) != season_league_id:
                    if existing.program_id is None:
                        # A league-less team is repaired to the target league
                        # only if EVERY retained registration already resolves
                        # there; otherwise its history would go cross-league.
                        stray = [r.id for r in team_regs
                                 if self._registration_league(r) != season_league_id]
                        if stray:
                            gate_errors.append({
                                "sheet": "teams", "team_code": code,
                                "reason": "team_league_ambiguous",
                                "message": (f"Team {code} has registrations in "
                                            "another league; assign its permanent "
                                            "league before importing."),
                                "affected_registration_ids": stray})
                    else:
                        gate_errors.append({
                            "sheet": "teams", "team_code": code,
                            "reason": "team_league_move_blocked",
                            "message": (f"Team {code} already belongs to a "
                                        "different league; the import can't "
                                        "re-home it."),
                            "affected_registration_ids": [r.id for r in team_regs],
                            "affected_game_ids": [
                                g.id for g in self.store.all_games()
                                if existing.id in (g.home_team_id, g.away_team_id)]})
                # (2) ANY change to a registration's division must not strand
                # committed games — the same guard assign_season_team_division
                # enforces. The target is resolved for every row, including
                # None for a blank division (which would CLEAR the division), so
                # a blank re-import can't quietly unassign a team that still has
                # scheduled games. Applies to inactive/historical registrations
                # too (a re-import reactivates and may re-place them).
                div_name = row.get("division_name")
                reg = self.store.registration_for_team_in_season(
                    season_id, existing.id)
                if reg is not None:
                    if _blank(div_name):
                        target_div_id = None
                    else:
                        match = next(
                            (d for d in self.store.all_divisions()
                             if d.season_id == season_id
                             and d.name == _clean(div_name)), None)
                        # A not-yet-created named division is necessarily a
                        # different placement than the current one.
                        target_div_id = match.id if match else object()
                    if target_div_id != reg.division_id:
                        stranded = self._games_scheduled_for_team_in_season(
                            season_id, existing.id)
                        if stranded:
                            gate_errors.append({
                                "sheet": "teams", "team_code": code,
                                "reason": "registration_division_move_strands_games",
                                "message": (f"Re-importing team {code} would move "
                                            "or clear its division while it has "
                                            "scheduled games; resolve them first."),
                                "affected_game_ids": stranded})
            if gate_errors:
                return {"committed": False, "summary": result["summary"],
                        "errors": gate_errors, "warnings": result["warnings"]}

            team_code_to_id = {}
            for row in team_rows:
                team_code = _clean(row.get("team_code"))
                team_name = _clean(row.get("team_name"))

                club_id = None
                club_name_raw = row.get("club_name")
                if not _no_club(club_name_raw):
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

                # #180: a team's participation is converged onto the permanent
                # league_id + a SeasonTeamRegistration, never the legacy
                # Team.division_id. The team is a permanent member of THIS
                # import's season league; the imported division lives on the
                # registration for (season_id, team), not on the Team.
                team = next((t for t in self.store.all_teams()
                            if t.external_ref == team_code), None)
                if team is not None:
                    team.name = team_name
                    team.club_id = club_id
                    if season_league_id:
                        team.program_id = season_league_id
                    self.store.save_team(team)
                    self._audit("team_updated", "team", team.id, actor_id,
                                {"club_id": club_id, "league_id": team.program_id,
                                 "import_batch_id": batch_id})
                    counts["teams_updated"] += 1
                else:
                    team = Team(id=self.store.next_id("team"), name=team_name,
                               club_id=club_id, program_id=season_league_id,
                               external_ref=team_code)
                    self.store.add_team(team)
                    self._audit("team_created", "team", team.id, actor_id,
                                {"club_id": club_id, "league_id": season_league_id,
                                 "import_batch_id": batch_id})
                    counts["teams_created"] += 1
                team_code_to_id[team_code] = team.id

                # Idempotently upsert THIS season's registration with the
                # imported division; never touch another season's row.
                reg = self.store.registration_for_team_in_season(season_id, team.id)
                if reg is not None:
                    if not reg.active or reg.division_id != division_id:
                        reg.active = True
                        reg.division_id = division_id
                        reg.league_id = self._derive_registration_league(
                            season_id, division_id)
                        self.store.save_season_team_registration(reg)
                        self._audit("season_team_registration_updated",
                                    "season_team_registration", reg.id, actor_id,
                                    {"season_id": season_id, "team_id": team.id,
                                     "division_id": division_id,
                                     "import_batch_id": batch_id})
                else:
                    reg = SeasonTeamRegistration(
                        id=self.store.next_id("streg"), season_id=season_id,
                        team_id=team.id, division_id=division_id,
                        league_id=self._derive_registration_league(
                            season_id, division_id),
                        active=True)
                    self.store.add_season_team_registration(reg)
                    self._audit("season_team_registered",
                                "season_team_registration", reg.id, actor_id,
                                {"season_id": season_id, "team_id": team.id,
                                 "division_id": division_id,
                                 "import_batch_id": batch_id})

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
                if not _no_club(club_name_raw):
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
    def list_programs(self) -> List[Program]:
        return list(self.store.all_programs())

    def list_seasons(self, program_id: str) -> List[Season]:
        return self.store.seasons_for_program(program_id)

    def list_divisions(self, season_id: str) -> List[Division]:
        return self.store.divisions_for_season(season_id)

    def record_demo_event(self, action: str,
                          actor_id: Optional[str] = None) -> SetupAuditLog:
        """Audit a demo-lifecycle event (#215) — ``demo_reset``/``demo_loaded``/
        ``demo_cleared`` — once the new dataset exists.

        Written by DemoState.reset after the (re)build, inside the same atomic
        unit for a SQL store, so the action and its audit row commit together.
        Not ``@_transactional``: the caller already owns the transaction.
        """
        return self._audit(action, "demo", "demo", actor_id,
                           {"at": self.clock().isoformat()})

    # -- safe destructive deletion (#215) ---------------------------------
    # Every delete runs a *pre-write* dependency gate: if any dependent record
    # or history exists it raises HasDependenciesError with a structured
    # breakdown and writes nothing. The gate must precede the first write
    # because the in-memory store's transaction is a lock, not a rollback, so a
    # mid-delete abort would not undo anything. Deletion is never a silent
    # cascade — the operator must clear the dependents first. All eight routed
    # under /api/setup (MANAGE_SETUP → League Admin only), each audited with the
    # server-resolved actor id.
    _DEP_SAMPLE = 8  # cap sample names per dependency group in the error detail

    def _dep_group(self, label: str, items: list, name_fn, id_fn=None,
                   display: Optional[str] = None) -> dict:
        """One dependency group for a blocked delete.

        Carries the full ``count`` plus a capped list of ``items`` — each an
        ``{id, name}`` pair — so the UI can locate a specific blocker even when
        names collide (#215 review 4). ``id_fn`` defaults to the record's ``id``
        attribute; pass one when the blocker's identifier lives elsewhere.
        ``names`` is retained as a convenience mirror of the item names.

        ``type`` is the frozen structured code (e.g. ``league``/``level``);
        ``display`` is the human-facing noun rendered in the message and UI
        (#233 — e.g. ``program``/``league``). It defaults to ``type`` so groups
        whose display noun is unchanged need no extra argument.
        """
        id_of = id_fn or (lambda x: getattr(x, "id", None))
        sample = items[:self._DEP_SAMPLE]
        pairs = [{"id": id_of(x), "name": name_fn(x)} for x in sample]
        return {"type": label, "display": display or label, "count": len(items),
                "items": pairs, "names": [p["name"] for p in pairs]}

    def _block_if_dependents(self, entity_type: str, entity_id: str,
                             entity_label: str, groups: list) -> None:
        groups = [g for g in groups if g["count"]]
        if not groups:
            return
        total = sum(g["count"] for g in groups)
        parts = ", ".join(f"{g['count']} {g.get('display', g['type'])}"
                          f"{'' if g['count'] == 1 else 's'}" for g in groups)
        raise HasDependenciesError(
            f"Can't delete this {entity_label} — {total} dependent "
            f"record(s) still exist ({parts}). Remove them first.",
            details={"entity_type": entity_type, "entity_id": entity_id,
                     "dependencies": groups})

    def _team_name(self, team_id) -> str:
        team = self.store.get_team(team_id) if team_id else None
        return team.name if team else (team_id or "—")

    def _season_name(self, season_id) -> str:
        season = self.store.get_season(season_id) if season_id else None
        return season.name if season else (season_id or "—")

    def _venue_name(self, venue_id) -> str:
        venue = self.store.get_venue(venue_id) if venue_id else None
        return venue.name if venue else (venue_id or "—")

    def _slot_label(self, slot) -> str:
        rink = self.store.get_rink(slot.rink_id) if slot.rink_id else None
        when = slot.start_time.isoformat() if slot.start_time else ""
        return f"{rink.name if rink else 'ice slot'} {when}".strip()

    @_transactional
    def delete_organization(self, org_id: str,
                            actor_id: Optional[str] = None) -> Organization:
        org = self.store.get_organization(org_id)
        if org is None:
            raise NotFoundError(f"Organization {org_id} not found.")
        leagues = [lg for lg in self.store.all_programs()
                   if lg.operator_organization_id == org_id]
        venues = [v for v in self.store.all_venues()
                  if v.organization_id == org_id]
        self._block_if_dependents("organization", org_id, "facility owner", [
            self._dep_group("league", leagues, lambda lg: lg.name,
                            display="program"),
            self._dep_group("venue", venues, lambda v: v.name)])
        self.store.delete_organization(org_id)
        self._audit("organization_deleted", "organization", org_id, actor_id,
                    {"name": org.name})
        return org

    @_transactional
    def delete_league(self, league_id: str, actor_id: Optional[str] = None) -> League:
        league = self.store.get_league(league_id)
        if league is None:
            raise NotFoundError(f"League {league_id} not found.")
        divisions = [d for d in self.store.all_divisions()
                     if d.league_id == league_id]
        # #233 B2b review r2: a registration's league_id is REQUIRED in v2 and
        # can point directly at this League with no Division (division-less
        # participation) — checking only Divisions as dependents let a League
        # delete silently orphan such a registration's required league_id.
        # Mirrors delete_division's own registration check just below.
        regs = [r for r in self.store.all_season_team_registrations()
                if r.league_id == league_id]
        self._block_if_dependents("level", league_id, "league", [
            self._dep_group("division", divisions, lambda d: d.name),
            self._dep_group("team registration", regs,
                            lambda r: self._team_name(r.team_id))])
        self.store.delete_league(league_id)
        self._audit("level_deleted", "level", league_id, actor_id,
                    {"name": league.name, "season_id": league.season_id})
        return league

    @_transactional
    def delete_program(self, program_id: str, actor_id: Optional[str] = None) -> Program:
        program = self.store.get_program(program_id)
        if program is None:
            raise NotFoundError(f"Program {program_id} not found.")
        seasons = self.store.seasons_for_program(program_id)
        teams = self.store.teams_for_program(program_id)
        venues = [v for v in self.store.all_venues() if v.league_id == program_id]
        self._block_if_dependents("league", program_id, "program", [
            self._dep_group("season", seasons, lambda s: s.name),
            self._dep_group("team", teams, lambda t: t.name),
            self._dep_group("venue", venues, lambda v: v.name)])
        self.store.delete_program(program_id)
        self._audit("league_deleted", "league", program_id, actor_id,
                    {"name": program.name})
        return program

    @_transactional
    def delete_season(self, season_id: str, actor_id: Optional[str] = None) -> Season:
        season = self.store.get_season(season_id)
        if season is None:
            raise NotFoundError(f"Season {season_id} not found.")
        levels = [lv for lv in self.store.all_leagues() if lv.season_id == season_id]
        divisions = self.store.divisions_for_season(season_id)
        regs = self.store.registrations_for_season(season_id)
        # Games/results/history reference the season directly (#215): a game
        # whose division is legacy/null/mismatched still carries season_id, so
        # check by season_id rather than trusting the division tree above.
        games = [g for g in self.store.all_games() if g.season_id == season_id]
        # SeasonVenueAccess (#233 Slice E, reviewer blocker on #255): checked
        # regardless of active status, mirroring team registrations above —
        # revoke_season_venue_access deliberately only deactivates a row (the
        # grant/revoke history is preserved), so an inactive row is not proof
        # the Season is free of it. delete_season_venue_access (mirroring
        # #251's delete_season_team_registration) is the explicit cleanup an
        # operator runs on each revoked row before this delete can succeed.
        venue_access = self.store.season_venue_access_for_season(season_id)
        self._block_if_dependents("season", season_id, "season", [
            self._dep_group("level", levels, lambda lv: lv.name,
                            display="league"),
            self._dep_group("division", divisions, lambda d: d.name),
            self._dep_group("team registration", regs,
                            lambda r: self._team_name(r.team_id)),
            self._dep_group("game", games, self._matchup),
            self._dep_group("venue access", venue_access,
                            lambda a: self._venue_name(a.venue_id))])
        self.store.delete_season(season_id)
        self._audit("season_deleted", "season", season_id, actor_id,
                    {"name": season.name, "league_id": season.program_id})
        return season

    @_transactional
    def delete_division(self, division_id: str,
                        actor_id: Optional[str] = None) -> dict:
        division = self.store.get_division(division_id)
        if division is None:
            raise NotFoundError(f"Division {division_id} not found.")
        regs = [r for r in self.store.all_season_team_registrations()
                if r.division_id == division_id]
        games = [g for g in self.store.all_games() if g.division_id == division_id]
        # Deletion keys off real operational dependents only (#180, #233 D1
        # bundled fix): an ACTIVE registration or any Game blocks deletion. An
        # INACTIVE registration (the team was removed from the season via
        # unregister_team_from_season, which deliberately retains division_id
        # for history) is not a live dependent — the Setup UI can show 0 teams
        # under a Division while these hidden rows still exist, so they must
        # not silently block deletion forever. A stale legacy
        # Team.division_id pointer is no longer read operationally, so it
        # never blocks a division delete either.
        active_regs = [r for r in regs if r.active]
        inactive_regs = [r for r in regs if not r.active]
        self._block_if_dependents("division", division_id, "division", [
            self._dep_group("team registration", active_regs,
                            lambda r: self._team_name(r.team_id)),
            self._dep_group("game", games, self._matchup)])
        # Clear the inactive registrations' division_id (never a hard delete —
        # the row, its Season/Team/League identity, and its active=False
        # status are all retained) before removing the Division itself. Each
        # cleanup is audited individually against its own registration (the
        # old→new convention used elsewhere, e.g. season_team_league_assigned)
        # so every affected row has its own traceable audit entry.
        for reg in inactive_regs:
            old_division_id = reg.division_id
            reg.division_id = None
            self.store.save_season_team_registration(reg)
            self._audit("season_team_division_cleared",
                        "season_team_registration", reg.id, actor_id,
                        {"from": old_division_id, "to": None,
                         "reason": "division_deleted"})
        self.store.delete_division(division_id)
        self._audit("division_deleted", "division", division_id, actor_id,
                    {"name": division.name, "season_id": division.season_id,
                     "inactive_registrations_cleaned": len(inactive_regs)})
        return {"id": division.id, "season_id": division.season_id,
                "name": division.name, "age_group": division.age_group,
                "league_id": division.league_id,
                "external_ref": division.external_ref,
                "inactive_registrations_cleaned": len(inactive_regs)}

    @_transactional
    def delete_club(self, club_id: str, actor_id: Optional[str] = None) -> Club:
        club = self.store.get_club(club_id)
        if club is None:
            raise NotFoundError(f"Club {club_id} not found.")
        teams = [t for t in self.store.all_teams() if t.club_id == club_id]
        self._block_if_dependents("club", club_id, "club", [
            self._dep_group("team", teams, lambda t: t.name)])
        self.store.delete_club(club_id)
        self._audit("club_deleted", "club", club_id, actor_id, {"name": club.name})
        return club

    @_transactional
    def delete_team(self, team_id: str, actor_id: Optional[str] = None) -> Team:
        team = self.store.get_team(team_id)
        if team is None:
            raise NotFoundError(f"Team {team_id} not found.")
        regs = [r for r in self.store.all_season_team_registrations()
                if r.team_id == team_id]
        games = [g for g in self.store.all_games()
                 if team_id in (g.home_team_id, g.away_team_id)]
        players = self.store.players_for_team(team_id)
        # Live scoped/integration state also references a Team directly (#215
        # review 4): a coach account bound to it, a live team calendar feed, and
        # any team-targeted contact/preference/device rows. Deleting the Team
        # while these exist would strand an active identity or integration
        # pointing at a missing record, so they block the delete too. (This is a
        # block, not a silent cascade or account erasure — the latter is an
        # explicit non-goal of #215.)
        accounts = [a for a in self.store.all_user_accounts()
                    if (a.scope or {}).get("team_id") == team_id]
        feeds = [t for t in self.store.all_calendar_feed_tokens()
                 if t.actor_type == "team" and t.actor_ref == team_id
                 and t.revoked_at is None]
        contacts = [c for c in self.store.all_contact_destinations()
                    if c.recipient_ref == team_id]
        prefs = [p for p in self.store.all_notification_preferences()
                 if p.recipient_ref == team_id]
        devices = [d for d in self.store.all_device_tokens()
                   if d.recipient_ref == team_id]
        self._block_if_dependents("team", team_id, "team", [
            self._dep_group("season registration", regs,
                            lambda r: self._season_name(r.season_id)),
            self._dep_group("game", games, self._matchup),
            self._dep_group("player", players, lambda p: p.name),
            self._dep_group("account", accounts, lambda a: a.username),
            self._dep_group("calendar feed", feeds,
                            lambda t: t.label or t.actor_ref),
            self._dep_group("contact destination", contacts,
                            lambda c: c.label or c.destination),
            self._dep_group("notification preference", prefs,
                            lambda p: p.channel.value),
            self._dep_group("device token", devices,
                            lambda d: d.label or d.provider)])
        self.store.delete_team(team_id)
        self._audit("team_deleted", "team", team_id, actor_id,
                    {"name": team.name, "league_id": team.program_id})
        return team

    @_transactional
    def delete_venue(self, venue_id: str, actor_id: Optional[str] = None) -> Venue:
        venue = self.store.get_venue(venue_id)
        if venue is None:
            raise NotFoundError(f"Venue {venue_id} not found.")
        rinks = [r for r in self.store.all_rinks() if r.venue_id == venue_id]
        # SeasonVenueAccess (#233 Slice E, reviewer blocker on #255): checked
        # regardless of active status — see delete_season's identical
        # comment; delete_season_venue_access is the matching cleanup op.
        venue_access = self.store.season_venue_access_for_venue(venue_id)
        self._block_if_dependents("venue", venue_id, "venue", [
            self._dep_group("rink", rinks, lambda r: r.name),
            self._dep_group("venue access", venue_access,
                            lambda a: self._season_name(a.season_id))])
        self.store.delete_venue(venue_id)
        self._audit("venue_deleted", "venue", venue_id, actor_id,
                    {"name": venue.name})
        return venue

    @_transactional
    def delete_rink(self, rink_id: str, actor_id: Optional[str] = None) -> Rink:
        rink = self.store.get_rink(rink_id)
        if rink is None:
            raise NotFoundError(f"Rink {rink_id} not found.")
        slots = [s for s in self.store.all_ice_slots() if s.rink_id == rink_id]
        self._block_if_dependents("rink", rink_id, "rink", [
            self._dep_group("ice slot", slots, self._slot_label)])
        self.store.delete_rink(rink_id)
        self._audit("rink_deleted", "rink", rink_id, actor_id,
                    {"name": rink.name, "venue_id": rink.venue_id})
        return rink

    @_transactional
    def delete_ice_slot(self, slot_id: str, actor_id: Optional[str] = None) -> IceSlot:
        slot = self.store.get_ice_slot(slot_id)
        if slot is None:
            raise NotFoundError(f"Ice slot {slot_id} not found.")
        # Only an UNUSED, FUTURE, still-AVAILABLE slot may be deleted (#215).
        # A game referencing the slot is a true dependency (report it first, with
        # counts/ids). Otherwise past inventory is history and an
        # allocated/blocked/maintenance slot is in use — neither is a free future
        # opening; those are state rules that raise a plain validation error.
        # Every path is zero-write.
        games = [g for g in self.store.all_games() if g.ice_slot_id == slot_id]
        self._block_if_dependents("ice slot", slot_id, "ice slot", [
            self._dep_group("game", games, self._matchup)])
        if slot.start_time is not None and slot.start_time <= self.clock():
            raise ValidationError(
                "Only a future ice slot can be deleted; past slots are history.")
        if slot.status != IceSlotStatus.AVAILABLE:
            raise ValidationError(
                "Only an available ice slot can be deleted; this slot is "
                f"{slot.status.value} (in use or reserved).")
        self.store.delete_ice_slot(slot_id)
        self._audit("ice_slot_deleted", "ice_slot", slot_id, actor_id,
                    {"rink_id": slot.rink_id})
        return slot

    @_transactional
    def delete_game(self, game_id: str, actor_id: Optional[str] = None) -> Game:
        """Delete a scheduler *draft* game only (#215).

        Only a true draft (``is_draft`` True, not published, not cancelled, no
        result) may be hard-deleted. A manually created or committed game — even
        an unpublished one — is NOT a draft and must be cancelled instead so its
        fixture and notification history survive. A clean draft carries no
        operational work; if any roster entry, official assignment, availability
        response, substitute request, or reschedule request exists, deletion is
        blocked rather than silently orphaning it. On deletion the draft's
        allocated ice slot is released back to AVAILABLE so it can be rebooked.
        """
        game = self.store.get_game(game_id)
        if game is None:
            raise NotFoundError(f"Game {game_id} not found.")
        if not getattr(game, "is_draft", False) or game.published \
                or game.cancelled \
                or self.store.result_for_game(game_id) is not None:
            raise ValidationError(
                "Only a draft game can be deleted; a scheduled, published, or "
                "historical game must be cancelled instead.")
        roster = self.store.roster_for_game(game_id)
        assignments = self.store.assignments_for_game(game_id)
        availability = self.store.availability_for_game(game_id)
        substitutes = self.store.substitutes_for_game(game_id)
        reschedules = self.store.reschedule_requests_for_game(game_id)
        self._block_if_dependents("game", game_id, "draft game", [
            self._dep_group("roster entry", roster,
                            lambda e: getattr(e, "player_id", "player")),
            self._dep_group("official assignment", assignments,
                            lambda a: getattr(a, "official_id", "official")),
            self._dep_group("availability response", availability,
                            lambda a: getattr(a, "player_id", "player")),
            self._dep_group("substitute request", substitutes,
                            lambda s: getattr(s, "player_id", "player")),
            self._dep_group("reschedule request", reschedules, lambda r: r.id)])
        # Release the slot the draft held so the ice reads as bookable again.
        if game.ice_slot_id:
            slot = self.store.get_ice_slot(game.ice_slot_id)
            if slot is not None and slot.status == IceSlotStatus.ALLOCATED:
                slot.status = IceSlotStatus.AVAILABLE
                self.store.save_ice_slot(slot)
        self.store.delete_game(game_id)
        self._audit("game_deleted", "game", game_id, actor_id,
                    {"matchup": self._matchup(game),
                     "ice_slot_id": game.ice_slot_id})
        return game
