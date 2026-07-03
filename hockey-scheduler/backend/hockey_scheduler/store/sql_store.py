"""SQL-backed store (PostgreSQL target; SQLite for local/dev/tests).

Implements the same interface as :class:`InMemoryStore`. Rows are mapped to/from
the domain dataclasses via small per-table column specs. Types are kept portable
across SQLite and Postgres: TEXT/INTEGER columns only, datetimes stored as
ISO-8601 text, booleans as 0/1, and dict payloads as JSON text.
"""

import json
import os
import threading
from contextlib import contextmanager
from dataclasses import fields
from datetime import datetime, timezone
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
    ContactDestination,
    DeliveryStatus,
    DeviceToken,
    Notification,
    NotificationAudience,
    NotificationChannel,
    NotificationDelivery,
    NotificationEvent,
    NotificationKind,
    NotificationPreference,
    NotificationRecipient,
    Official,
    OfficialAssignment,
    OfficialAssignmentStatus,
    OfficialRole,
    Player,
    ResultStatus,
    Position,
    Rink,
    Role,
    RosterEntryStatus,
    RosterRole,
    Season,
    SelectionSource,
    Session,
    SetupAuditLog,
    SubstituteEnrollment,
    SubstituteStatus,
    Team,
    UserAccount,
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
    Notification: Spec(Notification, "notifications_feed",
                       {"kind": _enum(NotificationKind),
                        "audience": _enum(NotificationAudience),
                        "at": _dt()}),
    NotificationRecipient: Spec(NotificationRecipient, "notification_recipients",
                                {"read_at": _dt()}),
    NotificationDelivery: Spec(NotificationDelivery, "notification_deliveries",
                               {"channel": _enum(NotificationChannel),
                                "status": _enum(DeliveryStatus),
                                "sent_at": _dt(), "last_attempt_at": _dt(),
                                "next_attempt_at": _dt(),
                                "dead_lettered_at": _dt()}),
    ContactDestination: Spec(ContactDestination, "contact_destinations",
                             {"channel": _enum(NotificationChannel)}),
    DeviceToken: Spec(DeviceToken, "device_tokens", {"active": _bool()}),
    NotificationPreference: Spec(
        NotificationPreference, "notification_preferences",
        {"channel": _enum(NotificationChannel), "enabled": _bool()}),
    UserAccount: Spec(UserAccount, "user_accounts",
                      {"role": _enum(Role), "created_at": _dt(),
                       "scope": _jsonc(), "active": _bool()}),
    Session: Spec(Session, "sessions",
                  {"issued_at": _dt(), "expires_at": _dt(), "revoked_at": _dt()}),
}

# Numbered, forward-only migrations (#75). Each ``NNN_name.sql`` file under
# migrations/ is applied at most once, in numeric order; ``schema_migrations``
# records which versions have run and is the single source of truth. The DDL is
# CREATE ... IF NOT EXISTS so adopting this system on a pre-#75 database (which
# had all tables but no per-migration rows) is safe — the files re-run harmlessly
# and simply backfill the version ledger. No migration ever drops or rewrites
# data; a destructive rebuild is reset_schema(), demo-only and never run in prod.
_MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "migrations")


def _load_migrations():
    """Return ``[(version, [statement, ...]), ...]`` in numeric version order.

    Version is the filename stem (e.g. ``001_initial``); statements are the
    file split on ``;`` with line comments and blanks removed, so each can be
    executed individually — portable across drivers that don't run multi-
    statement strings.
    """
    out = []
    for fname in sorted(os.listdir(_MIGRATIONS_DIR)):
        if not fname.endswith(".sql"):
            continue
        version = fname[:-len(".sql")]
        with open(os.path.join(_MIGRATIONS_DIR, fname), encoding="utf-8") as fh:
            raw = fh.read()
        # Drop whole-line comments first (a comment may itself contain a ';',
        # so it must go before we split statements on ';'), then split.
        body = "\n".join(ln for ln in raw.splitlines()
                         if not ln.strip().startswith("--"))
        statements = [s.strip() for s in body.split(";") if s.strip()]
        out.append((version, statements))
    return out


def migrate(conn, dialect) -> None:
    """Apply every pending migration in order, forward only.

    ``schema_migrations`` is authoritative: a version already recorded there is
    skipped, and a version is recorded only after all of its statements succeed
    (so a partially-applied file simply re-runs next boot — safe, since the DDL
    is idempotent). Nothing here drops or mutates existing data.
    """
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TEXT)")
    cur.execute("SELECT version FROM schema_migrations")
    # Both sqlite3.Row and psycopg dict_row support key access (not positional).
    applied = {row["version"] for row in cur.fetchall()}
    for version, statements in _load_migrations():
        if version in applied:
            continue
        for stmt in statements:
            cur.execute(stmt)
        cur.execute(dialect.sql(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)"),
            (version, _utcnow().isoformat()))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SqlStore:
    def __init__(self, url: str = ":memory:"):
        self.conn, self.dialect = connect(url)
        # Store kind for the runtime status endpoint (#72): psycopg uses the
        # "pyformat" paramstyle, sqlite3 uses "qmark".
        self.backend = "postgres" if self.dialect.paramstyle == "pyformat" else "sqlite"
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

    # -- feed notifications (#32) ------------------------------------------
    def add_notification_feed(self, n): return self._insert(n)
    def save_notification_feed(self, n): return self._update(n)
    def get_notification_feed(self, notification_id):
        return self._get(Notification, notification_id)
    def all_notifications_feed(self): return self._query(Notification, order="id")

    # -- per-recipient read state (#57) ------------------------------------
    def get_notification_recipient(self, recipient_id):
        return self._get(NotificationRecipient, recipient_id)
    def save_notification_recipient(self, r): return self._upsert(r)
    def recipients_for_actor(self, actor_key):
        return self._query(NotificationRecipient, "actor_key = ?", (actor_key,),
                           order="id")

    # -- notification delivery queue (#58) ---------------------------------
    def add_notification_delivery(self, d): return self._insert(d)
    def save_notification_delivery(self, d): return self._update(d)
    def get_notification_delivery(self, delivery_id):
        return self._get(NotificationDelivery, delivery_id)
    def deliveries_for_notification(self, notification_id):
        return self._query(NotificationDelivery, "notification_id = ?",
                           (notification_id,), order="id")
    def all_notification_deliveries(self):
        return self._query(NotificationDelivery, order="id")
    def pending_deliveries(self, max_attempts):
        return self._query(
            NotificationDelivery,
            "status = ? OR (status = ? AND attempts < ?)",
            (DeliveryStatus.PENDING.value, DeliveryStatus.FAILED.value,
             max_attempts),
            order="id")

    # -- contact registry (#60) --------------------------------------------
    def add_contact_destination(self, c): return self._insert(c)
    def save_contact_destination(self, c): return self._update(c)
    def get_contact_destination(self, recipient_ref, channel):
        rows = self._query(
            ContactDestination, "recipient_ref = ? AND channel = ?",
            (recipient_ref, channel.value), order="id")
        return rows[0] if rows else None
    def all_contact_destinations(self):
        return self._query(ContactDestination, order="id")

    # -- device token registry (#65) ---------------------------------------
    def add_device_token(self, t): return self._insert(t)
    def save_device_token(self, t): return self._update(t)
    def get_device_token(self, token_id):
        return self._get(DeviceToken, token_id)
    def get_device_token_by_value(self, recipient_ref, token):
        rows = self._query(
            DeviceToken, "recipient_ref = ? AND token = ?",
            (recipient_ref, token), order="id")
        return rows[0] if rows else None
    def device_tokens_for(self, recipient_ref):
        return self._query(DeviceToken, "recipient_ref = ?",
                           (recipient_ref,), order="id")
    def active_device_token_for(self, recipient_ref):
        rows = self._query(
            DeviceToken, "recipient_ref = ? AND active = ?",
            (recipient_ref, 1), order="id")
        return rows[0] if rows else None
    def all_device_tokens(self):
        return self._query(DeviceToken, order="id")

    # -- notification preferences (#81) ------------------------------------
    def save_notification_preference(self, p):
        return self._upsert(p)
    def get_notification_preference(self, recipient_ref, channel):
        rows = self._query(
            NotificationPreference, "recipient_ref = ? AND channel = ?",
            (recipient_ref, channel.value), order="id")
        return rows[0] if rows else None
    def preferences_for_recipient(self, recipient_ref):
        return self._query(NotificationPreference, "recipient_ref = ?",
                           (recipient_ref,), order="id")

    # -- user accounts (#67) -------------------------------------------------
    def add_user_account(self, a): return self._insert(a)
    def save_user_account(self, a): return self._update(a)
    def get_user_account(self, account_id):
        return self._get(UserAccount, account_id)
    def get_user_account_by_username(self, username):
        rows = self._query(UserAccount, "username = ?", (username,), order="id")
        return rows[0] if rows else None
    def all_user_accounts(self):
        return self._query(UserAccount, order="id")

    # -- sessions (#74) ----------------------------------------------------
    def add_session(self, sess): return self._insert(sess)
    def save_session(self, sess): return self._update(sess)
    def get_session(self, session_id): return self._get(Session, session_id)
    def get_session_by_hash(self, token_hash):
        rows = self._query(Session, "token_hash = ?", (token_hash,), order="id")
        return rows[0] if rows else None
    def sessions_for_user(self, user_id):
        return self._query(Session, "user_id = ?", (user_id,), order="id")

    def delete_sessions_before(self, cutoff):
        """Delete finished sessions whose terminal time is before ``cutoff``:
        revoked sessions by revoked_at, else expired ones by expires_at. Active
        and recently-finished sessions are kept. Returns rows removed (#77)."""
        iso = cutoff.isoformat()
        with self._lock:
            cur = self._exec(
                "DELETE FROM sessions WHERE "
                "(revoked_at IS NOT NULL AND revoked_at < ?) OR "
                "(revoked_at IS NULL AND expires_at < ?)", (iso, iso))
        return cur.rowcount if cur.rowcount is not None else 0
