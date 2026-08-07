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
    SchedulingPolicy,
    PolicyScopeType,
    ActiveContext,
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
    ScheduleScenario,
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
    DependentDeleteConflict,
    dependent_delete_conflict,
    translate_db_exception,
    translate_ice_slot_conflict_exception,
    translate_ice_slot_time_conflict_exception,
    translate_player_jersey_exception,
    translate_program_org_fk_exception,
    translate_reassignment_fk_exception,
    translate_venue_hierarchy_fk_exception,
)
from .integrity_checks import (
    assert_competition_hierarchy_reset_ready,
    assert_competition_reset_ready_c1b,
    assert_iceslot_venue_fks_ready,
    assert_program_org_fks_ready,
    assert_reassignment_fks_ready,
    assert_regular_games_resolve_league_season,
    assert_no_duplicate_active_ice_slots,
    assert_no_duplicate_ice_slot_times,
    assert_no_duplicate_result_games,
    assert_no_duplicate_roster_players,
    assert_no_duplicate_rink_external_refs,
    assert_officials_availability_import_constraints_ready,
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
    # Child of the permanent scheduling hierarchy; keep it before its parents
    # so SQLite factory-reset deletion follows child-first FK order.
    ScheduleScenario: Spec(
        ScheduleScenario, "schedule_scenarios",
        {"request_input": _jsonc(), "proposal": _jsonc(),
         "generation_snapshot": _jsonc(), "created_at": _dt()}),
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
    SchedulingPolicy: Spec(SchedulingPolicy, "scheduling_policies",
                           {"scope_type": _enum(PolicyScopeType)}),
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
    ActiveContext: Spec(ActiveContext, "user_active_context",
                        {"updated_at": _dt()}),
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

# Per-transaction isolation levels a caller may request (SQL is interpolated, so
# this whitelist is the injection guard — never format an arbitrary string in).
_ISOLATION_LEVELS = frozenset({"SERIALIZABLE", "REPEATABLE READ"})

# Strength order, so a NESTED transaction() can tell "I need at least X" from "I
# need exactly X" (#369). A join cannot RAISE the isolation of an already-open
# transaction — that is still a programming error — but joining one that is
# already at least as strong SATISFIES the request, which is what lets an
# outer guard adopt the strongest level any participant needs and then call the
# inner services that each ask for their own. ``None`` is the driver default
# (READ COMMITTED on PostgreSQL), the weakest rung.
_ISOLATION_RANK = {None: 0, "REPEATABLE READ": 1, "SERIALIZABLE": 2}


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
    "042_program_org_fks": assert_program_org_fks_ready,
    "045_ice_slot_unique_time": assert_no_duplicate_ice_slot_times,
    "047_official_import_unique_keys":
        assert_officials_availability_import_constraints_ready,
    "048_rink_external_ref_unique": assert_no_duplicate_rink_external_refs,
}


def migrate(conn, dialect) -> None:
    """Apply every pending migration in order, forward only.

    ``schema_migrations`` is authoritative: a version already recorded there is
    skipped, and a version is recorded only after all of its statements succeed
    (so a partially-applied file simply re-runs next boot). Migrations are
    forward-only and vary in kind: most add columns or indexes; some backfill or
    transform existing row values; and — because SQLite cannot add a foreign key
    to an existing table — an FK migration on SQLite rebuilds the affected tables
    (create-copy-drop-rename), which drops and physically rewrites data while
    preserving each row's values. Every such change is applied inside the
    migration's single transaction (see ``_apply_migration``), so it is
    all-or-nothing; it is never an in-place no-op. Take a backup before upgrading.

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
        # A migration may rebuild a table that is REFERENCED by a foreign key
        # (create-copy-drop-rename; e.g. migration 040 rebuilds players, 041
        # rebuilds games). PRAGMA foreign_keys is a no-op inside a transaction,
        # and PRAGMA defer_foreign_keys does NOT clear the deferred violations
        # that dropping a still-referenced parent (with existing child rows)
        # registers — so on a populated upgrade the COMMIT would fail even though
        # the final state is consistent. Enforcement is therefore suspended the
        # SQLite-recommended way — foreign_keys = OFF, set BEFORE BEGIN — and a
        # foreign_key_check inside the same transaction proves the result is clean
        # before COMMIT re-enables it, so a genuinely inconsistent rebuild still
        # fails loudly and rolls back. (Fresh/empty databases are unaffected; this
        # matters only when the table already holds child rows. The nested-txn
        # branch above keeps using the migration file's PRAGMA defer_foreign_keys,
        # which is sound there because the demo reset re-migrates emptied tables.)
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            # IMMEDIATE for the same reason transaction() uses it (#392): take
            # the write lock at BEGIN so no statement inside ever has to
            # promote SHARED->RESERVED, the one acquisition SQLite refuses to
            # run the busy handler for.
            #
            # This was very nearly left as a plain BEGIN on the argument that a
            # migration's first statement is a write anyway. That argument was
            # FALSE — migrations 040, 041 and 042 all open with
            # `PRAGMA defer_foreign_keys = ON` — and, worse, it was the wrong
            # test. What decides the promotion is whether the first statement
            # takes a READ LOCK, not whether it is a write, and the two are not
            # the same question. Measured on one file with a 5000ms
            # busy_timeout, against a holder of RESERVED:
            #
            #   BEGIN; <no statement>; UPDATE    locked after 5.34s  honoured
            #   BEGIN; PRAGMA defer_foreign_keys; UPDATE
            #                                    locked after 5.39s  honoured
            #   BEGIN; SELECT; UPDATE            locked after 0.000s BYPASSED
            #   BEGIN; PRAGMA table_info; UPDATE locked after 0.000s BYPASSED
            #   BEGIN IMMEDIATE; UPDATE          locked after 5.37s  honoured
            #
            # Two PRAGMAs, opposite answers, and nothing in either one's
            # spelling says which. An invariant that subtle, enforced only by
            # prose and re-checked by hand every time a migration is authored,
            # is one `PRAGMA table_info` away from reproducing this exact bug.
            # IMMEDIATE deletes the invariant instead of documenting it, and it
            # is free: interleaved cold builds of the full migration set, 32
            # samples each, median 34.90ms plain vs 35.00ms IMMEDIATE.
            conn.execute("BEGIN IMMEDIATE")
            body()
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(
                    f"migration {version} left {len(violations)} foreign-key "
                    "violation(s); rolling back")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SqlStore:
    def __init__(self, url: str = ":memory:"):
        self.conn, self.dialect, resolved_path = connect(url)
        # Kept so a connection the DATABASE closed can be re-established
        # (#404); see _reconnect_if_lost. Never logged or exposed — it can
        # carry credentials.
        self._url = url
        # Latched by close(): a deliberately closed store never reconnects.
        self._closed = False
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
        # Isolation the OPEN outermost transaction actually holds (#369), so a
        # nested join can be admitted when what it asks for is already
        # guaranteed and refused when it would need more. None outside a
        # transaction, and outside one it is meaningless.
        self._txn_isolation = None
        # Re-resolver for the no-row-lock parent-delete FK race (#201 Slice 3),
        # set by the service via set_dependent_conflict_resolver. Invoked by the
        # OUTERMOST transaction() only after rollback, so the itemised
        # has-dependencies re-scan always runs on a clean connection — even when
        # the losing delete was nested inside a caller's transaction() and the
        # connection was transaction-aborted mid-flight.
        self._dependent_conflict_resolver = None
        migrate(self.conn, self.dialect)

    def set_dependent_conflict_resolver(self, resolver) -> None:
        """Register the callback that turns a DependentDeleteConflict (a
        no-row-lock parent delete that lost to a concurrently-committed child,
        #201 Slice 3) into the itemised has-dependencies domain error.

        The store cannot itemise dependents itself — that is service logic
        (display names, group types) — so it calls back into the service, but
        only from the outermost transaction()'s post-rollback handler where the
        connection is guaranteed clean. Doing the re-scan any earlier (e.g. in
        the service before the outer rollback) would read on a transaction-
        aborted connection and raise InFailedSqlTransaction.
        """
        self._dependent_conflict_resolver = resolver

    @contextmanager
    def transaction(self, isolation=None):
        """Atomic multi-write block: commit on success, roll back on error.

        Reentrant (#215): only the outermost ``transaction()`` opens and
        commits/rolls back the real database transaction; nested calls simply
        run inside it, so the whole nest succeeds or fails as one unit.

        ``isolation`` (``"SERIALIZABLE"`` / ``"REPEATABLE READ"``) raises the
        isolation of **this one** PostgreSQL transaction — a narrow, per-call
        snapshot, never a global connection change — so all its reads observe a
        single consistent snapshot (used by the context selector so an
        authorization computation can't straddle a concurrent scope revocation,
        #159). A nested join can never RAISE the isolation of the already-open
        transaction, so asking for MORE than is currently held is a programming
        error and raises. Asking for the same level or LESS is satisfied by the
        open transaction and simply joins it (#369): the atomic setup guard opens
        one SERIALIZABLE transaction around an authorization computation that
        itself asks for SERIALIZABLE (the context selector) and REPEATABLE READ
        (the target chain walk), and both are already guaranteed by the outer
        one. On SQLite it is a no-op: ``self._lock`` serializes every
        transaction taken through THIS store, and the ``BEGIN IMMEDIATE``
        below serializes this connection against any other one on the same
        file — together that is already strictly stronger than SERIALIZABLE.
        """
        if isolation is not None and isolation not in _ISOLATION_LEVELS:
            raise ValueError(f"unsupported isolation level: {isolation!r}")
        with self._lock:
            if self._txn_depth > 0:  # already inside a transaction — just join it
                if (_ISOLATION_RANK[isolation]
                        > _ISOLATION_RANK[self._txn_isolation]):
                    raise RuntimeError(
                        "transaction(isolation=...) must be the outermost "
                        "transaction; a nested join cannot raise the isolation "
                        f"of the open one ({self._txn_isolation!r} < "
                        f"{isolation!r})")
                self._txn_depth += 1
                try:
                    yield
                finally:
                    self._txn_depth -= 1
                return
            self._txn_depth = 1
            self._txn_isolation = isolation
            try:
                try:
                    if self.dialect.paramstyle == "pyformat":  # psycopg manages it
                        with self.conn.transaction():
                            if isolation is not None:
                                # First statement in the txn (after BEGIN), before
                                # any read — required for SET TRANSACTION to apply.
                                self.conn.execute(
                                    "SET TRANSACTION ISOLATION LEVEL " + isolation)
                            yield
                    else:  # sqlite (autocommit) — explicit txn (isolation no-op)
                        try:
                            # IMMEDIATE, not DEFERRED: take the database's
                            # write lock as this transaction's FIRST statement.
                            #
                            # SQLite has no row locks, so `_lock_setup_row` is
                            # a no-op read here and the file's write lock is
                            # the only analogue there is. Under a plain BEGIN
                            # the unit therefore held nothing but a SHARED read
                            # lock all the way through its authorization phase
                            # and only reached for the write lock at its first
                            # UPDATE — a SHARED->RESERVED promotion. That is
                            # the one acquisition SQLite deliberately refuses
                            # to run the busy handler for: promoting while
                            # another connection already holds RESERVED is a
                            # potential deadlock, so it returns SQLITE_BUSY
                            # *immediately* and the connection's 5s
                            # busy_timeout never applies. The write failed
                            # instantly with "database is locked", which the
                            # translator turns into the retryable
                            # lock_not_available conflict — a guarded mutation
                            # losing a lock it was authorized to take, with no
                            # wait attempted at all.
                            #
                            # THE BUG HAD TWO WIRE SYMPTOMS, and only one of
                            # them looks like a lock problem. Write it down
                            # here so the second is never chased as a separate
                            # defect. The 409 above is what happens when the
                            # bounded retries run out. But a retry that loses
                            # the promotion ROLLS BACK — and if the concurrent
                            # mover's write commits in that gap, attempt 2
                            # re-authorizes against the new truth, correctly
                            # finds the row outside the caller's scope, and
                            # renders a perfectly generic
                            #
                            #   404 {"code": "not_found",
                            #        "message": "Venue venue_2 not found."}
                            #
                            # (also "Player player_1 not found." and
                            # "Registration streg_1 not found." — it appears in
                            # all three *_lock_sqlite_file tests). Nothing in
                            # that response mentions locking. It reads like a
                            # lost write or a scope bug, and it is neither: the
                            # refusal itself is correct, it is the rollback
                            # that should never have happened. Measured on the
                            # unfixed tree, two independent samples: 648
                            # invocations -> 8x 409 / 3x 404, and 756
                            # invocations -> 1x 409 / 7x 404. Roughly half the
                            # failures wear the 404 face, and which one shows
                            # is pure timing.
                            #
                            # Acquiring at BEGIN fixes the cause rather than
                            # the symptom: the unit holds the write lock across
                            # authorize -> mutate -> commit (which is what
                            # makes the SQLite no-op row lock sound), no
                            # statement inside it ever has to promote, and a
                            # genuinely contended writer now blocks in the busy
                            # handler at BEGIN — where SQLite *does* honour the
                            # timeout — instead of failing on contact.
                            self.conn.execute("BEGIN IMMEDIATE")
                            yield
                            self.conn.commit()
                        except Exception:
                            self.conn.rollback()
                            raise
                except Exception as exc:
                    # The transaction has now rolled back, so there is zero
                    # partial state and the connection is clean again.
                    #
                    # A no-row-lock parent delete (venue/rink/ice-slot, #201
                    # Slice 3) that lost the FK race surfaces here as a
                    # DependentDeleteConflict: a child committed between the
                    # pre-check and the DELETE, so the DB rejected the DELETE and
                    # aborted the connection mid-transaction. Re-resolve the
                    # now-committed dependents into the SAME itemised
                    # has-dependencies error the pre-check raises. This runs at
                    # the OUTERMOST boundary AFTER rollback, so it is correct
                    # whether the delete ran on its own or nested inside a
                    # caller's transaction() — re-scanning inside the still-
                    # aborted transaction would raise InFailedSqlTransaction.
                    self._resolve_dependent_delete_conflict(exc)
                    # Translate a recognized DB integrity/concurrency failure
                    # into a stable, secret-free domain error (#201 Slice 2);
                    # anything unrecognized propagates unchanged so it surfaces
                    # as an internal error rather than a misclassified user error.
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
                self._txn_isolation = None

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

    def _resolve_dependent_delete_conflict(self, exc) -> None:
        """Post-rollback re-resolution of a lost no-row-lock parent delete (#201
        Slice 3). A no-op unless ``exc`` (or its cause chain) is a
        ``DependentDeleteConflict`` and a resolver is registered.

        When it fires it hands the conflict to the service-registered resolver,
        which raises the itemised has-dependencies error (or a stable retry
        conflict) — so the caller sees the SAME structured error whether the
        dependent was present at pre-check or committed during the race. Runs
        only from the outermost transaction()'s handler, i.e. after rollback, so
        the resolver's fresh dependent scan reads on a clean connection.
        """
        if self._dependent_conflict_resolver is None:
            return
        conflict = exc
        while conflict is not None and not isinstance(
                conflict, DependentDeleteConflict):
            conflict = getattr(conflict, "__cause__", None)
        if conflict is None:
            return
        # Raises the itemised domain error; if the resolver can't itemise this
        # entity type it returns and the original DependentDeleteConflict
        # propagates (surfacing as an internal error, never a partial delete).
        self._dependent_conflict_resolver(conflict)

    def close(self) -> None:
        """Release the connection for good.

        Latches ``_closed`` so #404 recovery can never resurrect this store.
        A store closed on PURPOSE — the superseded one after a demo reset, a
        half-built one after a failed reset — must stay closed; reconnecting
        it would leak a connection and hand out a live handle to something the
        caller has finished with. That is a different event from a connection
        the DATABASE killed, and only the latter is recoverable.
        """
        self._closed = True
        self.conn.close()

    # -- operational health (#90) ------------------------------------------
    def db_reachable(self) -> bool:
        """Whether the database can actually be reached RIGHT NOW.

        Attempts the #404 recovery first, deliberately: this is what a
        platform health check polls, so a connection the database closed but
        which can be re-established should report reachable and let the
        instance keep serving. Only a database that is genuinely gone — where
        ``connect()`` itself fails — reports False, which is exactly when a
        restart is the right response.
        """
        try:
            self._reconnect_if_lost()
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
    def _reconnect_if_lost(self):
        """Re-establish a connection the DATABASE closed (#404).

        The process holds exactly one connection with no pool. When it dies —
        a managed-Postgres restart, failover, maintenance window, idle reaper,
        network blip — every store-touching request failed PERMANENTLY until
        someone restarted the process, while ``/api/health`` kept answering
        200 so nothing did.

        Three deliberate limits:

        * ``conn.closed`` is the oracle, never a string match on driver text.
          psycopg sets it for a dead connection and leaves it False for an
          ordinary SQL error — so a failing QUERY never reconnects, which
          would otherwise mask real bugs and silently drop session state.
        * Never inside a transaction. Swapping the connection there would
          discard the writes already made in it and let the rest commit on a
          fresh one, turning an outage into corruption. The transaction fails,
          as it must; the NEXT request outside one recovers.
        * The failed statement is NOT retried here. In autocommit a write may
          have committed server-side just before the socket died, so a silent
          retry could double-apply it. One request pays for the outage (#403
          renders it as a structured 500) and the rest are served.

        SQLite is excluded: it has no ``closed`` attribute and a local file
        connection does not die this way. There is nothing to recover from.
        A store closed deliberately via :meth:`close` is excluded too — see
        there for why that is a different event.
        """
        if self.backend != "postgres" or self._closed:
            return
        with self._lock:                       # RLock: safe to re-acquire
            # Re-checked under the lock: a concurrent caller may have healed
            # it already, and reconnecting twice would strand a live socket.
            if self._txn_depth > 0 or not getattr(self.conn, "closed", False):
                return
            self.conn, self.dialect, _ = connect(self._url)

    def _exec(self, query, params=()):
        self._reconnect_if_lost()
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
    def get_game_for_update(self, game_id):
        return self._get_for_update(Game, game_id)
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
    def _write_program(self, write, program):
        try:
            return write(program)
        except Exception as exc:
            # programs.operator_organization_id → organizations(id) (migration
            # 042): a race-losing create/update onto a concurrently-deleted
            # organization surfaces as the same stable conflict the service raises
            # when it validates the organization (#201 Slice 4).
            translated = translate_program_org_fk_exception(
                exc, constraint="fk_programs_operator_org",
                reason="organization_not_found",
                message="The change references an organization that does not exist.",
                organization_id=program.operator_organization_id)
            if translated is not None:
                raise translated from exc
            raise

    def add_program(self, program): return self._write_program(self._insert, program)
    def get_program(self, program_id): return self._get(Program, program_id)

    def get_program_for_update(self, program_id):
        # Row lock (#158) so ice-availability commit reads the Program timezone
        # under the same lock a concurrent import's timezone update contends for,
        # keeping the generated calendar windows consistent with committed state.
        return self._get_for_update(Program, program_id)
    def all_programs(self): return self._query(Program, order="id")
    def save_program(self, program): return self._write_program(self._update, program)

    def _write_season(self, write, season):
        try:
            return write(season)
        except Exception as exc:
            # seasons.program_id → programs(id) (migration 042): a race-losing
            # create onto a concurrently-deleted program surfaces as the same
            # stable conflict the service raises when it validates the program.
            translated = translate_program_org_fk_exception(
                exc, constraint="fk_seasons_program", reason="program_not_found",
                message="The change references a program that does not exist.",
                program_id=season.program_id)
            if translated is not None:
                raise translated from exc
            raise

    def add_season(self, season): return self._write_season(self._insert, season)
    def get_season(self, season_id): return self._get(Season, season_id)

    def get_season_for_update(self, season_id):
        # Row lock (#159) so concurrent archive/reopen serialize — exactly one
        # transition wins, the loser sees the stable lifecycle error.
        return self._get_for_update(Season, season_id)
    def all_seasons(self): return self._query(Season, order="id")
    def save_season(self, season): return self._write_season(self._update, season)
    def seasons_for_program(self, program_id):
        return self._query(Season, "program_id = ?", (program_id,), order="id")

    # Permanent competition grouping: League (#233/#283). A League is now a
    # permanent child of a Program (``program_id``), not of a Season.
    def _write_league(self, write, league):
        try:
            return write(league)
        except Exception as exc:
            # leagues.program_id → programs(id) (migration 042): a race-losing
            # create onto a concurrently-deleted program surfaces as the same
            # stable conflict the service raises when it validates the program.
            translated = translate_program_org_fk_exception(
                exc, constraint="fk_leagues_program", reason="program_not_found",
                message="The change references a program that does not exist.",
                program_id=league.program_id)
            if translated is not None:
                raise translated from exc
            raise

    def add_league(self, league): return self._write_league(self._insert, league)
    def get_league(self, league_id): return self._get(League, league_id)
    def get_league_for_update(self, league_id):
        return self._get_for_update(League, league_id)
    def all_leagues(self): return self._query(League, order="id")
    def save_league(self, league): return self._write_league(self._update, league)
    def leagues_for_program(self, program_id):
        return self._query(League, "program_id = ?", (program_id,), order="id")

    # LeagueSeason (#283): a permanent League's participation in one Season.
    def add_league_season(self, ls): return self._insert(ls)
    def get_league_season(self, ls_id): return self._get(LeagueSeason, ls_id)
    def get_league_season_for_update(self, ls_id):
        return self._get_for_update(LeagueSeason, ls_id)
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
    def get_division_for_update(self, division_id):
        return self._get_for_update(Division, division_id)
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
    def get_season_team_registration_for_update(self, reg_id):
        return self._get_for_update(SeasonTeamRegistration, reg_id)
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
    def registrations_for_team_in_league_season(self, league_season_id, team_id):
        """Every row at this EXACT (team, LeagueSeason) key (#331 review
        round 19) -- always 0 or 1 here, since ``ux_team_league_season``
        (migration 035) makes a second impossible to insert, but returning a
        list gives InMemoryStore's own (unenforced) equivalent a uniform
        contract callers can share."""
        return self._query(
            SeasonTeamRegistration,
            "league_season_id = ? AND team_id = ?", (league_season_id, team_id))
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
    def get_season_venue_access_for_update(self, sva_id):
        return self._get_for_update(SeasonVenueAccess, sva_id)
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
    def get_organization_for_update(self, org_id):
        return self._get_for_update(Organization, org_id)
    def all_organizations(self): return self._query(Organization, order="id")
    def save_organization(self, org): return self._update(org)

    def _write_venue(self, write, venue):
        try:
            return write(venue)
        except Exception as exc:
            # venues carries TWO outgoing foreign keys (migration 042):
            # organization_id → organizations(id) and league_id → programs(id).
            # A race-losing create/update onto a concurrently-deleted parent must
            # surface the same stable conflict the service raises when it
            # validates that parent (organization_not_found / program_not_found).
            org_hit = translate_program_org_fk_exception(
                exc, constraint="fk_venues_organization",
                reason="organization_not_found",
                message="The change references an organization that does not exist.",
                organization_id=venue.organization_id)
            prog_hit = translate_program_org_fk_exception(
                exc, constraint="fk_venues_program", reason="program_not_found",
                message="The change references a program that does not exist.",
                program_id=venue.league_id)
            # PostgreSQL matched exactly one by constraint name. SQLite matched
            # both (its message names no constraint), so disambiguate by which
            # validated parent is actually missing now — a plain read is safe
            # here (a SQLite constraint failure rolls back only the statement, not
            # the transaction) and, because SQLite serializes writers, the racing
            # delete has already committed and is visible.
            if org_hit is not None and prog_hit is not None:
                if (venue.organization_id is not None
                        and self.get_organization(venue.organization_id) is None):
                    raise org_hit from exc
                if (venue.league_id is not None
                        and self.get_program(venue.league_id) is None):
                    raise prog_hit from exc
                raise  # neither parent missing — not our FK, re-raise unchanged
            if org_hit is not None:
                raise org_hit from exc
            if prog_hit is not None:
                raise prog_hit from exc
            raise

    def add_venue(self, venue): return self._write_venue(self._insert, venue)
    def get_venue(self, venue_id): return self._get(Venue, venue_id)
    def get_venue_for_update(self, venue_id):
        return self._get_for_update(Venue, venue_id)
    def all_venues(self): return self._query(Venue, order="id")
    def save_venue(self, venue): return self._write_venue(self._update, venue)

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

    def get_rink_for_update(self, rink_id):
        # Row-lock a rink so every ice-slot write on it serializes (#158 review):
        # commit_ice_availability and create_ice_slot both take this before
        # checking/writing, so two writers can't both pass an overlap/duplicate
        # check and then both insert. Must run inside transaction().
        return self._get_for_update(Rink, rink_id)

    def _write_ice_slot(self, write, slot):
        try:
            return write(slot)
        except Exception as exc:
            # One physical slot per (rink, start, end) (migration 045): a
            # race-losing INSERT is an idempotent duplicate, translated to a
            # stable conflict rather than a raw driver error (#158 review).
            dup = translate_ice_slot_time_conflict_exception(exc, slot.rink_id)
            if dup is not None:
                raise dup from exc
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
    def get_ice_slot_for_update(self, slot_id):
        return self._get_for_update(IceSlot, slot_id)
    def all_ice_slots(self): return self._query(IceSlot, order="id")
    def save_ice_slot(self, slot): return self._write_ice_slot(self._update, slot)

    def add_scheduling_policy(self, policy): return self._insert(policy)
    def get_scheduling_policy(self, policy_id):
        return self._get(SchedulingPolicy, policy_id)
    def save_scheduling_policy(self, policy): return self._update(policy)
    def delete_scheduling_policy(self, policy_id):
        self._delete(SchedulingPolicy, policy_id)
    def all_scheduling_policies(self):
        return self._query(SchedulingPolicy, order="id")

    # -- immutable schedule scenarios (#378) -----------------------------
    def add_schedule_scenario(self, scenario): return self._insert(scenario)
    def get_schedule_scenario(self, scenario_id):
        return self._get(ScheduleScenario, scenario_id)
    def get_schedule_scenario_for_update(self, scenario_id):
        return self._get_for_update(ScheduleScenario, scenario_id)
    def all_schedule_scenarios(self):
        return self._query(ScheduleScenario, order="id")
    def find_scheduling_policy(self, scope_type, scope_id):
        return self._first(SchedulingPolicy, "scope_type = ? AND scope_id = ?",
                           (getattr(scope_type, "value", scope_type), scope_id))

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
    def delete_organization(self, org_id):
        self._delete_parent(Organization, "organization", org_id)
    def delete_program(self, program_id):
        self._delete_parent(Program, "program", program_id)
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
        # games→ice_slots, season_venue_access→venues). Signal that incoming-FK
        # violation as a DependentDeleteConflict; the service catches it, rolls
        # back, re-resolves the now-committed dependents, and raises the SAME
        # itemised has-dependencies error its pre-check raises — never a raw
        # driver error or cascade.
        try:
            self._delete(model, entity_id)
        except Exception as exc:
            conflict = dependent_delete_conflict(
                exc, entity_type=entity_type, entity_id=entity_id)
            if conflict is not None:
                raise conflict from exc
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

    # -- per-user active Program/Season context (#159) ---------------------
    #
    # The lock keyspace for the per-user context mutex (#386 re-review). An
    # arbitrary but STABLE namespace, so these advisory locks can never
    # collide with another feature's.
    _ACTIVE_CONTEXT_LOCK_NAMESPACE = 0x4143      # "AC"

    def _lock_active_context_mutex(self, user_id):
        """Take the per-user ActiveContext MUTEX for this transaction (#386).

        ``SELECT ... FOR UPDATE`` on ``user_active_context`` is not enough on
        its own, and the reason is exactly the owner's: **an absent-row read is
        not a lock**. A user who has never saved a selection has no row, so the
        authorizing transaction's ``FOR UPDATE`` locks nothing, and all it holds
        against a concurrent FIRST ``INSERT`` is a single read->write
        anti-dependency — which SERIALIZABLE is not obliged to abort. A brand
        new operator could therefore authorize from fallback tuple A, make
        their first saved selection B concurrently, and have BOTH the B
        selection and the A Game writes commit.

        A PostgreSQL transaction-scoped ADVISORY lock has the one property a
        row lock cannot have here: it exists before the row does. It is keyed
        on the user id, needs nothing to be present, and is released
        automatically at commit or rollback, so no cleanup path can leak it.
        Both sides take it — the mutating authorization and
        ``set_active_context`` — which is what makes it a mutex rather than a
        hint.

        ``hashtext`` collisions are possible and harmless: two unrelated users
        would merely serialize against each other. Correctness never depends on
        the key being unique, only on it being the SAME for one user.

        A falsy ``user_id`` takes nothing. Such a caller (the identity-less
        X-Demo-Role fallback) can never own a row — ``set_active_context``
        refuses without a user id — so there is no first insert to race.

        SQLite is a documented no-op, and it takes TWO mechanisms to say why —
        the old wording, "the process-wide lock", named a thing that does not
        exist. ``self._lock`` is a per-INSTANCE ``RLock``: it serializes every
        transaction taken through THIS store object and nothing else. What
        covers the rest is ``transaction()``'s ``BEGIN IMMEDIATE`` (#392),
        which holds the database file's write lock against every OTHER
        connection for the whole block. Together they are strictly stronger
        than this advisory lock.
        """
        if not user_id or self.backend != "postgres":
            return
        self._exec("SELECT pg_advisory_xact_lock(?, hashtext(?))",
                   (self._ACTIVE_CONTEXT_LOCK_NAMESPACE, user_id))

    @contextmanager
    def active_context_mutex(self, user_id):
        """Hold the per-user context mutex ACROSS a whole mutation unit (#386).

        The transaction-scoped lock alone cannot order this, and the reason is
        exact: PostgreSQL fixes a SERIALIZABLE transaction's view at its FIRST
        query, and `SELECT pg_advisory_xact_lock(...)` *is* a query. A batch
        that issues it, blocks behind a first selection, and then wins the lock
        still holds the snapshot that statement took — taken BEFORE the
        selection committed. Winning the lock tells it nothing it had not
        already decided: it sees no saved context, resolves the fallback tuple
        and mutates it after the real selection is persisted. **Blocking on a
        lock does not refresh a view the blocking statement itself took.**

        So the wait has to happen OUTSIDE the transaction that authorizes.
        This is a SESSION-scoped advisory lock taken before ``BEGIN`` and held
        across commit or rollback, so the mutation unit's snapshot is created
        only after the wait is over — and any selection that lands afterwards
        blocks until this unit is completely finished.

        RELEASING IS THE DANGEROUS PART, and it is why this is a context
        manager rather than a pair of calls. A session lock survives rollback
        and survives the connection being reused; leaking it once leaves that
        user permanently unable to schedule. The ``finally`` therefore always
        runs, and if the connection is in an aborted state (the unit raised
        mid-transaction) it is rolled back first so the unlock can execute —
        an unlock that silently failed would be exactly the leak this guards
        against. A failure to release is re-raised, never swallowed: a leaked
        mutex must be loud.

        MUST be entered OUTSIDE any transaction; asserted, because acquiring
        inside one would reintroduce the very ordering bug it exists to fix.
        SQLite is a no-op, for the same two-part reason as
        ``_active_context_advisory_lock`` above: ``self._lock`` is a per-
        INSTANCE ``RLock`` (not, as this said before, a process-wide one) and
        serializes every transaction through this store, while
        ``transaction()``'s ``BEGIN IMMEDIATE`` (#392) holds the file's write
        lock against any other connection. That is strictly stronger and needs
        no cross-transaction mutex.
        """
        if not user_id or self.backend != "postgres":
            yield
            return
        key = (self._ACTIVE_CONTEXT_LOCK_NAMESPACE, user_id)
        # The connection lock is taken FIRST and held through the advisory
        # unlock. Two reasons, and the second one is a bug this check itself
        # introduced:
        #
        # * it makes the whole mutex context, the nested `transaction()` and
        #   the release ONE unit on this connection — which a single
        #   PostgreSQL connection requires anyway, since it cannot run two
        #   statements at once;
        # * `_txn_depth` is STORE state, not thread-local, and `transaction()`
        #   holds this same lock for its whole block. Reading the depth
        #   WITHOUT the lock let a request see a DIFFERENT request's open
        #   transaction, mistake it for its own re-entry, and raise instead of
        #   waiting — a 500 on two perfectly valid concurrent requests with no
        #   advisory-lock contention involved at all. Holding the lock means
        #   any depth observed here is genuinely this caller's own.
        #
        # `_lock` is an RLock, so the nested `transaction()` re-enters it
        # freely; nothing here deadlocks against the code it wraps.
        with self._lock:
            if self._txn_depth > 0:
                raise RuntimeError(
                    "active_context_mutex() must be entered OUTSIDE any "
                    "transaction: a mutex acquired inside the transaction it "
                    "protects is taken by a statement that has already fixed "
                    "that transaction's snapshot")
            self._exec("SELECT pg_advisory_lock(?, hashtext(?))", key)
            try:
                yield
            finally:
                try:
                    self._exec(
                        "SELECT pg_advisory_unlock(?, hashtext(?))", key)
                except Exception:
                    # The unit failed mid-transaction and left the connection
                    # aborted. Clear it and release anyway — never leak.
                    self.conn.rollback()
                    self._exec(
                        "SELECT pg_advisory_unlock(?, hashtext(?))", key)

    def get_active_context(self, user_id):
        return self._get(ActiveContext, user_id)

    def get_active_context_for_update(self, user_id):
        """The caller's saved context, ROW-LOCKED until this transaction ends
        (#386).

        The lock half of the protocol that orders an active-tuple
        AUTHORIZATION against a concurrent ``set_active_context``. A consistent
        snapshot is not enough on its own: ``ContextService._snapshot`` gives
        the resolve a coherent read, but nothing stops a competing
        ``POST /api/context`` from committing between that read and the Games
        landing. Whoever takes this lock first wins, and the loser blocks until
        the first one commits — so a write authorized under tuple A can never
        be performed under tuple B.

        ``set_active_context`` deliberately takes NO explicit lock of its own.
        Its ``INSERT ... ON CONFLICT (id) DO UPDATE`` acquires the row lock
        itself when the row exists, so it already blocks behind this one — an
        extra ``SELECT ... FOR UPDATE`` there would be redundant, and it also
        put a second ``user_active_context`` statement in front of the INSERT,
        which silently moved the barrier in
        ``test_active_context.ContextConcurrencyPgTest`` off the write it was
        instrumenting. The ordering is a property of this side of the protocol.

        The row may not exist (a caller running on `_fallback`, never having
        selected anything). ``FOR UPDATE`` then locks nothing — there is no row
        to lock and no lock the writer could block on either. An earlier
        revision leaned on SERIALIZABLE's read->write anti-dependency to order
        that case and the owner's review rejected it: **an absent-row read is
        not a lock**, and one anti-dependency does not oblige PostgreSQL to
        abort either transaction, so a brand new operator could authorize from
        fallback tuple A, make their first saved selection B concurrently, and
        commit BOTH. `_lock_active_context_mutex` above is what actually orders
        the first insert; the row lock remains for the ordinary case.
        """
        self._lock_active_context_mutex(user_id)
        return self._get_for_update(ActiveContext, user_id)

    def set_active_context(self, ctx):
        """Persist a user's selected context (one row per user), last-write-wins.

        An ATOMIC ``INSERT ... ON CONFLICT (id) DO UPDATE`` (portable, as
        ``next_id`` already uses) — NOT a read-then-write upsert: two concurrent
        first writes for the same user would both see "missing" and race the
        PRIMARY KEY, one hitting a raw integrity error/500. Here one INSERTs and
        the other DO-UPDATEs; both commit, exactly one row remains, and the
        last-committed values win.

        ``league_id`` (#345, migration 049) is written on the SAME row and in the
        same statement, so the three axes can never be persisted half-updated:
        a caller that selects Program+Season without a League overwrites any
        previously-saved League with NULL rather than leaving a stale one bound
        to a context it may not belong to."""
        with self.transaction():
            # #386 — take the SAME per-user mutex the authorizing readers
            # take, so a context switch and a write authorized against the
            # tuple it switches away from order against each other on the
            # database. This is the half that covers the caller's very FIRST
            # selection, where there is no row for either side to lock: the
            # advisory lock exists before the row does. An explicit row lock is
            # deliberately NOT taken here — `ON CONFLICT (id) DO UPDATE`
            # acquires it itself, and adding a second `user_active_context`
            # statement ahead of the INSERT silently moved the barrier in
            # `test_active_context.ContextConcurrencyPgTest` off the write it
            # instruments.
            self._lock_active_context_mutex(ctx.id)
            self._exec(
                "INSERT INTO user_active_context "
                "(id, program_id, season_id, updated_at, league_id) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET "
                "program_id = excluded.program_id, "
                "season_id = excluded.season_id, "
                "updated_at = excluded.updated_at, "
                "league_id = excluded.league_id",
                (ctx.id, ctx.program_id, ctx.season_id,
                 ctx.updated_at.isoformat() if ctx.updated_at else None,
                 getattr(ctx, "league_id", None)))
        return ctx
