"""API facade.

Each method maps 1:1 to an endpoint in docs/architecture/api-contract.md and
returns plain JSON-serializable dicts. Domain exceptions are caught and
returned as the structured ``{"error": {...}}`` shape so callers (and a future
web framework) never see Python tracebacks across the boundary.
"""

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, List, Optional

from ..domain import (
    AvailabilityStatus,
    IceSlotType,
    Position,
    RosterEntryStatus,
    SlotType,
    SubstituteStatus,
)
from ..domain.errors import DomainError, ValidationError
from ..services import RosterService, SetupService
from ..store import InMemoryStore


def _jsonify(value):
    """Recursively convert a value into JSON-safe primitives.

    Enums → their ``.value``; datetimes → ISO-8601 strings; dataclasses,
    dicts, and lists are walked so nested timestamps/enums are converted too.
    """
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return _jsonify(asdict(value))
    if isinstance(value, dict):
        return {k: _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(v) for v in value]
    return value


def _serialize(obj) -> dict:
    """Convert a domain dataclass to a fully JSON-safe dict."""
    return _jsonify(obj)


def _parse_enum(enum_cls, value, field_name: str):
    """Parse a client-supplied enum string, raising a structured error."""
    try:
        return enum_cls(value)
    except ValueError:
        allowed = ", ".join(e.value for e in enum_cls)
        raise ValidationError(
            f"Invalid {field_name}: {value!r}. Allowed values: {allowed}."
        )


def _parse_dt(value, field_name: str):
    """Parse an optional ISO-8601 *UTC* timestamp into a timezone-aware datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            raise ValidationError(
                f"Invalid {field_name}: {value!r}. Expected an ISO-8601 timestamp."
            )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(
            f"Invalid {field_name}: expected a timezone-aware ISO-8601 UTC timestamp."
        )
    return parsed.astimezone(timezone.utc)


def catch(fn: Callable):
    """Wrap a facade method so domain errors become structured dicts."""

    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except DomainError as exc:
            return exc.to_dict()

    return wrapper


class ApiService:
    def __init__(self, store: Optional[InMemoryStore] = None):
        self.store = store or InMemoryStore()
        self.roster = RosterService(self.store)
        self.setup = SetupService(self.store)

    # -- games -------------------------------------------------------------
    @catch
    def get_game(self, game_id: str) -> dict:
        game = self.roster._require_game(game_id)
        return _serialize(game)

    @catch
    def get_roster(self, game_id: str) -> List[dict]:
        self.roster._require_game(game_id)
        return [_serialize(e) for e in self.store.roster_for_game(game_id)]

    @catch
    def select_roster(self, game_id: str, player_ids: List[str],
                      actor_id: Optional[str] = None) -> List[dict]:
        entries = self.roster.select_roster(game_id, player_ids, actor_id)
        return [_serialize(e) for e in entries]

    @catch
    def set_roster_status(self, game_id: str, player_id: str, status: str,
                          actor_id: Optional[str] = None) -> dict:
        entry = self.roster.set_roster_entry_status(
            game_id, player_id, _parse_enum(RosterEntryStatus, status, "status"),
            actor_id,
        )
        return _serialize(entry)

    # -- availability ------------------------------------------------------
    @catch
    def get_availability(self, game_id: str) -> List[dict]:
        self.roster._require_game(game_id)
        return [_serialize(a) for a in self.store.availability_for_game(game_id)]

    @catch
    def set_availability(self, game_id: str, player_id: str,
                         availability_status: str, response_source: str = "player",
                         actor_id: Optional[str] = None) -> dict:
        av = self.roster.set_availability(
            game_id, player_id,
            _parse_enum(AvailabilityStatus, availability_status,
                        "availability_status"),
            response_source, actor_id,
        )
        return _serialize(av)

    # -- substitutes -------------------------------------------------------
    @catch
    def get_substitutes(self, game_id: str) -> List[dict]:
        self.roster._require_game(game_id)
        return [_serialize(s) for s in self.store.substitutes_for_game(game_id)]

    @catch
    def enroll_substitute(self, game_id: str, player_id: str,
                          actor_id: Optional[str] = None) -> dict:
        return _serialize(self.roster.enroll_substitute(game_id, player_id, actor_id))

    @catch
    def withdraw_substitute(self, game_id: str, player_id: str,
                            actor_id: Optional[str] = None) -> dict:
        return _serialize(self.roster.withdraw_substitute(game_id, player_id, actor_id))

    @catch
    def offer_substitute(self, game_id: str, player_id: str,
                         actor_id: Optional[str] = None,
                         expires_at: Optional[str] = None) -> dict:
        return _serialize(self.roster.offer_substitute(
            game_id, player_id, actor_id,
            offer_expires_at=_parse_dt(expires_at, "expires_at"),
        ))

    @catch
    def accept_substitute(self, game_id: str, player_id: str,
                          actor_id: Optional[str] = None) -> dict:
        return _serialize(self.roster.accept_substitute(game_id, player_id, actor_id))

    @catch
    def decline_substitute(self, game_id: str, player_id: str,
                           actor_id: Optional[str] = None) -> dict:
        return _serialize(self.roster.decline_substitute(game_id, player_id, actor_id))

    @catch
    def add_substitute_to_roster(self, game_id: str, player_id: str,
                                 actor_id: Optional[str] = None) -> dict:
        return _serialize(
            self.roster.add_substitute_to_roster(game_id, player_id, actor_id)
        )

    # -- roster status -----------------------------------------------------
    @catch
    def get_roster_status(self, game_id: str) -> dict:
        return self.roster.compute_roster_status(game_id).to_dict()

    @catch
    def auto_build_roster(self, game_id: str, actor_id: Optional[str] = None) -> dict:
        """Demo helper: select + confirm a full roster from the home team.

        Picks the home team's goalies and skaters up to the game's targets so a
        newly-scheduled game becomes immediately playable by the roster flow.
        Raises if the home team has no players (the UI shows an empty state).
        """
        game = self.roster._require_game(game_id)
        players = self.store.players_for_team(game.home_team_id)
        if not players:
            raise ValidationError(
                "Home team has no players yet. Add or import players first."
            )
        goalies = [p for p in players if p.slot_type == SlotType.GOALIE]
        skaters = [p for p in players if p.slot_type == SlotType.SKATER]
        selected = ([g.id for g in goalies[:game.target_goalies]]
                    + [s.id for s in skaters[:game.target_skaters]])
        self.roster.select_roster(game_id, selected, actor_id)
        for pid in selected:
            self.roster.set_availability(game_id, pid, AvailabilityStatus.AVAILABLE)
        status = self.roster.compute_roster_status(game_id).to_dict()
        # Coach-friendly classification of a short roster.
        status["missing_goalies"] = status["open_goalie_slots"]
        status["missing_skaters"] = status["open_skater_slots"]
        status["short_roster"] = (status["open_goalie_slots"] > 0
                                  or status["open_skater_slots"] > 0)
        return status

    @catch
    def publish_game(self, game_id: str, actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.publish_game(game_id, True, actor_id))

    # -- screen view-model -------------------------------------------------
    @catch
    def get_board(self, game_id: str) -> dict:
        """Everything the Game Detail screen needs in one call.

        Groups every team player into selected / substitute / available so
        the iPhone UI can render Coach and Player views without extra round
        trips. This is a UI convenience over the contract endpoints; it does
        not introduce new domain rules.
        """
        game = self.roster._require_game(game_id)
        status = self.roster.compute_roster_status(game_id).to_dict()

        roster = {e.player_id: e for e in self.store.roster_for_game(game_id)}
        avail = {a.player_id: a for a in self.store.availability_for_game(game_id)}
        subs = {s.player_id: s for s in self.store.substitutes_for_game(game_id)}

        rows = []
        for p in self.store.players_for_team(game.home_team_id):
            entry = roster.get(p.id)
            a = avail.get(p.id)
            s = subs.get(p.id)
            backed_out = entry is not None and not entry.status.occupies_slot
            active_sub = s is not None and s.status in (
                SubstituteStatus.ENROLLED, SubstituteStatus.OFFERED
            )
            if entry is not None:
                group = "selected"
            elif active_sub:
                group = "substitute"
            else:
                group = "available"
            rows.append({
                "id": p.id,
                "name": p.name,
                "position": p.position.value,
                "slot_type": p.slot_type.value,
                "jersey_number": p.jersey_number,
                "group": group,
                "roster_status": entry.status.value if entry else None,
                "backed_out": backed_out,
                "availability": a.availability_status.value if a else "pending",
                "sub_status": s.status.value if s else None,
            })

        notifications = [
            {"type": n.type.value, "audience": n.audience, "message": n.message,
             "at": n.at.isoformat(), "subject_player_id": n.subject_player_id}
            for n in self.store.notifications_for_game(game_id)
        ]
        audit = [
            {"action": a.action.value, "actor_id": a.actor_id,
             "subject_player_id": a.subject_player_id, "at": a.at.isoformat(),
             "detail": a.detail}
            for a in self.store.audit_for_game(game_id)
        ]
        return {
            "game": _serialize(game),
            "status": status,
            "players": rows,
            "notifications": notifications,
            "audit": audit,
            "audit_count": len(audit),
        }

    # -- coach controls ----------------------------------------------------
    @catch
    def lock_roster(self, game_id: str, actor_id: Optional[str] = None) -> dict:
        return _serialize(self.roster.lock_roster(game_id, actor_id))

    @catch
    def unlock_roster(self, game_id: str, actor_id: Optional[str] = None) -> dict:
        return _serialize(self.roster.unlock_roster(game_id, actor_id))

    @catch
    def cancel_game(self, game_id: str, actor_id: Optional[str] = None) -> dict:
        return _serialize(self.roster.cancel_game(game_id, actor_id))

    # ====================================================================
    # Full E2E demo overview (League / Arena / Schedule / Public)
    # ====================================================================
    @catch
    def get_demo_overview(self) -> dict:
        """Assemble the League/Arena/Schedule/Public view for the E2E demo.

        The ``public`` section deliberately contains NO player names or any
        personal data — only fixture information that is safe to show fans.
        """
        divisions = {d.id: d for d in self.store.divisions.values()}
        clubs = {c.id: c for c in self.store.clubs.values()}
        teams = {t.id: t for t in self.store.teams.values()}
        venues = {v.id: v for v in self.store.venues.values()}
        rinks = {r.id: r for r in self.store.rinks.values()}

        def is_junior(div):
            if div is None:
                return False
            tag = (div.age_group or div.name or "").upper()
            return tag.startswith("U")

        def team_name(tid):
            t = teams.get(tid)
            return t.name if t else tid

        division_rows = [
            {"id": d.id, "season_id": d.season_id, "name": d.name,
             "age_group": d.age_group, "is_junior": is_junior(d)}
            for d in divisions.values()
        ]
        team_rows = [
            {"id": t.id, "name": t.name, "club_id": t.club_id,
             "division_id": t.division_id,
             "club_name": clubs[t.club_id].name if t.club_id in clubs else None,
             "division_name": divisions[t.division_id].name
             if t.division_id in divisions else t.division}
            for t in teams.values()
        ]
        rink_rows = [
            {"id": r.id, "venue_id": r.venue_id, "name": r.name,
             "venue_name": venues[r.venue_id].name if r.venue_id in venues else None}
            for r in rinks.values()
        ]

        game_by_slot = {g.ice_slot_id: g for g in self.store.games.values()
                        if g.ice_slot_id}
        slot_rows = []
        for s in sorted(self.store.ice_slots.values(),
                        key=lambda x: (x.rink_id, x.start_time)):
            g = game_by_slot.get(s.id)
            slot_rows.append({
                "id": s.id, "rink_id": s.rink_id,
                "rink_name": rinks[s.rink_id].name if s.rink_id in rinks else None,
                "start_time": s.start_time.isoformat(),
                "end_time": s.end_time.isoformat(),
                "slot_type": s.slot_type.value, "status": s.status.value,
                "game_id": g.id if g else None,
                "game_label": f"{team_name(g.home_team_id)} vs "
                              f"{team_name(g.away_team_id)}" if g else None,
            })

        schedule, public_fixtures = [], []
        for g in self.store.games.values():
            div = divisions.get(g.division_id)
            rstatus = self.roster.compute_roster_status(g.id)
            venue_name = None
            slot = self.store.get_ice_slot(g.ice_slot_id) if g.ice_slot_id else None
            if slot and slot.rink_id in rinks:
                rk = rinks[slot.rink_id]
                venue_name = venues[rk.venue_id].name if rk.venue_id in venues else None
            schedule.append({
                "game_id": g.id,
                "home_team_name": team_name(g.home_team_id),
                "away_team_name": team_name(g.away_team_id) if g.away_team_id else None,
                "division_name": div.name if div else None,
                "rink_name": g.rink, "venue_name": venue_name,
                "start_time": g.start_time.isoformat(),
                "roster_status": rstatus.status.value,
                "published": g.published,
            })
            # PUBLIC: only PUBLISHED games, fixture info only — no players/PII.
            if g.published and not g.cancelled:
                public_fixtures.append({
                    "division_name": div.name if div else None,
                    "home_team_name": team_name(g.home_team_id),
                    "away_team_name": team_name(g.away_team_id) if g.away_team_id else None,
                    "venue_name": venue_name, "rink_name": g.rink,
                    "start_time": g.start_time.isoformat(),
                    "status": "Scheduled",
                    "is_junior": is_junior(div),
                })

        leagues = [_serialize(x) for x in self.store.leagues.values()]
        seasons = [_serialize(x) for x in self.store.seasons.values()]
        setup_audit = [
            {"action": a.action, "entity_type": a.entity_type,
             "entity_id": a.entity_id, "at": a.at.isoformat()}
            for a in self.store.setup_audit
        ]
        return {
            "league": leagues[0] if leagues else None,
            "leagues": leagues,
            "seasons": seasons,
            "divisions": division_rows,
            "clubs": [_serialize(c) for c in clubs.values()],
            "teams": team_rows,
            "venues": [_serialize(v) for v in venues.values()],
            "rinks": rink_rows,
            "ice_slots": slot_rows,
            "schedule": schedule,
            "public_fixtures": public_fixtures,
            "setup_audit": setup_audit,
            "setup_audit_count": len(setup_audit),
        }

    # ====================================================================
    # League + Arena setup
    # ====================================================================
    @catch
    def create_league(self, name: str, country: str = "", timezone: str = "UTC",
                      actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.create_league(name, country, timezone, actor_id))

    @catch
    def list_leagues(self) -> List[dict]:
        return [_serialize(x) for x in self.setup.list_leagues()]

    @catch
    def create_season(self, league_id: str, name: str,
                      start_date: Optional[str] = None, end_date: Optional[str] = None,
                      actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.create_season(
            league_id, name, _parse_dt(start_date, "start_date"),
            _parse_dt(end_date, "end_date"), actor_id))

    @catch
    def create_division(self, season_id: str, name: str, age_group: str = "",
                        actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.create_division(season_id, name, age_group, actor_id))

    @catch
    def create_club(self, name: str, country: str = "",
                    actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.create_club(name, country, actor_id))

    @catch
    def create_team(self, club_id: str, division_id: str, name: str,
                    actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.create_team(club_id, division_id, name, actor_id))

    @catch
    def create_venue(self, name: str, address: str = "", timezone: str = "UTC",
                     actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.create_venue(name, address, timezone, actor_id))

    @catch
    def create_rink(self, venue_id: str, name: str,
                    actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.create_rink(venue_id, name, actor_id))

    @catch
    def create_ice_slot(self, rink_id: str, start_time: str, end_time: str,
                        slot_type: str = "game", actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.create_ice_slot(
            rink_id, _parse_dt(start_time, "start_time"),
            _parse_dt(end_time, "end_time"),
            _parse_enum(IceSlotType, slot_type, "slot_type"), actor_id))

    @catch
    def create_player(self, team_id: str, name: str, position: str,
                      jersey_number: Optional[int] = None,
                      actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.add_player(
            team_id, name, _parse_enum(Position, position, "position"),
            jersey_number, actor_id))

    @catch
    def create_game(self, season_id: str, division_id: str, home_team_id: str,
                    away_team_id: str, ice_slot_id: str, target_goalies: int = 1,
                    target_skaters: int = 15, max_skaters: int = 18,
                    allow_division_override: bool = False,
                    actor_id: Optional[str] = None) -> dict:
        return _serialize(self.setup.create_game(
            season_id, division_id, home_team_id, away_team_id, ice_slot_id,
            target_goalies, target_skaters, max_skaters,
            allow_division_override, actor_id))
