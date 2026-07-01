"""SQL-backed store (PostgreSQL target; SQLite for local/dev/tests).

Implements the same interface as :class:`InMemoryStore`. Rows are mapped to/from
the domain dataclasses via small per-table column specs. Types are kept portable
across SQLite and Postgres: TEXT/INTEGER columns only, datetimes stored as
ISO-8601 text, booleans as 0/1, and dict payloads as JSON text.
"""

import json
import threading
from contextlib import contextmanager
from dataclasses import fields
from datetime import datetime
from typing import List, Optional

from ..domain import (
    AuditAction,
    AuditLog,
    AvailabilityStatus,
    Club,
    Division,
    Game,
    GameAvailability,
    GameRosterEntry,
    IceSlot,
    IceSlotStatus,
    IceSlotType,
    GameResult,
    League,
    NotificationEvent,
    Official,
    OfficialAssignment,
    OfficialAssignmentStatus,
    OfficialRole,
    Player,
    ResultStatus,
    Position,
    Rink,
    RosterEntryStatus,
    RosterRole,
    Season,
    SelectionSource,
    SetupAuditLog,
    SubstituteEnrollment,
    SubstituteStatus,
    Team,
    Venue,
)
from ..domain.enums import NotificationType
from .db import connect


# ---- column converters -------------------------------------------------
def _text(v):  # noqa: D401
    return v


def _enum(cls):
    return (lambda v: None if v is None else v.value,
            lambda v: None if v is None else cls(v))


def _dt():
    return (lambda v: None if v is None else v.isoformat(),
            lambda v: None if v is None else datetime.fromisoformat(v))


def _bool():
    return (lambda v: 1 if v else 0, lambda v: bool(v))


def _jsonc():
    return (lambda v: json.dumps(v or {}),
            lambda v: json.loads(v) if v else {})


class Col:
    __slots__ = ("name", "to_db", "from_db")

    def __init__(self, name, kind=None):
        self.name = name
        if kind is None:
            self.to_db, self.from_db = _text, _text
        elif isinstance(kind, tuple):
            self.to_db, self.from_db = kind
        else:
            self.to_db, self.from_db = kind, kind


def _cols(model, overrides=None):
    """Build columns from a dataclass; overrides map field → converter tuple."""
    overrides = overrides or {}
    cols = []
    for f in fields(model):
        cols.append(Col(f.name, overrides.get(f.name)))
    return cols


class Spec:
    def __init__(self, model, table, overrides=None):
        self.model = model
        self.table = table
        self.cols = _cols(model, overrides)
        self.names = [c.name for c in self.cols]


SPECS = {
    League: Spec(League, "leagues"),
    Season: Spec(Season, "seasons", {"start_date": _dt(), "end_date": _dt()}),
    Division: Spec(Division, "divisions"),
    Club: Spec(Club, "clubs"),
    Team: Spec(Team, "teams"),
    Player: Spec(Player, "players",
                 {"position": _enum(Position), "is_active": _bool()}),
    Venue: Spec(Venue, "venues"),
    Rink: Spec(Rink, "rinks"),
    IceSlot: Spec(IceSlot, "ice_slots",
                  {"start_time": _dt(), "end_time": _dt(),
                   "slot_type": _enum(IceSlotType), "status": _enum(IceSlotStatus)}),
    Game: Spec(Game, "games",
               {"start_time": _dt(), "end_time": _dt(), "roster_lock_time": _dt(),
                "locked": _bool(), "cancelled": _bool(), "published": _bool()}),
    GameRosterEntry: Spec(GameRosterEntry, "game_roster_entries",
                          {"roster_role": _enum(RosterRole),
                           "selection_source": _enum(SelectionSource),
                           "status": _enum(RosterEntryStatus),
                           "selected_at": _dt(), "updated_at": _dt()}),
    GameAvailability: Spec(GameAvailability, "game_availability",
                           {"availability_status": _enum(AvailabilityStatus),
                            "responded_at": _dt()}),
    SubstituteEnrollment: Spec(SubstituteEnrollment, "substitute_enrollments",
                               {"position": _enum(Position),
                                "status": _enum(SubstituteStatus),
                                "enrolled_at": _dt(), "offered_at": _dt(),
                                "offer_expires_at": _dt(), "accepted_at": _dt(),
                                "declined_at": _dt()}),
    AuditLog: Spec(AuditLog, "audit_logs",
                   {"action": _enum(AuditAction), "at": _dt(), "detail": _jsonc()}),
    NotificationEvent: Spec(NotificationEvent, "notification_events",
                            {"type": _enum(NotificationType), "at": _dt()}),
    SetupAuditLog: Spec(SetupAuditLog, "setup_audit_logs",
                        {"at": _dt(), "detail": _jsonc()}),
    Official: Spec(Official, "officials", {"is_active": _bool()}),
    OfficialAssignment: Spec(OfficialAssignment, "official_assignments",
                             {"role": _enum(OfficialRole),
                              "status": _enum(OfficialAssignmentStatus),
                              "assigned_at": _dt(), "responded_at": _dt()}),
    GameResult: Spec(GameResult, "game_results",
                     {"status": _enum(ResultStatus),
                      "recorded_at": _dt(), "approved_at": _dt()}),
}

# Column type for DDL: INTEGER for int/bool fields, else TEXT.
_INT_FIELDS = {
    "target_goalies", "target_skaters", "max_skaters", "jersey_number",
    "priority_rank", "is_active", "locked", "cancelled", "published",
    "home_score", "away_score",
}


def _ddl(spec) -> str:
    defs = []
    for name in spec.names:
        coltype = "INTEGER" if name in _INT_FIELDS else "TEXT"
        pk = " PRIMARY KEY" if name == "id" else ""
        defs.append(f"{name} {coltype}{pk}")
    return f"CREATE TABLE IF NOT EXISTS {spec.table} ({', '.join(defs)})"


# Helpful indexes for the common lookups (created IF NOT EXISTS).
_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_ice_slots_rink ON ice_slots(rink_id, start_time)",
    "CREATE INDEX IF NOT EXISTS ix_games_slot ON games(ice_slot_id)",
    "CREATE INDEX IF NOT EXISTS ix_games_teams ON games(home_team_id, away_team_id)",
    "CREATE INDEX IF NOT EXISTS ix_players_team ON players(team_id)",
    "CREATE INDEX IF NOT EXISTS ix_roster_game ON game_roster_entries(game_id, player_id)",
    "CREATE INDEX IF NOT EXISTS ix_subs_game ON substitute_enrollments(game_id, player_id)",
    "CREATE INDEX IF NOT EXISTS ix_avail_game ON game_availability(game_id, player_id)",
    "CREATE INDEX IF NOT EXISTS ix_off_assign_game ON official_assignments(game_id)",
    "CREATE INDEX IF NOT EXISTS ix_off_assign_official ON official_assignments(official_id)",
    "CREATE INDEX IF NOT EXISTS ix_game_results_game ON game_results(game_id)",
]


def migrate(conn, dialect) -> None:
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS counters "
                "(prefix TEXT PRIMARY KEY, value INTEGER)")
    for spec in SPECS.values():
        cur.execute(_ddl(spec))
    for ddl in _INDEXES:
        cur.execute(ddl)
    cur.execute(dialect.sql(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?) "
        "ON CONFLICT(version) DO NOTHING"),
        ("0001_initial", datetime(2026, 1, 1).isoformat()))


class SqlStore:
    def __init__(self, url: str = ":memory:"):
        self.conn, self.dialect = connect(url)
        # Reentrant: transaction() holds the lock while inner _exec re-acquires.
        self._lock = threading.RLock()
        migrate(self.conn, self.dialect)

    @contextmanager
    def transaction(self):
        """Atomic multi-write block: commit on success, roll back on error."""
        with self._lock:
            if self.dialect.paramstyle == "pyformat":  # psycopg manages it
                with self.conn.transaction():
                    yield
            else:  # sqlite (autocommit) — explicit txn
                try:
                    self.conn.execute("BEGIN")
                    yield
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()
                    raise

    def close(self) -> None:
        self.conn.close()

    def reset_schema(self) -> None:
        """Drop all tables and re-migrate — for a clean test database."""
        with self._lock:
            cur = self.conn.cursor()
            for spec in SPECS.values():
                cur.execute(f"DROP TABLE IF EXISTS {spec.table}")
            cur.execute("DROP TABLE IF EXISTS counters")
            cur.execute("DROP TABLE IF EXISTS schema_migrations")
        migrate(self.conn, self.dialect)

    # -- low-level ---------------------------------------------------------
    def _exec(self, query, params=()):
        cur = self.conn.cursor()
        cur.execute(self.dialect.sql(query), params)
        return cur

    def _insert(self, obj):
        spec = SPECS[type(obj)]
        vals = [c.to_db(getattr(obj, c.name)) for c in spec.cols]
        ph = ", ".join("?" for _ in spec.cols)
        with self._lock:
            self._exec(f"INSERT INTO {spec.table} ({', '.join(spec.names)}) "
                       f"VALUES ({ph})", vals)
        return obj

    def _update(self, obj):
        spec = SPECS[type(obj)]
        setp = ", ".join(f"{c.name} = ?" for c in spec.cols if c.name != "id")
        vals = [c.to_db(getattr(obj, c.name)) for c in spec.cols if c.name != "id"]
        vals.append(obj.id)
        with self._lock:
            self._exec(f"UPDATE {spec.table} SET {setp} WHERE id = ?", vals)
        return obj

    def _upsert(self, obj):
        if self._get(type(obj), obj.id) is None:
            return self._insert(obj)
        return self._update(obj)

    def _row_to_obj(self, model, row):
        spec = SPECS[model]
        kwargs = {c.name: c.from_db(row[c.name]) for c in spec.cols}
        return model(**kwargs)

    def _get(self, model, pk):
        spec = SPECS[model]
        with self._lock:
            cur = self._exec(f"SELECT {', '.join(spec.names)} FROM {spec.table} "
                             f"WHERE id = ?", (pk,))
            row = cur.fetchone()
        return self._row_to_obj(model, row) if row else None

    def _query(self, model, where=None, params=(), order=None):
        spec = SPECS[model]
        q = f"SELECT {', '.join(spec.names)} FROM {spec.table}"
        if where:
            q += f" WHERE {where}"
        if order:
            q += f" ORDER BY {order}"
        with self._lock:
            cur = self._exec(q, params)
            rows = cur.fetchall()
        return [self._row_to_obj(model, r) for r in rows]

    def _first(self, model, where, params):
        rows = self._query(model, where, params)
        return rows[0] if rows else None

    # -- id generation -----------------------------------------------------
    def next_id(self, prefix: str) -> str:
        with self._lock:
            cur = self._exec(
                "INSERT INTO counters(prefix, value) VALUES (?, 1) "
                "ON CONFLICT(prefix) DO UPDATE SET value = counters.value + 1 "
                "RETURNING value", (prefix,))
            row = cur.fetchone()
        # sqlite3.Row and psycopg dict_row both support key access.
        return f"{prefix}_{row['value']}"

    # -- teams / players ---------------------------------------------------
    def add_team(self, team): return self._insert(team)
    def get_team(self, team_id): return self._get(Team, team_id)
    def add_player(self, player): return self._insert(player)
    def get_player(self, player_id): return self._get(Player, player_id)

    def players_for_team(self, team_id):
        return self._query(Player, "team_id = ?", (team_id,), order="id")

    # -- games -------------------------------------------------------------
    def add_game(self, game): return self._insert(game)
    def get_game(self, game_id): return self._get(Game, game_id)
    def all_games(self): return self._query(Game, order="id")
    def save_game(self, game): return self._update(game)

    def game_using_ice_slot(self, slot_id):
        for g in self._query(Game, "ice_slot_id = ?", (slot_id,)):
            if not g.cancelled:
                return g
        return None

    # -- roster entries ----------------------------------------------------
    def add_roster_entry(self, entry): return self._insert(entry)
    def save_roster_entry(self, entry): return self._update(entry)

    def roster_for_game(self, game_id):
        return self._query(GameRosterEntry, "game_id = ?", (game_id,), order="id")

    def roster_entry_for_player(self, game_id, player_id):
        return self._first(GameRosterEntry, "game_id = ? AND player_id = ?",
                           (game_id, player_id))

    # -- availability ------------------------------------------------------
    def upsert_availability(self, av): return self._upsert(av)
    def save_availability(self, av): return self._upsert(av)

    def availability_for_game(self, game_id):
        return self._query(GameAvailability, "game_id = ?", (game_id,), order="id")

    def availability_for_player(self, game_id, player_id):
        return self._first(GameAvailability, "game_id = ? AND player_id = ?",
                           (game_id, player_id))

    # -- substitutes -------------------------------------------------------
    def add_substitute(self, sub): return self._insert(sub)
    def save_substitute(self, sub): return self._update(sub)

    def substitutes_for_game(self, game_id):
        return self._query(SubstituteEnrollment, "game_id = ?", (game_id,), order="id")

    def substitute_for_player(self, game_id, player_id):
        return self._first(SubstituteEnrollment, "game_id = ? AND player_id = ?",
                           (game_id, player_id))

    # -- audit / notifications --------------------------------------------
    def add_audit(self, entry): return self._insert(entry)
    def audit_for_game(self, game_id):
        return self._query(AuditLog, "game_id = ?", (game_id,), order="id")

    def add_notification(self, event): return self._insert(event)
    def notifications_for_game(self, game_id):
        return self._query(NotificationEvent, "game_id = ?", (game_id,), order="id")

    # -- organization & arena setup ---------------------------------------
    def add_league(self, league): return self._insert(league)
    def get_league(self, league_id): return self._get(League, league_id)
    def all_leagues(self): return self._query(League, order="id")

    def add_season(self, season): return self._insert(season)
    def get_season(self, season_id): return self._get(Season, season_id)
    def all_seasons(self): return self._query(Season, order="id")
    def seasons_for_league(self, league_id):
        return self._query(Season, "league_id = ?", (league_id,), order="id")

    def add_division(self, division): return self._insert(division)
    def get_division(self, division_id): return self._get(Division, division_id)
    def all_divisions(self): return self._query(Division, order="id")
    def divisions_for_season(self, season_id):
        return self._query(Division, "season_id = ?", (season_id,), order="id")

    def add_club(self, club): return self._insert(club)
    def get_club(self, club_id): return self._get(Club, club_id)
    def all_clubs(self): return self._query(Club, order="id")
    def all_teams(self): return self._query(Team, order="id")

    def add_venue(self, venue): return self._insert(venue)
    def get_venue(self, venue_id): return self._get(Venue, venue_id)
    def all_venues(self): return self._query(Venue, order="id")

    def add_rink(self, rink): return self._insert(rink)
    def get_rink(self, rink_id): return self._get(Rink, rink_id)
    def all_rinks(self): return self._query(Rink, order="id")

    def add_ice_slot(self, slot): return self._insert(slot)
    def get_ice_slot(self, slot_id): return self._get(IceSlot, slot_id)
    def all_ice_slots(self): return self._query(IceSlot, order="id")
    def save_ice_slot(self, slot): return self._update(slot)

    def add_setup_audit(self, entry): return self._insert(entry)
    def all_setup_audit(self): return self._query(SetupAuditLog, order="id")

    # -- officials (#30) ---------------------------------------------------
    def add_official(self, official): return self._insert(official)
    def get_official(self, official_id): return self._get(Official, official_id)
    def all_officials(self): return self._query(Official, order="id")

    def add_official_assignment(self, a): return self._insert(a)
    def save_official_assignment(self, a): return self._update(a)
    def get_official_assignment(self, assignment_id):
        return self._get(OfficialAssignment, assignment_id)
    def all_official_assignments(self):
        return self._query(OfficialAssignment, order="id")
    def assignments_for_game(self, game_id):
        return self._query(OfficialAssignment, "game_id = ?", (game_id,), order="id")
    def assignments_for_official(self, official_id):
        return self._query(OfficialAssignment, "official_id = ?", (official_id,),
                           order="id")
    def remove_official_assignment(self, assignment_id):
        with self._lock:
            self._exec(f"DELETE FROM {SPECS[OfficialAssignment].table} WHERE id = ?",
                       (assignment_id,))

    # -- game results (#31) ------------------------------------------------
    def add_game_result(self, result): return self._insert(result)
    def save_game_result(self, result): return self._update(result)
    def result_for_game(self, game_id):
        rows = self._query(GameResult, "game_id = ?", (game_id,), order="id")
        return rows[0] if rows else None
    def all_game_results(self): return self._query(GameResult, order="id")
