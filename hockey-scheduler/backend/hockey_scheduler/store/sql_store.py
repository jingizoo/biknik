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
    CalendarFeedToken,
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
    LeagueSeason,
    Program,
    ContactDestination,
    DeliveryStatus,
    DeviceToken,
    FactoryResetChallenge,
    FactoryResetEvent,
    FactoryResetLock,
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
    OfficialAvailability,
    OfficialAvailabilityStatus,
    OfficialRole,
    Organization,
    Player,
    ResultStatus,
    Position,
    RescheduleRequest,
    RescheduleStatus,
    Rink,
    Role,
    RosterEntryStatus,
    GuardianLink,
    InstallationState,
    RosterRole,
    Season,
    SeasonStatus,
    SeasonTeamRegistration,
    SeasonVenueAccess,
    TeamLeagueMigrationDecision,
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
from ..domain.errors import IntegrityConflictError
from .db import connect
from .db_errors import (
    translate_db_exception,
    translate_dependency_delete_exception,
    translate_ice_slot_conflict_exception,
    translate_player_jersey_exception,
    translate_reassignment_fk_exception,
    translate_venue_hierarchy_fk_exception,
)
from .integrity_checks import (
    assert_competition_hierarchy_reset_ready,
    assert_competition_reset_ready_c1b,
    assert_iceslot_venue_fks_ready,
    assert_reassignment_fks_ready,
    assert_regular_games_resolve_league_season,
    assert_no_duplicate_active_ice_slots,
    assert_no_duplicate_result_games,
    assert_no_duplicate_roster_players,
    assert_result_games_exist,
    assert_results_have_game,
    assert_roster_refs_exist,
    assert_venue_season_access_backfill_ready,
    assert_player_jersey_constraints_ready,
)


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
    Program: Spec(Program, "programs"),
    Season: Spec(Season, "seasons", {"start_date": _dt(), "end_date": _dt(),
                                     "status": _enum(SeasonStatus),
                                     "archived_at": _dt()}),
    League: Spec(League, "leagues"),
    LeagueSeason: Spec(LeagueSeason, "league_seasons"),
    Division: Spec(Division, "divisions"),
    SeasonTeamRegistration: Spec(
        SeasonTeamRegistration, "season_team_registrations", {"active": _bool()}),
    TeamLeagueMigrationDecision: Spec(
        TeamLeagueMigrationDecision, "team_league_migration_decisions"),
    SeasonVenueAccess: Spec(
        SeasonVenueAccess, "season_venue_access", {"active": _bool()}),
    Club: Spec(Club, "clubs"),
    Team: Spec(Team, "teams"),
    Player: Spec(Player, "players",
                 {"position": _enum(Position), "is_active": _bool()}),
    Organization: Spec(Organization, "organizations"),
    Venue: Spec(Venue, "venues"),
    Rink: Spec(Rink, "rinks"),
    IceSlot: Spec(IceSlot, "ice_slots",
                  {"start_time": _dt(), "end_time": _dt(),
                   "slot_type": _enum(IceSlotType), "status": _enum(IceSlotStatus)}),
    Game: Spec(Game, "games",
               {"start_time": _dt(), "end_time": _dt(), "roster_lock_time": _dt(),
                "locked": _bool(), "cancelled": _bool(), "published": _bool(),
                "is_draft": _bool()}),
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
    FactoryResetEvent: Spec(
        FactoryResetEvent, "factory_reset_events",
        {"started_at": _dt(), "completed_at": _dt(),
         "pre_reset_counts": _jsonc()}),
    FactoryResetChallenge: Spec(
        FactoryResetChallenge, "factory_reset_challenges",
        {"counts": _jsonc(), "expires_at": _dt(), "created_at": _dt()}),
    FactoryResetLock: Spec(
        FactoryResetLock, "factory_reset_locks",
        {"acquired_at": _dt(), "expires_at": _dt()}),
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
                             {"channel": _enum(NotificationChannel),
                              "active": _bool()}),
    DeviceToken: Spec(DeviceToken, "device_tokens", {"active": _bool()}),
    NotificationPreference: Spec(
        NotificationPreference, "notification_preferences",
        {"channel": _enum(NotificationChannel), "enabled": _bool(),
         "active": _bool()}),
    InstallationState: Spec(
        InstallationState, "installation_state",
        {"claimed_at": _dt()}),
    UserAccount: Spec(UserAccount, "user_accounts",
                      {"role": _enum(Role), "created_at": _dt(),
                       "scope": _jsonc(), "active": _bool()}),
    Session: Spec(Session, "sessions",
                  {"issued_at": _dt(), "expires_at": _dt(), "revoked_at": _dt()}),
    GuardianLink: Spec(GuardianLink, "guardian_links",
                       {"created_at": _dt(), "verified": _bool(),
                        "consented_at": _dt()}),
    RescheduleRequest: Spec(
        RescheduleRequest, "reschedule_requests",
        {"status": _enum(RescheduleStatus), "created_at": _dt(),
         "opponent_responded_at": _dt(), "league_decided_at": _dt()}),
    CalendarFeedToken: Spec(
        CalendarFeedToken, "calendar_feed_tokens",
        {"created_at": _dt(), "revoked_at": _dt(), "last_used_at": _dt()}),
    OfficialAvailability: Spec(
        OfficialAvailability, "official_availability",
        {"start_time": _dt(), "end_time": _dt(),
         "status": _enum(OfficialAvailabilityStatus)}),
}

# Numbered, forward-only migrations (#75). Each ``NNN_name.sql`` file under
# migrations/ is applied at most once, in numeric order; ``schema_migrations``
# records which versions have run and is the single source of truth. The DDL is
# CREATE ... IF NOT EXISTS so adopting this system on a pre-#75 database (which
# had all tables but no per-migration rows) is safe — the files re-run harmlessly
# and simply backfill the version ledger. No migration ever drops or rewrites
# data; a destructive rebuild is reset_schema(), demo-only and never run in prod.
_MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "migrations")

# A version given per-dialect must supply BOTH engines' files (never just one).
_DIALECT_PAIR = {"sqlite", "postgres"}


def _split_statements(raw):
    # Drop whole-line comments first (a comment may itself contain a ';', so it
    # must go before we split statements on ';'), then split.
    body = "\n".join(ln for ln in raw.splitlines()
                     if not ln.strip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


def _load_migrations(backend=None):
    """Return ``[(version, [statement, ...]), ...]`` in numeric version order.

    Version is the filename stem (e.g. ``001_initial``); statements are the file
    split on ``;`` with line comments and blanks removed, so each can be executed
    individually — portable across drivers that don't run multi-statement strings.

    A version must ship as EXACTLY ONE of:
    - a single portable ``NNN_name.sql`` (applied to both backends), or
    - a complete per-dialect pair ``NNN_name.sqlite.sql`` +
      ``NNN_name.postgres.sql`` (for DDL that can't be written portably),
      sharing the version stem ``NNN_name`` so the ``schema_migrations`` ledger
      is identical on either engine — a database only ever runs one of the two.

    Any other shape — a lone per-dialect file, or a portable file mixed with a
    per-dialect one — is a build error and raises, so a missing variant can never
    silently fall back to the *other* engine's DDL. ``backend``
    ("sqlite"/"postgres") selects which variant's statements to return.
    """
    # version -> {dialect_or_None: statements}
    by_version = {}
    for fname in sorted(os.listdir(_MIGRATIONS_DIR)):
        if not fname.endswith(".sql"):
            continue
        stem = fname[:-len(".sql")]
        dialect = None
        for suffix in (".sqlite", ".postgres"):
            if stem.endswith(suffix):
                dialect, stem = suffix[1:], stem[:-len(suffix)]
                break
        with open(os.path.join(_MIGRATIONS_DIR, fname), encoding="utf-8") as fh:
            statements = _split_statements(fh.read())
        by_version.setdefault(stem, {})[dialect] = statements

    out = []
    for version in sorted(by_version):
        variants = by_version[version]
        shape = set(variants)
        if shape == {None}:
            statements = variants[None]  # portable — both backends
        elif shape == _DIALECT_PAIR:
            # Complete pair: pick the requested backend. For a version-only
            # listing (backend is None) the statements are unused, so default to
            # SQLite deterministically rather than guessing.
            statements = variants[backend if backend in variants else "sqlite"]
        else:
            raise RuntimeError(
                f"Migration {version!r} must be a single portable .sql or a "
                f"complete .sqlite.sql + .postgres.sql pair; found "
                f"{sorted(d or 'portable' for d in shape)}. A lone per-dialect "
                f"file (or a portable/per-dialect mix) is rejected so a missing "
                f"variant can never run the other engine's DDL.")
        out.append((version, statements))
    return out


# Data validations that must pass BEFORE a given migration's statements run
# (#201): a constraint migration first reports any existing rows that would
# violate it, so an upgrade fails with the offending records named rather than
# an opaque driver error. Keyed by migration version (filename stem).
_PRE_MIGRATION_CHECKS = {
    "022_one_active_game_per_slot": assert_no_duplicate_active_ice_slots,
    "023_one_roster_row_per_player": assert_no_duplicate_roster_players,
    "024_one_result_per_game": assert_no_duplicate_result_games,
    "025_result_game_fk": assert_result_games_exist,
    "026_result_game_not_null": assert_results_have_game,
    "027_roster_entry_fks": assert_roster_refs_exist,
    "028_competition_reset": assert_competition_reset_ready_c1b,
    "029_season_venue_access": assert_venue_season_access_backfill_ready,
    "035_competition_hierarchy_reset": assert_competition_hierarchy_reset_ready,
    "037_game_league_season": assert_regular_games_resolve_league_season,
    "038_active_team_jersey_unique": assert_player_jersey_constraints_ready,
    "040_reassignment_fks": assert_reassignment_fks_ready,
    "041_iceslot_venue_fks": assert_iceslot_venue_fks_ready,
}


def migrate(conn, dialect) -> None:
    """Apply every pending migration in order, forward only.

    ``schema_migrations`` is authoritative: a version already recorded there is
    skipped, and a version is recorded only after all of its statements succeed
    (so a partially-applied file simply re-runs next boot — safe, since the DDL
    is idempotent). Nothing here drops or mutates existing data.

    A version with a registered pre-migration check (``_PRE_MIGRATION_CHECKS``)
    runs that check first; it raises (aborting the upgrade) if existing data
    would violate the constraint the migration adds.

    Each pending version's statements and its ledger row are applied as one
    atomic unit, so a migration that fails part-way (e.g. a SQLite table rebuild)
    leaves the database on the previous version rather than half-migrated.
    """
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TEXT)")
    cur.execute("SELECT version FROM schema_migrations")
    # Both sqlite3.Row and psycopg dict_row support key access (not positional).
    applied = {row["version"] for row in cur.fetchall()}
    for version, statements in _load_migrations(dialect.backend):
        if version in applied:
            continue
        check = _PRE_MIGRATION_CHECKS.get(version)
        if check is not None:
            check(conn)  # read-only; safe to run before opening the txn
        _apply_migration(conn, dialect, version, statements)


def _apply_migration(conn, dialect, version, statements) -> None:
    """Run one migration's statements plus its ledger row in a single txn."""
    def body():
        cur = conn.cursor()
        for stmt in statements:
            cur.execute(stmt)
        cur.execute(dialect.sql(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)"),
            (version, _utcnow().isoformat()))

    if dialect.backend == "postgres":
        # psycopg: group the autocommit statements. Nesting is safe — if migrate()
        # runs inside an outer transaction (e.g. the demo reset), this opens a
        # savepoint rather than a second transaction.
        with conn.transaction():
            body()
    elif conn.in_transaction:
        # Already inside a transaction (e.g. reset_schema called within the demo
        # reset's store.transaction()) — join it; SQLite can't nest BEGIN, and the
        # outer transaction already makes this migration all-or-nothing.
        body()
    else:  # sqlite (autocommit) — explicit txn so the rebuild is all-or-nothing
        try:
            conn.execute("BEGIN")
            body()
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SqlStore:
    def __init__(self, url: str = ":memory:"):
        self.conn, self.dialect, resolved_path = connect(url)
        # Store kind for the runtime status endpoint (#72): psycopg uses the
        # "pyformat" paramstyle, sqlite3 uses "qmark".
        self.backend = "postgres" if self.dialect.paramstyle == "pyformat" else "sqlite"
        # SQLite's ":memory:" (and an empty path, which sqlite3 treats as a
        # temp file deleted on close) are exactly as ephemeral as
        # InMemoryStore — readiness (#143) needs to tell them apart from a
        # genuinely durable file/Postgres, since being a SqlStore instance
        # alone doesn't guarantee that.
        self.is_memory_backed = resolved_path in (":memory:", "")
        # Reentrant: transaction() holds the lock while inner _exec re-acquires.
        self._lock = threading.RLock()
        # Transaction nesting depth (#215): a nested transaction() call joins the
        # outer one instead of opening a second (SQLite can't nest BEGIN, and we
        # want one all-or-nothing unit anyway). Lets an operation that must be
        # atomic — e.g. the demo reset's reset_schema + full reseed — wrap many
        # @_transactional service calls in a single commit/rollback.
        self._txn_depth = 0
        migrate(self.conn, self.dialect)

    @contextmanager
    def transaction(self):
        """Atomic multi-write block: commit on success, roll back on error.

        Reentrant (#215): only the outermost ``transaction()`` opens and
        commits/rolls back the real database transaction; nested calls simply
        run inside it, so the whole nest succeeds or fails as one unit.
        """
        with self._lock:
            if self._txn_depth > 0:  # already inside a transaction — just join it
                self._txn_depth += 1
                try:
                    yield
                finally:
                    self._txn_depth -= 1
                return
            self._txn_depth = 1
            try:
                try:
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
                except Exception as exc:
                    # The transaction has now rolled back, so there is zero
                    # partial state. Translate a recognized DB integrity/
                    # concurrency failure into a stable, secret-free domain
                    # error (#201 Slice 2); anything unrecognized propagates
                    # unchanged so it surfaces as an internal error rather than
                    # a misclassified user error.
                    translated = translate_db_exception(exc)
                    if translated is not None:
                        raise translated from exc
                    # A jersey unique-conflict was already translated at the
                    # write site into a stable duplicate_jersey_number error,
                    # but the racing writer couldn't name the winning holder.
                    # Now that the rollback is done and the connection is clean,
                    # look it up so the lost-race error carries the SAME
                    # conflicting-player context the service pre-check does
                    # (#292) — id + display name only, never contact data.
                    self._enrich_jersey_conflict(exc)
                    raise
            finally:
                self._txn_depth = 0

    def _enrich_jersey_conflict(self, exc) -> None:
        """Add the conflicting player to a rolled-back jersey conflict (#292).

        A no-op unless ``exc`` is the stable ``duplicate_jersey_number`` conflict
        that is still missing its holder. The lookup runs AFTER rollback (a plain
        read on a clean connection, portable across SQLite/PostgreSQL), and adds
        only ``conflicting_player_id`` + ``conflicting_player_name`` — the same
        non-private fields the service pre-check exposes.
        """
        details = getattr(exc, "details", None)
        if not isinstance(details, dict):
            return
        if details.get("reason") != "duplicate_jersey_number":
            return
        if details.get("conflicting_player_id"):
            return
        team_id, jersey = details.get("team_id"), details.get("jersey_number")
        if team_id is None or jersey is None:
            return
        holder = next(
            (p for p in self.players_for_team(team_id)
             if p.is_active and p.jersey_number == jersey), None)
        if holder is not None:
            details["conflicting_player_id"] = holder.id
            details["conflicting_player_name"] = holder.name

    def close(self) -> None:
        self.conn.close()

    # -- operational health (#90) ------------------------------------------
    def db_reachable(self) -> bool:
        try:
            with self._lock:
                cur = self.conn.cursor()
                cur.execute("SELECT 1")
                cur.fetchone()
            return True
        except Exception:
            return False

    def migration_status(self) -> dict:
        """Applied vs. expected migration versions, and whether all shipped
        migrations have been applied (#90)."""
        expected = [v for v, _ in _load_migrations()]
        try:
            with self._lock:
                cur = self.conn.cursor()
                cur.execute("SELECT version FROM schema_migrations")
                applied = sorted(r["version"] for r in cur.fetchall())
            current = all(v in applied for v in expected)
        except Exception:
            applied, current = [], False
        return {"backend": self.backend, "applied": applied,
                "expected": expected, "current": current}

    def reset_schema(self) -> None:
        """Drop all tables and re-migrate — for a clean test database.

        Foreign keys (#201) make drop order matter: dropping a parent while a
        child still references it fails. Postgres uses ``DROP ... CASCADE`` to
        also remove the dependent constraint. SQLite has no such clause, so its
        FK enforcement is suspended for the drops — but the *how* depends on
        whether a transaction is already open (the demo reset runs reset_schema
        inside one for atomicity): ``PRAGMA foreign_keys`` is a no-op mid-
        transaction, so ``defer_foreign_keys`` (which holds checks until COMMIT,
        by which point every table is gone, and resets itself there) is used
        instead; standalone, plain ``foreign_keys = OFF`` around the drops works.
        """
        cascade = " CASCADE" if self.backend == "postgres" else ""
        with self._lock:
            cur = self.conn.cursor()
            deferred = self.backend == "sqlite" and self.conn.in_transaction
            if self.backend == "sqlite":
                cur.execute("PRAGMA defer_foreign_keys = ON" if deferred
                            else "PRAGMA foreign_keys = OFF")
            try:
                for spec in SPECS.values():
                    cur.execute(f"DROP TABLE IF EXISTS {spec.table}{cascade}")
                # Legacy competition tables from a PRE-028 shape (#233 C1b): a
                # historical/abort/downgrade test can reverse migration 028 on a
                # SHARED database, leaving the umbrella `leagues`/grouping `levels`
                # collision behind. SPECS already drops the canonical `programs`
                # AND `leagues` (so the umbrella↔grouping name collision is
                # covered), but `levels` is no longer a canonical table — drop it
                # explicitly so a re-migrate rebuilds the canonical baseline
                # cleanly and no stale legacy rows persist into the next test.
                cur.execute(f"DROP TABLE IF EXISTS levels{cascade}")
                cur.execute(f"DROP TABLE IF EXISTS counters{cascade}")
                cur.execute(f"DROP TABLE IF EXISTS schema_migrations{cascade}")
            finally:
                # defer_foreign_keys resets itself at COMMIT; only the standalone
                # foreign_keys toggle needs restoring here.
                if self.backend == "sqlite" and not deferred:
                    cur.execute("PRAGMA foreign_keys = ON")
        migrate(self.conn, self.dialect)

    # Tables clear_all_data()/row_counts() never touch (#256): the durable
    # event log must survive the wipe it describes, and the challenge/lock
    # rows are orchestration state for the reset in progress, not wipeable
    # application data — the challenge is always consumed before a wipe
    # begins, and the lock is actively held for the wipe's whole duration.
    _FACTORY_RESET_SURVIVING_TABLES = frozenset({
        "factory_reset_events", "factory_reset_challenges",
        "factory_reset_locks"})

    @classmethod
    def _clearable_tables(cls):
        """Every table ``clear_all_data()``/``row_counts()`` touch — a single
        list so the two can never drift apart (#256)."""
        return [spec.table for spec in SPECS.values()
               if spec.table not in cls._FACTORY_RESET_SURVIVING_TABLES]

    def row_counts(self) -> dict:
        """Row count per table that ``clear_all_data()`` would delete (#256
        preview) — built from the exact same table list, so a preview count
        can never overstate or understate what an execute() would actually
        wipe."""
        counts = {}
        with self._lock:
            cur = self.conn.cursor()
            for table in self._clearable_tables():
                cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
                row = cur.fetchone()
                counts[table] = row["n"] if row else 0
        return counts

    def clear_all_data(self) -> None:
        """Delete every row from every table, but never drop or recreate the
        schema (#256 production factory reset) — unlike ``reset_schema()``,
        which is a demo/test-only DDL drop-and-rebuild, this preserves the
        schema and the migration ledger exactly, touching only rows.

        ``factory_reset_events`` is deliberately excluded: it is the durable
        record of the reset attempt itself and must survive the wipe it
        describes (the caller writes the outcome row after this returns).
        ``schema_migrations`` and ``counters`` are outside ``SPECS`` and so
        are never touched either — leaving ``counters`` alone means IDs
        issued after a reset are never reused, which avoids colliding with
        any pre-reset id a durable ``factory_reset_events`` row, an external
        system, or a support ticket might still reference.

        Call within ``store.transaction()`` for atomicity — same FK-ordering
        technique as ``reset_schema()`` (see its docstring), applied to
        row-level ``DELETE``/``TRUNCATE`` instead of ``DROP TABLE``.
        """
        tables = self._clearable_tables()
        with self._lock:
            cur = self.conn.cursor()
            if self.backend == "postgres":
                if tables:
                    cur.execute(f"TRUNCATE TABLE {', '.join(tables)} CASCADE")
                return
            deferred = self.conn.in_transaction
            cur.execute("PRAGMA defer_foreign_keys = ON" if deferred
                        else "PRAGMA foreign_keys = OFF")
            try:
                for table in tables:
                    cur.execute(f"DELETE FROM {table}")
            finally:
                if not deferred:
                    cur.execute("PRAGMA foreign_keys = ON")

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

    def _delete(self, model, pk):
        spec = SPECS[model]
        with self._lock:
            self._exec(f"DELETE FROM {spec.table} WHERE id = ?", (pk,))

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

    def _get_for_update(self, model, pk):
        """Like ``_get`` but takes a row lock so a concurrent delete of the same
        row is serialized (#266). On PostgreSQL this is ``SELECT ... FOR
        UPDATE``, held until the surrounding ``transaction()`` commits; on
        SQLite the connection-level write lock already serializes writers, so a
        plain read suffices. Must be called inside ``transaction()`` for the
        lock to persist across the subsequent write."""
        spec = SPECS[model]
        suffix = " FOR UPDATE" if self.backend == "postgres" else ""
        with self._lock:
            cur = self._exec(f"SELECT {', '.join(spec.names)} FROM {spec.table} "
                             f"WHERE id = ?{suffix}", (pk,))
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
    def _write_team(self, write, team):
        try:
            return write(team)
        except Exception as exc:
            # teams.club_id → clubs(id) (migration 040): a race-losing write onto
            # a concurrently-deleted club surfaces as the same stable conflict the
            # service raises when it validates the club up front (#201 Slice 2).
            translated = translate_reassignment_fk_exception(
                exc, constraint="fk_teams_club", reason="club_not_found",
                message="The change references a club that does not exist.",
                club_id=team.club_id)
            if translated is not None:
                raise translated from exc
            raise

    def add_team(self, team): return self._write_team(self._insert, team)
    def get_team(self, team_id): return self._get(Team, team_id)
    def get_team_for_update(self, team_id): return self._get_for_update(Team, team_id)
    def save_team(self, team): return self._write_team(self._update, team)
    def teams_for_program(self, program_id):
        return self._query(Team, "program_id = ?", (program_id,), order="id")
    def _write_player(self, write, player):
        try:
            return write(player)
        except Exception as exc:
            translated = translate_player_jersey_exception(
                exc, player.team_id, player.jersey_number)
            # players.team_id → teams(id) (migration 040): a race-losing write
            # onto a concurrently-deleted team surfaces as the same stable
            # conflict the service raises when it validates the team (#201 Slice 2).
            if translated is None:
                translated = translate_reassignment_fk_exception(
                    exc, constraint="fk_players_team", reason="team_not_found",
                    message="The change references a team that does not exist.",
                    team_id=player.team_id)
            if translated is not None:
                raise translated from exc
            raise

    def add_player(self, player):
        return self._write_player(self._insert, player)
    def get_player(self, player_id): return self._get(Player, player_id)
    def get_player_for_update(self, player_id):
        return self._get_for_update(Player, player_id)
    def save_player(self, player):
        return self._write_player(self._update, player)

    def players_for_team(self, team_id):
        return self._query(Player, "team_id = ?", (team_id,), order="id")

    def all_players(self):
        return self._query(Player, order="id")

    # -- games -------------------------------------------------------------
    def _write_game(self, write, game):
        try:
            return write(game)
        except Exception as exc:
            # ux_games_active_ice_slot (migration 022): a race-losing create onto
            # a slot another active game just booked — even from a different
            # Season — surfaces as the same stable ScheduleConflictError the
            # service raises via game_using_ice_slot (#201 Slice 3).
            translated = translate_ice_slot_conflict_exception(
                exc, game.ice_slot_id)
            # games.ice_slot_id → ice_slots(id) (migration 041): a race-losing
            # write onto a concurrently-deleted slot surfaces as the same stable
            # conflict the service raises when it validates the slot up front.
            if translated is None:
                translated = translate_venue_hierarchy_fk_exception(
                    exc, constraint="fk_games_ice_slot", reason="ice_slot_not_found",
                    message="The change references an ice slot that does not exist.",
                    ice_slot_id=game.ice_slot_id)
            if translated is not None:
                raise translated from exc
            raise

    def add_game(self, game): return self._write_game(self._insert, game)
    def get_game(self, game_id): return self._get(Game, game_id)
    def all_games(self): return self._query(Game, order="id")
    def save_game(self, game): return self._write_game(self._update, game)
    def delete_game(self, game_id):
        with self._lock:
            self._exec("DELETE FROM games WHERE id = ?", (game_id,))

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

    def roster_entries_for_player(self, player_id):
        return self._query(GameRosterEntry, "player_id = ?", (player_id,), order="id")

    # -- availability ------------------------------------------------------
    def upsert_availability(self, av): return self._upsert(av)
    def save_availability(self, av): return self._upsert(av)

    def availability_for_game(self, game_id):
        return self._query(GameAvailability, "game_id = ?", (game_id,), order="id")

    def availability_for_player(self, game_id, player_id):
        return self._first(GameAvailability, "game_id = ? AND player_id = ?",
                           (game_id, player_id))

    def availability_entries_for_player(self, player_id):
        return self._query(GameAvailability, "player_id = ?", (player_id,), order="id")

    # -- substitutes -------------------------------------------------------
    def add_substitute(self, sub): return self._insert(sub)
    def save_substitute(self, sub): return self._update(sub)

    def substitutes_for_game(self, game_id):
        return self._query(SubstituteEnrollment, "game_id = ?", (game_id,), order="id")

    def substitute_for_player(self, game_id, player_id):
        return self._first(SubstituteEnrollment, "game_id = ? AND player_id = ?",
                           (game_id, player_id))

    def substitute_enrollments_for_player(self, player_id):
        return self._query(SubstituteEnrollment, "player_id = ?", (player_id,), order="id")

    # -- audit / notifications --------------------------------------------
    def add_audit(self, entry): return self._insert(entry)
    def audit_for_game(self, game_id):
        return self._query(AuditLog, "game_id = ?", (game_id,), order="id")

    def add_notification(self, event): return self._insert(event)
    def notifications_for_game(self, game_id):
        return self._query(NotificationEvent, "game_id = ?", (game_id,), order="id")

    # -- organization & arena setup ---------------------------------------
    # Umbrella competition entity: Program (#233, formerly League).
    def add_program(self, program): return self._insert(program)
    def get_program(self, program_id): return self._get(Program, program_id)
    def all_programs(self): return self._query(Program, order="id")
    def save_program(self, program): return self._update(program)

    def add_season(self, season): return self._insert(season)
    def get_season(self, season_id): return self._get(Season, season_id)

    def get_season_for_update(self, season_id):
        # Row lock (#159) so concurrent archive/reopen serialize — exactly one
        # transition wins, the loser sees the stable lifecycle error.
        return self._get_for_update(Season, season_id)
    def all_seasons(self): return self._query(Season, order="id")
    def save_season(self, season): return self._update(season)
    def seasons_for_program(self, program_id):
        return self._query(Season, "program_id = ?", (program_id,), order="id")

    # Permanent competition grouping: League (#233/#283). A League is now a
    # permanent child of a Program (``program_id``), not of a Season.
    def add_league(self, league): return self._insert(league)
    def get_league(self, league_id): return self._get(League, league_id)
    def get_league_for_update(self, league_id):
        return self._get_for_update(League, league_id)
    def all_leagues(self): return self._query(League, order="id")
    def save_league(self, league): return self._update(league)
    def leagues_for_program(self, program_id):
        return self._query(League, "program_id = ?", (program_id,), order="id")

    # LeagueSeason (#283): a permanent League's participation in one Season.
    def add_league_season(self, ls): return self._insert(ls)
    def get_league_season(self, ls_id): return self._get(LeagueSeason, ls_id)
    def all_league_seasons(self): return self._query(LeagueSeason, order="id")
    def save_league_season(self, ls): return self._update(ls)
    def delete_league_season(self, ls_id): self._delete(LeagueSeason, ls_id)
    def league_seasons_for_season(self, season_id):
        return self._query(LeagueSeason, "season_id = ?", (season_id,), order="id")
    def league_seasons_for_league(self, league_id):
        return self._query(LeagueSeason, "league_id = ?", (league_id,), order="id")
    def league_season_for(self, league_id, season_id):
        rows = self._query(LeagueSeason, "league_id = ? AND season_id = ?",
                           (league_id, season_id))
        return rows[0] if rows else None

    def add_division(self, division): return self._insert(division)
    def get_division(self, division_id): return self._get(Division, division_id)
    def all_divisions(self): return self._query(Division, order="id")
    def save_division(self, division): return self._update(division)
    def divisions_for_league_season(self, league_season_id):
        return self._query(Division, "league_season_id = ?",
                           (league_season_id,), order="id")
    def divisions_for_season(self, season_id):
        """Every Division in a Season, across all its LeagueSeasons (#283
        convenience for whole-Season reads)."""
        return self._query(
            Division,
            "league_season_id IN (SELECT id FROM league_seasons WHERE season_id = ?)",
            (season_id,), order="id")

    # -- season team registrations (#180/#283) -----------------------------
    def add_season_team_registration(self, reg): return self._insert(reg)
    def get_season_team_registration(self, reg_id):
        return self._get(SeasonTeamRegistration, reg_id)
    def save_season_team_registration(self, reg): return self._update(reg)
    def all_season_team_registrations(self):
        return self._query(SeasonTeamRegistration, order="id")
    def registrations_for_league_season(self, league_season_id):
        return self._query(SeasonTeamRegistration, "league_season_id = ?",
                           (league_season_id,), order="id")
    def registration_for_team_in_league_season(self, league_season_id, team_id):
        rows = self._query(
            SeasonTeamRegistration,
            "league_season_id = ? AND team_id = ?", (league_season_id, team_id))
        return rows[0] if rows else None
    def registrations_for_season(self, season_id):
        """Every registration in a Season, across all its LeagueSeasons (#283
        convenience for whole-Season reads)."""
        return self._query(
            SeasonTeamRegistration,
            "league_season_id IN (SELECT id FROM league_seasons WHERE season_id = ?)",
            (season_id,), order="id")
    def registration_for_team_in_season(self, season_id, team_id):
        """A team's registration in a Season, across its LeagueSeasons (#283
        back-compat convenience)."""
        rows = self._query(
            SeasonTeamRegistration,
            "team_id = ? AND league_season_id IN "
            "(SELECT id FROM league_seasons WHERE season_id = ?)",
            (team_id, season_id))
        return rows[0] if rows else None

    # -- team → permanent League migration decisions (#283 migration 035) ---
    def add_team_league_migration_decision(self, decision):
        return self._insert(decision)
    def all_team_league_migration_decisions(self):
        return self._query(TeamLeagueMigrationDecision, order="id")
    def team_league_migration_decision_for(self, team_id):
        rows = self._query(TeamLeagueMigrationDecision, "team_id = ?", (team_id,))
        return rows[0] if rows else None
    def delete_team_league_migration_decision(self, decision_id):
        with self._lock:
            self._exec("DELETE FROM team_league_migration_decisions WHERE id = ?",
                       (decision_id,))

    # -- season venue access (#233 Slice E) ---------------------------------
    def _write_season_venue_access(self, write, sva):
        try:
            return write(sva)
        except Exception as exc:
            # season_venue_access.venue_id → venues(id) (migration 041): a
            # race-losing grant onto a concurrently-deleted venue surfaces as the
            # same stable conflict the service raises when it validates the venue
            # (#201 Slice 3). The season side takes the Season row lock, so only
            # the venue side needs this backstop — one FK, unambiguous on SQLite.
            translated = translate_venue_hierarchy_fk_exception(
                exc, constraint="fk_sva_venue", reason="venue_not_found",
                message="The change references a venue that does not exist.",
                venue_id=sva.venue_id)
            if translated is not None:
                raise translated from exc
            raise

    def add_season_venue_access(self, sva):
        return self._write_season_venue_access(self._insert, sva)
    def get_season_venue_access(self, sva_id):
        return self._get(SeasonVenueAccess, sva_id)
    def save_season_venue_access(self, sva):
        return self._write_season_venue_access(self._update, sva)
    def all_season_venue_access(self):
        return self._query(SeasonVenueAccess, order="id")
    def season_venue_access_for_season(self, season_id):
        return self._query(SeasonVenueAccess, "season_id = ?",
                           (season_id,), order="id")
    def season_venue_access_for_venue(self, venue_id):
        return self._query(SeasonVenueAccess, "venue_id = ?",
                           (venue_id,), order="id")
    def season_venue_access_for_pair(self, season_id, venue_id):
        rows = self._query(SeasonVenueAccess,
                           "season_id = ? AND venue_id = ?", (season_id, venue_id))
        return rows[0] if rows else None

    def add_club(self, club): return self._insert(club)
    def get_club(self, club_id): return self._get(Club, club_id)
    def get_club_for_update(self, club_id): return self._get_for_update(Club, club_id)
    def save_club(self, club): return self._update(club)
    def all_clubs(self): return self._query(Club, order="id")
    def all_teams(self): return self._query(Team, order="id")

    def add_organization(self, org): return self._insert(org)
    def get_organization(self, org_id): return self._get(Organization, org_id)
    def all_organizations(self): return self._query(Organization, order="id")
    def save_organization(self, org): return self._update(org)

    def add_venue(self, venue): return self._insert(venue)
    def get_venue(self, venue_id): return self._get(Venue, venue_id)
    def all_venues(self): return self._query(Venue, order="id")
    def save_venue(self, venue): return self._update(venue)

    def _write_rink(self, write, rink):
        try:
            return write(rink)
        except Exception as exc:
            # rinks.venue_id → venues(id) (migration 041): a race-losing create
            # onto a concurrently-deleted venue surfaces as the same stable
            # conflict the service raises when it validates the venue (#201 Slice 3).
            translated = translate_venue_hierarchy_fk_exception(
                exc, constraint="fk_rinks_venue", reason="venue_not_found",
                message="The change references a venue that does not exist.",
                venue_id=rink.venue_id)
            if translated is not None:
                raise translated from exc
            raise

    def add_rink(self, rink): return self._write_rink(self._insert, rink)
    def get_rink(self, rink_id): return self._get(Rink, rink_id)
    def all_rinks(self): return self._query(Rink, order="id")
    def save_rink(self, rink): return self._write_rink(self._update, rink)

    def _write_ice_slot(self, write, slot):
        try:
            return write(slot)
        except Exception as exc:
            # ice_slots.rink_id → rinks(id) (migration 041): a race-losing create
            # onto a concurrently-deleted rink surfaces as the same stable
            # conflict the service raises when it validates the rink (#201 Slice 3).
            translated = translate_venue_hierarchy_fk_exception(
                exc, constraint="fk_ice_slots_rink", reason="rink_not_found",
                message="The change references a rink that does not exist.",
                rink_id=slot.rink_id)
            if translated is not None:
                raise translated from exc
            raise

    def add_ice_slot(self, slot): return self._write_ice_slot(self._insert, slot)
    def get_ice_slot(self, slot_id): return self._get(IceSlot, slot_id)
    def all_ice_slots(self): return self._query(IceSlot, order="id")
    def save_ice_slot(self, slot): return self._write_ice_slot(self._update, slot)

    def add_setup_audit(self, entry): return self._insert(entry)
    def all_setup_audit(self): return self._query(SetupAuditLog, order="id")

    def add_factory_reset_event(self, event): return self._insert(event)
    def all_factory_reset_events(self):
        return self._query(FactoryResetEvent, order="started_at")

    _FACTORY_RESET_CHALLENGE_ID = "singleton"
    _FACTORY_RESET_LOCK_ID = "singleton"

    def get_factory_reset_challenge(self):
        return self._get(FactoryResetChallenge, self._FACTORY_RESET_CHALLENGE_ID)

    def set_factory_reset_challenge(self, challenge):
        """Replace the single outstanding challenge (#256 review blocker
        5) — a new preview always supersedes any prior, unconsumed one."""
        with self.transaction():
            self._upsert(challenge)
        return challenge

    def clear_factory_reset_challenge(self) -> None:
        with self.transaction():
            self._delete(FactoryResetChallenge, self._FACTORY_RESET_CHALLENGE_ID)

    def acquire_factory_reset_lock(self, lock) -> bool:
        """Try to become the sole in-progress factory reset installation-
        wide (#256 review round 1 blocker 5). A concurrent acquire attempt
        collides on the same singleton primary key; that unique-constraint
        violation is translated to IntegrityConflictError by transaction()
        (#201 Slice 2), which this catches and reports as a normal loss of
        the race rather than an unexpected error. Call
        ``release_stale_factory_reset_lock`` first so a crashed process's
        expired lock doesn't block acquisition forever (#256 review round 2
        blocker 3)."""
        try:
            with self.transaction():
                self._insert(lock)
            return True
        except IntegrityConflictError:
            return False

    def release_stale_factory_reset_lock(self, now) -> bool:
        """Reclaim the singleton lock if its lease has expired — a crashed
        process that acquired the lock and never released it would
        otherwise disable factory reset permanently (#256 review round 2
        blocker 3). Returns True if a stale lock was cleared."""
        with self.transaction():
            existing = self._get(FactoryResetLock, self._FACTORY_RESET_LOCK_ID)
            if existing is None or existing.expires_at >= now:
                return False
            self._delete(FactoryResetLock, self._FACTORY_RESET_LOCK_ID)
            return True

    def release_factory_reset_lock(self, token: str) -> None:
        """Compare-and-delete: only remove the lock if ``token`` matches the
        row currently held (#256 review round 2 blocker 3) — an unconditional
        delete would let a delayed release from a process that no longer
        holds the current lock destroy a different process's active one
        (e.g. after a stale lock was reclaimed by a new acquirer)."""
        with self.transaction():
            existing = self._get(FactoryResetLock, self._FACTORY_RESET_LOCK_ID)
            if existing is not None and existing.token == token:
                self._delete(FactoryResetLock, self._FACTORY_RESET_LOCK_ID)

    def lock_clearable_tables_for_wipe(self) -> None:
        """Acquire write-blocking locks on every table clear_all_data()
        would touch, BEFORE re-checking row_counts() against the preview
        (#256 review round 2 blocker 1) — otherwise an ordinary concurrent
        write can land on one of these tables between the recount and the
        wipe and be silently swept in without ever appearing in the
        confirmed preview. Must be called as the first statement inside the
        SAME transaction as the recount and the wipe itself, so the lock is
        held continuously from before the count until the wipe commits.

        PostgreSQL: ACCESS EXCLUSIVE table locks block every other
        transaction (including plain reads) on exactly these tables until
        this transaction ends. SQLite has no per-table locking; issuing any
        write statement inside a transaction immediately escalates SQLite's
        own connection-wide lock, which blocks every other connection from
        starting its own write until this transaction ends — a genuine
        no-op UPDATE on the lock row we already hold achieves the same
        write-blocking effect portably.
        """
        tables = self._clearable_tables()
        with self._lock:
            if self.backend == "postgres":
                if tables:
                    self._exec(f"LOCK TABLE {', '.join(tables)} IN ACCESS EXCLUSIVE MODE")
            else:
                self._exec(
                    "UPDATE factory_reset_locks SET acquired_at = acquired_at "
                    "WHERE id = ?", (self._FACTORY_RESET_LOCK_ID,))

    # -- setup-entity deletion (#215 safe destructive actions) -------------
    # Single-record hard deletes; the service runs a pre-write dependency gate
    # before calling these, so they never cascade.
    def delete_organization(self, org_id): self._delete(Organization, org_id)
    def delete_program(self, program_id): self._delete(Program, program_id)
    def delete_season(self, season_id): self._delete(Season, season_id)
    def delete_league(self, league_id): self._delete(League, league_id)
    def delete_division(self, division_id): self._delete(Division, division_id)
    def delete_club(self, club_id): self._delete(Club, club_id)
    def delete_team(self, team_id): self._delete(Team, team_id)
    def delete_season_team_registration(self, registration_id):
        self._delete(SeasonTeamRegistration, registration_id)
    def _delete_parent(self, model, entity_type, entity_id):
        # A parent delete that races behind a committed child blocks on the
        # child's FK key-share lock, then fails on the incoming reference — the
        # #201 Slice 3 facility-hierarchy backstop (rinks→venues, ice_slots→rinks,
        # games→ice_slots, season_venue_access→venues). Translate that incoming-FK
        # violation into the same stable has-dependencies block the service
        # pre-check raises, never a raw driver error or cascade.
        try:
            self._delete(model, entity_id)
        except Exception as exc:
            translated = translate_dependency_delete_exception(
                exc, entity_type=entity_type, entity_id=entity_id,
                message=f"Can't delete this {entity_type} — dependent record(s) "
                        "still exist. Remove them first.")
            if translated is not None:
                raise translated from exc
            raise

    def delete_venue(self, venue_id):
        self._delete_parent(Venue, "venue", venue_id)
    def delete_season_venue_access(self, sva_id):
        self._delete(SeasonVenueAccess, sva_id)
    def delete_rink(self, rink_id):
        self._delete_parent(Rink, "rink", rink_id)
    def delete_ice_slot(self, slot_id):
        self._delete_parent(IceSlot, "ice_slot", slot_id)
    def delete_official(self, official_id): self._delete(Official, official_id)
    def delete_player(self, player_id): self._delete(Player, player_id)

    # -- officials (#30) ---------------------------------------------------
    def add_official(self, official): return self._insert(official)
    def get_official(self, official_id): return self._get(Official, official_id)
    def get_official_for_update(self, official_id):
        return self._get_for_update(Official, official_id)
    def all_officials(self): return self._query(Official, order="id")
    def save_official(self, official): return self._update(official)

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
    def all_calendar_feed_tokens(self):
        return self._query(CalendarFeedToken, order="id")
    def all_notification_preferences(self):
        return self._query(NotificationPreference, order="id")

    # -- official availability (#88) ---------------------------------------
    def add_official_availability(self, a): return self._insert(a)
    def get_official_availability(self, avail_id):
        return self._get(OfficialAvailability, avail_id)
    def delete_official_availability(self, avail_id):
        with self._lock:
            self._exec("DELETE FROM official_availability WHERE id = ?", (avail_id,))
    def availability_for_official(self, official_id):
        return self._query(OfficialAvailability, "official_id = ?",
                           (official_id,), order="id")
    def save_official_availability(self, a): return self._update(a)

    # -- calendar feed tokens (#82) ----------------------------------------
    def add_calendar_feed_token(self, t): return self._insert(t)
    def save_calendar_feed_token(self, t): return self._update(t)
    def get_calendar_feed_token(self, token_id):
        return self._get(CalendarFeedToken, token_id)
    def get_calendar_feed_token_by_hash(self, token_hash):
        rows = self._query(CalendarFeedToken, "token_hash = ?", (token_hash,),
                           order="id")
        return rows[0] if rows else None
    def calendar_feed_tokens_for(self, actor_type, actor_ref):
        return self._query(CalendarFeedToken,
                           "actor_type = ? AND actor_ref = ?",
                           (actor_type, actor_ref), order="id")

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

    # -- installation claim state (#174) -----------------------------------
    def add_installation_state(self, state): return self._insert(state)
    def get_installation_state(self, state_id):
        return self._get(InstallationState, state_id)

    # -- user accounts (#67) -------------------------------------------------
    def add_user_account(self, a): return self._insert(a)
    def save_user_account(self, a): return self._update(a)
    def get_user_account(self, account_id):
        return self._get(UserAccount, account_id)
    def get_user_account_for_update(self, account_id):
        # Row-locked read (#266 review): set_active/rebind read-modify-write the
        # whole row, so a concurrent mutation of the same account would otherwise
        # lost-update (e.g. a rebind clobbering a deactivation). Locking here
        # serializes them on the account row within each transaction().
        return self._get_for_update(UserAccount, account_id)
    def get_user_account_by_username(self, username):
        rows = self._query(UserAccount, "username = ?", (username,), order="id")
        return rows[0] if rows else None
    def all_user_accounts(self):
        return self._query(UserAccount, order="id")

    # -- guardian ↔ junior links (#26) -------------------------------------
    def add_guardian_link(self, link): return self._insert(link)
    def save_guardian_link(self, link): return self._update(link)
    def get_guardian_link(self, link_id):
        return self._get(GuardianLink, link_id)
    def guardian_links_for(self, guardian_user_id):
        return self._query(GuardianLink, "guardian_user_id = ?",
                           (guardian_user_id,), order="id")
    def guardian_link_for(self, guardian_user_id, player_id):
        rows = self._query(GuardianLink,
                           "guardian_user_id = ? AND player_id = ?",
                           (guardian_user_id, player_id), order="id")
        return rows[0] if rows else None
    def guardian_links_for_player(self, player_id):
        return self._query(GuardianLink, "player_id = ?", (player_id,), order="id")
    def all_guardian_links(self):
        return self._query(GuardianLink, order="id")

    # -- reschedule requests (#29) ------------------------------------------
    def add_reschedule_request(self, r): return self._insert(r)
    def save_reschedule_request(self, r): return self._update(r)
    def get_reschedule_request(self, request_id):
        return self._get(RescheduleRequest, request_id)
    def reschedule_requests_for_game(self, game_id):
        return self._query(RescheduleRequest, "game_id = ?", (game_id,), order="id")
    def all_reschedule_requests(self):
        return self._query(RescheduleRequest, order="id")

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
