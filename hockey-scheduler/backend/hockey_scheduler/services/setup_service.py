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
        when = game.start_time.isoformat() if game.start_time else "a new time"
        self._notify_game_change(
            game, NotificationKind.GAME_MOVED, "Game moved",
            f"{self._game_label(game)} moved to {game.rink or 'a new rink'} "
            f"({when}).{(' ' + reason) if reason else ''}")
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
                        self._audit("club_created", "club", club.id, actor_id)
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
                                    actor_id, {"season_id": season_id})
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
                                {"club_id": club_id, "division_id": division_id})
                    counts["teams_updated"] += 1
                else:
                    team = Team(id=self.store.next_id("team"), name=team_name,
                               division=division_name_str, club_id=club_id,
                               division_id=division_id, external_ref=team_code)
                    self.store.add_team(team)
                    self._audit("team_created", "team", team.id, actor_id,
                                {"club_id": club_id, "division_id": division_id})
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
                                {"team_id": team_id})
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
                                {"team_id": team_id})
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

            batch_id = self.store.next_id("importbatch")
            # skipped/errors are always 0 here by construction: the
            # all-or-nothing gate above means the only way to reach this line
            # is a fully clean validate_import result — any error blocks the
            # transaction before an audit row is ever written. Present anyway
            # for a stable import_committed detail shape across #93-#95.
            self._audit("import_committed", "import_batch", batch_id, actor_id,
                        {"season_id": season_id, "skipped": 0, "errors": 0,
                         **counts})

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
                        self._audit("club_created", "club", club.id, actor_id)
                        counts["clubs_created"] += 1
                    club_id = club.id

                official = next((o for o in self.store.all_officials()
                                 if o.external_ref == official_code), None)
                if official is not None:
                    official.name = name
                    official.home_club_id = club_id
                    self.store.save_official(official)
                    self._audit("official_updated", "official", official.id,
                                actor_id, {"home_club_id": club_id})
                    counts["officials_updated"] += 1
                else:
                    official = Official(id=self.store.next_id("official"),
                                        name=name, home_club_id=club_id,
                                        external_ref=official_code)
                    self.store.add_official(official)
                    self._audit("official_created", "official", official.id,
                                actor_id)
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
                                          "status": status_raw})
                    counts["availability_updated"] += 1
                else:
                    self.set_official_availability(
                        official_id, start, end, status_raw, note=note,
                        actor_id=actor_id)
                    counts["availability_created"] += 1

            batch_id = self.store.next_id("importbatch")
            # skipped/errors are always 0 here by construction — see the
            # identical note on commit_teams_players_import's import_committed
            # audit row above.
            self._audit("import_committed", "import_batch", batch_id, actor_id,
                        {"officials_created": counts["officials_created"],
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
                                  status, note=None, actor_id=None):
        """Declare an available/unavailable window for an official (#88).

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
        self._audit("official_availability_set", "official_availability", a.id,
                    actor_id, {"official_id": official_id, "status": st.value})
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
